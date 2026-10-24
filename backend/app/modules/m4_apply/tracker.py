"""Module 4: apply tracker (spec §4, v1 — NO form filling).

1. Queue the link: pursued roles (config/apply.yaml bands + roles you marked `act`) go on your apply list
   with the company's own apply link(s), live-checked; sibling openings (several job IDs) all included.
2. Watch for proof: confirmation emails from hiring systems mark a role Applied (m4_apply.inbox); for systems
   that don't send one, you mark it yourself (`applied`).
3. Reminders: a pending link is a full card for a few days, then folds into "still pending (N)"; it leaves the
   list when you apply, skip it, or the posting closes.
4. Lifecycle: applied → confirmed → in review → interviewing → offer / rejected / silent, joined with the
   outreach for the same role into one story.
"""

import logging
import re
from collections import Counter
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.config_loader import get_config
from app.core import audit
from app.core.http import PoliteClient
from app.core.notify import notify
from app.models.application import STATUS_RANK, Application, ApplicationEvent
from app.models.assessment import JobAssessment
from app.models.company import STATUS_BLOCKED, Company
from app.models.job import JobCluster, JobPosting
from app.models.outreach import OutreachCampaign, OutreachEmail
from app.models.score import JobScore
from app.modules.m3_outreach.drafting import req_id

log = logging.getLogger(__name__)

FINAL = ("offer", "rejected", "silent", "skipped", "closed")
IN_FLIGHT = ("applied", "confirmed", "in_review", "interviewing")
_CLOSED_PAGE = re.compile(r"no longer (accepting|available|open)|position has been filled|job (is )?(closed|expired)|"
                          r"this job (posting )?(has been|was) (removed|closed)|job not found|page not found", re.I)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --- status -------------------------------------------------------------------------------------

def set_status(session: Session, app: Application, status: str, *, source: str, at: datetime | None = None,
               summary: str | None = None, link: str | None = None, message_id: str | None = None) -> bool:
    """Move an application forward, never backwards. Final states stay final, except: a silent application
    comes back to life when news arrives, and a closed/skipped one when you (or a receipt) show you applied
    after all. Every move is an event in the role's story. Returns True when the status changed."""
    at = at or _now()
    current = app.status
    if source == "email" and (app.last_heard_at is None or at > app.last_heard_at):
        app.last_heard_at = at                # any news counts against silence, even if the status stays
    if status == current:
        return False
    revived = current in ("silent", "closed", "skipped") and status not in ("silent", "skipped", "closed")
    if current in FINAL and not revived:
        return False
    if not revived and status not in ("offer", "rejected", "silent", "skipped", "closed") \
            and STATUS_RANK[status] <= STATUS_RANK[current]:
        return False
    if status in ("skipped", "closed") and current != "queued":
        return False                          # once you've applied, a closed posting doesn't undo it
    if current in ("queued", "closed", "skipped") and status in ("confirmed", "in_review", "interviewing", "offer",
                                                                  "rejected"):
        _event(session, app, "applied", source, at, "applied (seen from the hiring system's email)", link, message_id)
        app.applied_at, app.applied_how = at, "email" if source == "email" else "you"
    if status == "applied":
        app.applied_at, app.applied_how = at, "email" if source == "email" else "you"
    app.status, app.status_at = status, at
    _event(session, app, status, source, at, summary, link, message_id)
    return True


def _event(session: Session, app: Application, kind: str, source: str, at: datetime, summary, link, message_id) -> None:
    session.add(ApplicationEvent(application_id=app.id, kind=kind, source=source, at=at, summary=summary,
                                 link=link, message_id=message_id))


def get_or_create(session: Session, cluster_id: int, *, on_list: bool) -> Application:
    app = session.scalar(select(Application).where(Application.cluster_id == cluster_id))
    if app is not None:
        return app
    company_id = session.get(JobCluster, cluster_id).company_id
    app = Application(cluster_id=cluster_id, company_id=company_id, status="queued", on_list=on_list, links=[],
                      queued_at=_now() if on_list else None)
    session.add(app)
    session.flush()
    if on_list:
        _event(session, app, "queued", "agent", _now(), "added to your apply list", None, None)
    return app


# --- 1. the apply list ------------------------------------------------------------------------------

