"""Source report (owner request, 2026-10-25): for every source of companies — your seed list, curated packs,
YC, Hacker News, job sites… — how it's doing and where it breaks, with examples. The point is to notice a
broken source in days (the HN mix-ups were only found at the end). Shown in the dashboard's Maintenance tab;
`python -m app.cli report`; checked weekly, with one note per new warning.

Two kinds of numbers, on purpose:
- discovery (found → have a website → judged → kept) looks at a recent window (90 days by default);
- the companies you keep, their roles, applications and outreach are always counted in full, however old —
  so a long-kept company whose careers page breaks still shows up.
A company found by several sources counts under each one: each source has to stand on its own.
Warnings only where something is really wrong; how recent things are is shown as dates, not warnings.
"""

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.application import Application
from app.models.assessment import JobAssessment
from app.models.audit import AuditLog
from app.models.company import (
    STATUS_CANDIDATE, STATUS_DORMANT, STATUS_PASSED, STATUS_UNRESOLVED, STATUS_WATCHING, Company, CompanySighting,
)
from app.models.feed import CompanyFeed
from app.models.job import JobPosting
from app.models.outreach import OutreachCampaign, OutreachEmail
from app.models.score import JobScore
from app.models.verdict import CompanyVerdict

KEPT = (STATUS_PASSED, STATUS_WATCHING, STATUS_DORMANT)
LABELS = {"seeds": "Your seed list", "packs": "Curated packs", "peers": "AI peers (similar companies)",
          "yc": "YC (Y Combinator)", "hn": "Hacker News", "waas": "Work at a Startup", "adzuna": "Adzuna (job site)",
          "jooble": "Jooble (job site)", "remotive": "Remotive (job site)", "applied": "Your outside applications"}
APPLIED = ("applied", "confirmed", "in_review", "interviewing", "offer", "rejected", "silent")

NAME_MISFIT_SHARE = 0.10    # of kept companies whose name doesn't fit their website
FALLBACK_SHARE = 0.50       # of kept companies whose jobs come only via job sites / posts
FILTERED_SHARE = 0.05       # roles passing the filters / open roles
NEVER_FOUND_RUNS = 3        # a source that ran fine this often and never found a company
BACKLOG_DAYS = 7            # never-judged companies that would take longer than this to judge
FINDER_QUIET_DAYS = 7       # the website-finder found nothing for this long while it had work


@dataclass
class Warning_:
    key: str                     # stable id, so the weekly check notes each warning once
    text: str
    examples: list[str] = field(default_factory=list)
    check: str | None = None     # the command / screen to look deeper with


@dataclass
class SourceRow:
    scout: str
    label: str
    state: str = "ok"            # ok | failing | never_run | not_configured | disabled
    state_note: str | None = None
    last_run: datetime | None = None
    last_run_ok: bool | None = None
    runs: int = 0
    last_new_company: tuple[datetime, str] | None = None
    last_new_role: tuple[datetime, str] | None = None
    # discovery, in the window
    found: int = 0
    with_website: int = 0
    judged: int = 0
    kept_of_found: int = 0
    waiting_judging: int = 0     # never judged, in the queue
    looking_for_website: int = 0
    # the companies kept now (all time) — companies
    kept: int = 0
    read_directly: int = 0
    via_job_sites: int = 0
    only_here: int = 0
    # … and their roles
    open_roles: int = 0
    passed_filters: int = 0
    fast_lane: int = 0
    inbox: int = 0
    maybe: int = 0
    applied: int = 0             # every application, whatever the company's status now
    # outreach, roles at each stage
    to_review: int = 0
    approved: int = 0
    sent: int = 0
    replied: int = 0
    warnings: list[Warning_] = field(default_factory=list)

    @property
    def healthy(self) -> bool:
        return not self.warnings


def _now() -> datetime:
    return datetime.now(timezone.utc)


def name_fits(name: str, domain: str) -> bool:
    """Does the company's name plausibly belong to its website (acme.ai, lilly.com, bcg.com)?"""
    from app.core.hosts import domain_label
    from app.modules.m1_companies.employers import _fit
    from app.modules.m1_companies.identity import name_matches_domain

    words = re.findall(r"[a-z0-9]+", name.lower())
    label = domain_label(domain).replace("-", "")
    return (name_matches_domain(name, domain) or _fit(name, domain) is not None
            or (len(words) > 1 and label in (words[-1], "".join(w[0] for w in words))))