def role_links(session: Session, cluster_id: int) -> list[dict]:
    """The company's own form for every open opening of this role (job-site leads only if nothing better)."""
    jobs = session.scalars(select(JobPosting).where(JobPosting.cluster_id == cluster_id, JobPosting.status == "open",
                                                    JobPosting.url.is_not(None))
                           .order_by(JobPosting.lead_only, JobPosting.first_seen_at, JobPosting.id)).all()
    direct = [j for j in jobs if not j.lead_only]
    chosen = direct or jobs[:1]
    seen, out = set(), []
    for j in chosen:
        if j.url in seen:
            continue
        seen.add(j.url)
        out.append({"req_id": req_id(j), "url": j.url, "lead": j.lead_only})
    return out


def queue_roles(session: Session) -> Counter:
    """Pursued roles onto the list; roles whose postings all closed (or that you skipped) come off it."""
    cfg = get_config().apply
    counts: Counter = Counter()
    rows = session.execute(
        select(JobScore, JobPosting, Company)
        .join(JobAssessment, JobAssessment.id == JobScore.assessment_id)
        .join(JobPosting, JobPosting.id == JobAssessment.job_id)
        .join(Company, Company.id == JobPosting.company_id)
        .where(JobAssessment.outcome == "passed", JobPosting.status == "open", Company.status != STATUS_BLOCKED,
               or_(Company.status != "rejected", JobScore.owner_action == "act"),
               or_(JobScore.band.in_(cfg.apply_bands), JobScore.owner_action == "act"),
               or_(JobScore.owner_action.is_(None), JobScore.owner_action.not_in(("skip", "thumbs_down"))))
        .order_by(JobScore.score.desc())
    ).all()
    for score, job, company in rows:
        app = session.scalar(select(Application).where(Application.cluster_id == score.cluster_id))
        if app is None:
            app = get_or_create(session, score.cluster_id, on_list=True)
            app.links = role_links(session, score.cluster_id)
            counts["queued"] += 1
            notify(session, "needs_you", "needs_you", f"Apply: {job.title} at {company.name}",
                   detail=" · ".join(l["url"] for l in app.links[:3]) or "no link found yet",
                   link=app.links[0]["url"] if app.links else None, subject=("job_cluster", str(score.cluster_id)))
        elif not app.on_list and app.status == "queued":
            app.on_list, app.queued_at = True, _now()
            counts["queued"] += 1
    # Off the list: the posting closed, or you skipped the role.
    for app in session.scalars(select(Application).where(Application.status == "queued")).all():
        still_open = session.scalar(select(JobPosting.id).where(JobPosting.cluster_id == app.cluster_id,
                                                                JobPosting.status == "open").limit(1))
        score = session.scalar(select(JobScore).where(JobScore.cluster_id == app.cluster_id))
        if still_open is None:
            set_status(session, app, "closed", source="agent", summary="the posting closed")
            counts["closed"] += 1
        elif score is not None and score.owner_action in ("skip", "thumbs_down"):
            set_status(session, app, "skipped", source="you", summary=f"you chose {score.owner_action}")
            counts["skipped"] += 1
    return counts


def check_link(http: PoliteClient, url: str) -> tuple[bool | None, str]:
    """(ok, why). None = couldn't tell (e.g. the site blocks checks) — the link is still shown."""
    try:
        resp = http.get(url, scrape=True)
    except Exception as e:
        return None, f"couldn't check ({type(e).__name__})"
    if resp.status_code in (404, 410):
        return False, f"page gone ({resp.status_code})"
    if resp.status_code >= 400:
        return None, f"site answered {resp.status_code}"
    if _CLOSED_PAGE.search(resp.text[:200_000]):
        return False, "page says the job is closed"
    return True, "live"


def check_links(session: Session, http: PoliteClient, *, limit: int = 15, checker=check_link) -> Counter:
    """Live-check apply links before they're shown (and again every `link_check_hours`)."""
    cfg = get_config().apply
    counts: Counter = Counter()
    stale = _now() - timedelta(hours=cfg.link_check_hours)
    apps = session.scalars(select(Application).where(
        Application.status == "queued", Application.on_list.is_(True),
        or_(Application.links_checked_at.is_(None), Application.links_checked_at < stale))
        .order_by(Application.links_checked_at.asc().nulls_first()).limit(limit)).all()
    for app in apps:
        links = role_links(session, app.cluster_id) or [dict(l) for l in app.links]  # a fresh list: JSONB saves it
        for link in links:
            link["ok"], link["why"] = checker(http, link["url"])
            counts["live" if link["ok"] else "dead" if link["ok"] is False else "unknown"] += 1
        app.links, app.links_checked_at = links, _now()
    return counts


# --- 2. your own marks ------------------------------------------------------------------------------

def mark_applied(session: Session, cluster_id: int, note: str | None = None) -> Application:
    if session.get(JobCluster, cluster_id) is None:
        raise ValueError(f"no role with id {cluster_id}")
    app = get_or_create(session, cluster_id, on_list=False)
    if not set_status(session, app, "applied", source="you", summary=note or "you marked it applied"):
        raise ValueError(f"role {cluster_id} is already {app.status}")
    audit.record(session, "m4", "marked_applied", f"You marked role {cluster_id} applied", subject_type="job_cluster",
                 subject_id=str(cluster_id))
    return app


def skip(session: Session, cluster_id: int, reason: str) -> Application:
    app = session.scalar(select(Application).where(Application.cluster_id == cluster_id))
    if app is None or app.status != "queued":
        raise ValueError(f"role {cluster_id} isn't on your apply list")
    set_status(session, app, "skipped", source="you", summary=reason)
    app.note = reason
    return app


# --- 3. reminders -----------------------------------------------------------------------------------

def apply_list(session: Session, now: datetime | None = None) -> tuple[list[tuple[Application, JobCluster, Company]], int]:
    """(full cards, how many more are 'still pending'). For the summary mail (Module 6) and the CLI."""
    now = now or _now()
    days = get_config().apply.full_card_days
    rows = session.execute(
        select(Application, JobCluster, Company).join(JobCluster, JobCluster.id == Application.cluster_id)
        .join(Company, Company.id == Application.company_id)
        .where(Application.status == "queued", Application.on_list.is_(True))
        .order_by(Application.queued_at)
    ).all()
    full = [r for r in rows if r[0].queued_at and now - r[0].queued_at <= timedelta(days=days)]
    return full, len(rows) - len(full)


# --- 4. lifecycle -----------------------------------------------------------------------------------

def stamp_silence(session: Session, now: datetime | None = None) -> int:
    now = now or _now()
    days = get_config().apply.silent_after_days
    n = 0
    for app in session.scalars(select(Application).where(Application.status.in_(IN_FLIGHT))).all():
        last = max(t for t in (app.last_heard_at, app.applied_at, app.status_at) if t is not None)
        if now - last >= timedelta(days=days):
            set_status(session, app, "silent", source="agent", at=now,
                       summary=f"nothing heard for {days} days")
            n += 1
    return n


def story(session: Session, cluster_id: int) -> list[tuple[datetime, str, str]]:
    """One opening's whole story: outreach (Module 3) and the application (Module 4), oldest first."""
    out: list[tuple[datetime, str, str]] = []
    for c in session.scalars(select(OutreachCampaign).where(OutreachCampaign.cluster_id == cluster_id)).all():
        out.append((c.created_at, "outreach", "drafts ready for your review"))
        if c.armed_at:
            out.append((c.armed_at, "outreach", "you approved the emails"))
        for e in session.scalars(select(OutreachEmail).where(OutreachEmail.campaign_id == c.id)).all():
            who = e.to_name or e.to_email
            if e.sent_at:
                out.append((e.sent_at, "outreach", f"emailed {who} (day {e.wave + 1})"))
            if e.bounced_at:
                out.append((e.bounced_at, "outreach", f"{who}: bounced"))
            if e.replied_at or e.auto_reply_at:
                out.append((e.replied_at or e.auto_reply_at, "outreach",
                            f"{who} replied: {e.reply_category} — {e.reply_summary or ''}".strip(" —")))
        if c.outcome_at and c.outcome == "silent":
            out.append((c.outcome_at, "outreach", "no responses to the outreach"))
    app = session.scalar(select(Application).where(Application.cluster_id == cluster_id))
    if app is not None:
        for ev in session.scalars(select(ApplicationEvent).where(ApplicationEvent.application_id == app.id)).all():
            out.append((ev.at, "application", f"{ev.kind.replace('_', ' ')}" + (f": {ev.summary}" if ev.summary else "")))
    return sorted(out, key=lambda x: x[0])


def run_tracking(*, scan: bool = True) -> dict:
    from app.db import session_scope
    from app.modules.m4_apply.inbox import scan_inbox

    counts: Counter = Counter()
    with session_scope() as s:
        counts.update({f"list_{k}": v for k, v in queue_roles(s).items()})
    with PoliteClient() as http, session_scope() as s:
        counts.update({f"links_{k}": v for k, v in check_links(s, http).items()})
    if scan and get_config().apply.inbox_scan:
        try:
            counts.update({f"inbox_{k}": v for k, v in scan_inbox().items()})
        except Exception as e:  # Gmail not connected / revoked: the rest of tracking still runs
            counts["inbox_skipped"] = 1
            log.info("inbox scan skipped: %s", e)
    with session_scope() as s:
        counts["silent"] = stamp_silence(s)
    return dict(counts)