def name_ok(c: Company) -> bool:
    """You marked this name/website pair as correct (`names ok`) — for this website only."""
    return bool(c.domain) and ((c.source_data or {}).get("owner") or {}).get("name_ok") == c.domain


def fits_website(c: Company) -> bool:
    return not c.domain or name_ok(c) or any(name_fits(n, c.domain) for n in [c.name, *(c.aliases or [])])


def _fallback_cause(f: CompanyFeed) -> str:
    ev = f.ownership_evidence or ""
    if m := re.search(r"jobs are on ([\w, ]+) \(no reader yet\)", ev):
        return m.group(1)
    if "custom careers page" in ev:
        return "custom careers page"
    if "Hacker News post" in ev:
        return "no website in their post"
    return "no careers page found"


def build(session: Session, days: int | None = 90) -> dict:
    since = _now() - timedelta(days=days) if days else None
    seen_by: dict[str, set[int]] = defaultdict(set)        # all time
    recent_by: dict[str, set[int]] = defaultdict(set)      # in the window
    scouts_of: dict[int, set[str]] = defaultdict(set)
    newest: dict[str, tuple[datetime, int]] = {}
    for scout, cid, first in session.execute(
            select(CompanySighting.scout, CompanySighting.company_id, CompanySighting.first_seen_at)).all():
        seen_by[scout].add(cid)
        scouts_of[cid].add(scout)
        if since is None or (first and first >= since):
            recent_by[scout].add(cid)
        if first and (scout not in newest or first > newest[scout][0]):
            newest[scout] = (first, cid)

    ids = set(scouts_of)
    companies = {c.id: c for c in session.scalars(select(Company).where(Company.id.in_(ids or [0]))).all()}
    feeds = {f.company_id: f for f in session.scalars(select(CompanyFeed).where(CompanyFeed.company_id.in_(ids or [0])))}
    judged_ids = set(session.scalars(select(CompanyVerdict.company_id).where(
        CompanyVerdict.company_id.in_(ids or [0])).distinct()))
    open_roles = dict(session.execute(select(JobPosting.company_id, func.count()).where(
        JobPosting.status == "open").group_by(JobPosting.company_id)).all())
    passed = dict(session.execute(select(JobPosting.company_id, func.count()).where(
        JobPosting.status == "open", JobPosting.filter_status == "passed").group_by(JobPosting.company_id)).all())
    newest_role: dict[int, tuple[datetime, str]] = {}
    for cid, first, title in session.execute(select(JobPosting.company_id, JobPosting.first_seen_at, JobPosting.title)
                                             .order_by(JobPosting.first_seen_at)).all():
        newest_role[cid] = (first, title)
    applied = dict(session.execute(select(Application.company_id, func.count()).where(
        Application.status.in_(APPLIED)).group_by(Application.company_id)).all())
    bands: dict[int, Counter] = defaultdict(Counter)
    shortlisted: list[dict] = []
    for sc, job in session.execute(
            select(JobScore, JobPosting).join(JobAssessment, JobAssessment.id == JobScore.assessment_id)
            .join(JobPosting, JobPosting.id == JobAssessment.job_id)
            .where(JobPosting.status == "open", JobAssessment.outcome == "passed")).all():
        c = companies.get(job.company_id)
        if c is None or c.status not in KEPT or sc.owner_action in ("skip", "thumbs_down"):
            continue
        bands[job.company_id][sc.band] += 1
        if sc.band in ("fast_lane", "inbox"):
            shortlisted.append({"score": sc.score, "band": sc.band, "company": c.name, "title": job.title,
                                "cluster_id": sc.cluster_id, "sources": sorted(scouts_of.get(job.company_id, ()))})
    stage = _outreach_stages(session)

    from app.modules.m1_companies.health import scout_health

    health = {h["scout"]: h for h in scout_health(session)}
    rows: list[SourceRow] = []
    for scout in sorted(set(seen_by) | set(health), key=lambda s: list(LABELS).index(s) if s in LABELS else 99):
        r = SourceRow(scout, LABELS.get(scout, scout))
        _state(r, health.get(scout))
        if scout in newest and newest[scout][1] in companies:
            r.last_new_company = (newest[scout][0], companies[newest[scout][1]].name)
        misfits, fallback = [], Counter()
        for cid in seen_by.get(scout, ()):
            c = companies.get(cid)
            if c is None:
                continue
            if cid in recent_by.get(scout, ()):                        # discovery, in the window
                r.found += 1
                r.with_website += bool(c.domain)
                r.judged += cid in judged_ids
                r.kept_of_found += c.status in KEPT
                if c.status == STATUS_UNRESOLVED:
                    r.looking_for_website += 1
                elif c.status == STATUS_CANDIDATE and cid not in judged_ids:
                    r.waiting_judging += 1
            r.applied += applied.get(cid, 0)                          # whatever its status now
            for stg, n in stage.get(cid, Counter()).items():
                setattr(r, stg, getattr(r, stg) + n)
            if cid in newest_role and (r.last_new_role is None or newest_role[cid][0] > r.last_new_role[0]):
                r.last_new_role = (newest_role[cid][0], f"{newest_role[cid][1]} at {c.name}")
            if c.status not in KEPT:
                continue
            r.kept += 1                                               # kept now, however old
            f = feeds.get(cid)
            if f is not None and f.method != "aggregator":
                r.read_directly += 1
            elif f is not None:
                r.via_job_sites += 1
                fallback[_fallback_cause(f)] += 1
            if not fits_website(c):
                misfits.append(f"{c.name} ({c.domain})")
            if scouts_of.get(cid, set()) <= {scout}:
                r.only_here += 1
            r.open_roles += open_roles.get(cid, 0)
            r.passed_filters += passed.get(cid, 0)
            b = bands.get(cid, Counter())
            r.fast_lane, r.inbox, r.maybe = r.fast_lane + b["fast_lane"], r.inbox + b["inbox"], r.maybe + b["maybe"]
        _warn(r, health.get(scout), misfits, fallback)
        rows.append(r)
    shortlisted.sort(key=lambda x: -x["score"])
    return {"days": days, "rows": rows, "system": _system_warnings(session, companies),
            "shortlisted": shortlisted[:30], "generated_at": _now()}


def _outreach_stages(session: Session) -> dict[int, Counter]:
    """Per company, how many roles' outreach stands at each stage (each role once, at the stage it reached)."""
    sent_or_replied: dict[int, str] = {}
    for cid, replied in session.execute(
            select(OutreachEmail.campaign_id, OutreachEmail.replied_at)
            .where(OutreachEmail.status.in_(("sent", "bounced")))).all():
        sent_or_replied[cid] = "replied" if replied or sent_or_replied.get(cid) == "replied" else "sent"
    out: dict[int, Counter] = defaultdict(Counter)
    for c in session.scalars(select(OutreachCampaign)).all():
        if c.id in sent_or_replied:
            out[c.company_id][sent_or_replied[c.id]] += 1
        elif c.status == "pending_review":
            out[c.company_id]["to_review"] += 1
        elif c.status in ("scheduled", "running", "paused") and c.armed_at is not None:
            out[c.company_id]["approved"] += 1
    return out


def _state(r: SourceRow, h: dict | None) -> None:
    if h is None:
        return
    r.state = h["health"]
    last = h.get("last_run") or {}
    r.last_run = last.get("finished_at") or last.get("started_at")
    r.last_run_ok = None if not last else last.get("status") != "failed"
    if r.state == "not_configured":
        r.state_note = f"not set up ({h.get('reason') or 'needs a key'}) — optional"
    elif r.state == "disabled":
        r.state_note = "switched off"
    r.runs = h.get("successful_runs", 0)


def _warn(r: SourceRow, h: dict | None, misfits: list[str], fallback: Counter) -> None:
    if r.state == "failing":
        err = ((h.get("last_run") or {}).get("error") or "")[:120] if h else ""
        r.warnings.append(Warning_(f"{r.scout}:failing", f"source failing: the last {h['consecutive_failures']} "
                                   f"run(s) failed" + (f" — {err}" if err else ""), check="python -m app.cli stats"))
    elif r.state == "never_run":
        r.warnings.append(Warning_(f"{r.scout}:never_run", "switched on, but has never run",
                                   check="python -m app.cli stats"))
    elif r.state == "ok" and r.last_new_company is None and r.runs >= NEVER_FOUND_RUNS:
        r.warnings.append(Warning_(f"{r.scout}:never_found", f"has run {r.runs} times and never found a company",
                                   check="python -m app.cli stats"))
    if r.kept >= 3 and len(misfits) / r.kept >= NAME_MISFIT_SHARE:
        r.warnings.append(Warning_(f"{r.scout}:names", f"names don't fit websites: {len(misfits)} of {r.kept} kept "
                                   "companies — a wrong website means the wrong jobs are read", misfits[:3],
                                   check="python -m app.cli names"))
    if r.kept >= 3 and r.via_job_sites / r.kept >= FALLBACK_SHARE:
        causes = ", ".join(f"{k} {n}" for k, n in fallback.most_common(3))
        r.warnings.append(Warning_(f"{r.scout}:not_read", f"jobs not read directly for {r.via_job_sites} of {r.kept} "
                                   f"kept companies — {causes}", check="python -m app.cli coverage"))
    if r.open_roles >= 20 and r.passed_filters / r.open_roles < FILTERED_SHARE:
        r.warnings.append(Warning_(f"{r.scout}:filtered", f"almost no roles pass the filters: {r.passed_filters} of "
                                   f"{r.open_roles} open roles", check="python -m app.cli funnel"))


def _system_warnings(session: Session, companies: dict[int, Company]) -> list[Warning_]:
    """Things about the whole pipeline rather than one source."""
    from app.config_loader import get_config

    cfg = get_config()
    out: list[Warning_] = []
    judged = select(CompanyVerdict.company_id)
    never_judged = session.scalar(select(func.count()).select_from(Company).where(
        Company.status == STATUS_CANDIDATE, Company.id.not_in(judged))) or 0
    per_day = cfg.rules.evaluation.max_new_per_day
    if per_day and never_judged / per_day > BACKLOG_DAYS:
        out.append(Warning_("system:backlog", f"{never_judged} companies waiting to be judged — about "
                            f"{round(never_judged / per_day)} days at {per_day}/day",
                            check="python -m app.cli evaluate --force --limit 200 (or raise max_new_per_day)"))
    pending = session.scalar(select(func.count()).select_from(Company).where(
        Company.status == STATUS_UNRESOLVED, Company.resolution_attempts < cfg.scouts.resolver.max_attempts)) or 0
    if pending:
        recent = session.scalar(select(func.count()).select_from(AuditLog).where(
            AuditLog.action == "domain_resolved", AuditLog.created_at >= _now() - timedelta(days=FINDER_QUIET_DAYS))) or 0
        if not recent:
            out.append(Warning_("system:finder", f"website-finder: {pending} companies waiting, no website found in "
                                f"the last {FINDER_QUIET_DAYS} days (no search key, or its budget used up?)",
                                check="python -m app.cli stats"))
    odd = []
    for a in session.scalars(select(AuditLog).where(AuditLog.action == "company_merged")).all():
        d = a.detail or {}
        c = companies.get(int(a.subject_id)) if (a.subject_id or "").isdigit() else None
        merged = d.get("merged_name") or ""
        if c is None or c.status not in KEPT or not c.domain or name_ok(c) or merged not in (c.aliases or []):
            continue
        if not any(w in (d.get("reason") or "") for w in ("via search", "via llm")):
            continue          # only merges made by a website guess
        if not name_fits(merged, c.domain):
            odd.append(f"“{merged}” merged into {c.name} ({c.domain})")
    if odd:
        out.append(Warning_("system:merges", f"{len(odd)} merge(s) by a website guess where the name doesn't fit — "
                            "possibly a different company", odd[:3],
                            check="look at the company (python -m app.cli jobs --domain <website>) and ask Claude "
                                  "to separate them if they're different companies"))
    return out


# --- the weekly self-check --------------------------------------------------------------------------------

def weekly_check(session: Session) -> dict:
    """One note per warning that's new (a warning that persists is repeated at most once a month). Notes are
    saved for `pings` now, and shown in the summary mail's Housekeeping line once Module 6 is built."""
    from app.core.notify import notify
    from app.models.notification import Notification

    report = build(session, days=90)
    since = _now() - timedelta(days=30)
    noted, total = 0, 0
    labelled = [(r.label, w) for r in report["rows"] for w in r.warnings] + [("Pipeline", w) for w in report["system"]]
    for label, w in labelled:
        total += 1
        if session.scalar(select(Notification.id).where(Notification.subject_type == "source_warning",
                                                        Notification.subject_id == w.key[:64],
                                                        Notification.created_at >= since).limit(1)):
            continue
        notify(session, "update", "housekeeping", f"{label}: {w.text}"[:500],
               detail=("e.g. " + ", ".join(w.examples) + ". " if w.examples else "") + (f"Check: {w.check}" if w.check else ""),
               subject=("source_warning", w.key[:64]))
        noted += 1
    return {"warnings": total, "noted": noted}
