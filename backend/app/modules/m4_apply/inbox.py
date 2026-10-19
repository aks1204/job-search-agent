"""Module 4: watch for proof and status (owner decision 2026-10-19: a narrow inbox search).

Only emails FROM hiring systems (config/apply.yaml `ats_senders`) or from companies you're tracking, from the
last `lookback_days`, are looked at — each once. Threads the agent started are left to Part 3d. Nothing from
the email is stored except facts: which role, what happened, when, and a link to it in Gmail.

Each email gets a cheap AI read (is it about an application? received / in review / assessment / interview /
offer / rejection; which company, job title, job ID), then deterministic matching to a role:
job ID → title within the company → your one live application there. Two or more could fit → you're asked.

Outside applications (owner decisions 2026-10-19): from `track_outside_since` on, applying to a job the agent
doesn't know is tracked too (company + title from the email, labelled outside); a company it doesn't know is
handed to Module 1 ("you applied here") and judged by the usual rules. Earlier emails are past applications
and are not tracked. An unmatched interview or offer is put to you (`assign`); other unmatched news is quiet.
"""

import logging
import re
from collections import Counter
from datetime import datetime, timezone
from email.utils import parseaddr
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config_loader import get_config
from app.core.hosts import normalize_domain
from app.core.llm import LLM
from app.core.notify import notify
from app.db import session_scope
from app.models.application import Application, InboxMessage
from app.models.company import Company
from app.models.job import JobCluster, JobPosting
from app.models.outreach import OutreachEmail
from app.modules.m1_companies.identity import normalize_name
from app.modules.m3_outreach import gmail
from app.modules.m3_outreach.drafting import req_id
from app.modules.m4_apply.tracker import get_or_create, set_status

log = logging.getLogger(__name__)

KIND_TO_STATUS = {"received": "confirmed", "in_review": "in_review", "assessment": "in_review",
                  "interview": "interviewing", "offer": "offer", "rejection": "rejected"}
_STOP = {"and", "the", "of", "for", "a", "an", "in", "at", "to", "with", "role", "position", "job", "opening"}


class AtsRead(BaseModel):
    about_application: bool = Field(description="Is this email about a job application the candidate made?")
    kind: Literal["received", "in_review", "assessment", "interview", "offer", "rejection", "other"] = Field(
        description="received = application received/submitted; in_review = being reviewed/shortlisted; "
                    "assessment = a test or assignment; interview = an interview invite or scheduling; "
                    "offer = an offer; rejection = not moving forward; other = anything else.")
    company: str = Field(description="The hiring company's name ('' if not stated).")
    job_title: str = Field(description="The job title ('' if not stated).")
    job_id: str = Field(description="The job/requisition ID if stated, e.g. R-112054 ('' if not).")
    summary: str = Field(description="What the email says, at most 15 words.")
    confidence: float = Field(description="0 to 1.")


SYSTEM = """You read one email a job seeker received from a hiring system or a company, and say whether it is about
one of their job applications and what it says. Answer in the requested JSON format."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def build_query(session: Session) -> str:
    cfg = get_config().apply
    domains = list(dict.fromkeys(cfg.ats_senders))
    tracked = session.scalars(select(Company.domain).join(Application, Application.company_id == Company.id)
                              .where(Company.domain.is_not(None))).all()
    mail = get_config().outreach.contacts.mail_domains  # some companies email from another domain
    domains += [m for d in tracked if (m := mail.get(d, d)) not in domains]
    return f"newer_than:{cfg.lookback_days}d -in:sent -in:chats (" + " OR ".join(f"from:{d}" for d in domains) + ")"


def _tokens(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (s or "").lower()) if w not in _STOP and len(w) > 1}


def _company_for(session: Session, read: AtsRead, sender_domain: str | None) -> Company | None:
    if sender_domain:
        mail_map = {v: k for k, v in get_config().outreach.contacts.mail_domains.items()}
        c = session.scalar(select(Company).where(Company.domain == mail_map.get(sender_domain, sender_domain)))
        if c is not None:
            return c
    key = normalize_name(read.company) if read.company else ""
    if not key:
        return None
    exact = session.scalars(select(Company).where(Company.normalized_name == key)).all()
    if len(exact) == 1:
        return exact[0]
    # "Eli Lilly and Company" vs "Eli Lilly": containment, among companies with postings, if unique
    near = [c for c in session.scalars(select(Company).join(JobPosting, JobPosting.company_id == Company.id)
                                       .distinct()).all()
            if c.normalized_name and (c.normalized_name in key or key in c.normalized_name)]
    return near[0] if len(near) == 1 else None


OUTSIDE_PREFIX = "outside:"
NOT_NAMED = "(role not named)"


def _role_titles(session: Session, company: Company) -> dict[int, list[str]]:
    """Every role the agent knows at this company → its titles (postings, and outside applications)."""
    out: dict[int, list[str]] = {}
    for j in session.scalars(select(JobPosting).where(JobPosting.company_id == company.id,
                                                      JobPosting.cluster_id.is_not(None))).all():
        out.setdefault(j.cluster_id, []).append(j.title)
    for cl in session.scalars(select(JobCluster).where(JobCluster.company_id == company.id,
                                                       JobCluster.fingerprint.like(OUTSIDE_PREFIX + "%"))).all():
        out.setdefault(cl.id, []).append(cl.title)
    return out


def _live_apps(session: Session, company: Company) -> list[Application]:
    live = session.scalars(select(Application).where(
        Application.company_id == company.id,
        Application.status.not_in(("queued", "skipped", "closed", "rejected", "offer")))).all()
    return live or session.scalars(select(Application).where(Application.company_id == company.id,
                                                             Application.status == "queued")).all()


def match_role(session: Session, read: AtsRead, sender_domain: str | None
               ) -> tuple[str, int | None, Company | None, list[int]]:
    """(outcome, cluster_id, company, candidates):
    matched     — exactly one role fits (by job ID, then title, then your one live application there)
    ambiguous   — two or more of your roles could fit (candidates listed): you're asked
    not_tracked — a known company, but not a role the agent knows (or nothing live there)
    unmatched   — not a company the agent knows"""
    company = _company_for(session, read, sender_domain)
    if company is None:
        return "unmatched", None, None, []
    outcome, cluster_id, candidates = match_in_company(session, read, company)
    return outcome, cluster_id, company, candidates


def match_in_company(session: Session, read: AtsRead, company: Company) -> tuple[str, int | None, list[int]]:
    """Which of the agent's roles at this company the email is about: job ID → title → your one live application."""
    titles = _role_titles(session, company)
    if read.job_id:
        want = read.job_id.strip().upper()
        jobs = session.scalars(select(JobPosting).where(JobPosting.company_id == company.id,
                                                        JobPosting.cluster_id.is_not(None))).all()
        hits = {j.cluster_id for j in jobs if req_id(j).upper() == want or want in (j.external_id or "").upper()}
        if len(hits) == 1:
            return "matched", hits.pop(), []
    if read.job_title:
        want = _tokens(read.job_title)
        scored = {cid: max(len(want & _tokens(t)) / len(want) for t in ts) for cid, ts in titles.items() if want}
        good = sorted((s, cid) for cid, s in scored.items() if s >= 0.6)
        if not good:
            return "not_tracked", None, []
        if len(good) == 1 or good[-1][0] > good[-2][0]:
            return "matched", good[-1][1], []
        top = good[-1][0]
        return "ambiguous", None, [cid for s, cid in good if s == top]
    live = _live_apps(session, company)
    if len(live) == 1:
        return "matched", live[0].cluster_id, []
    if live:
        return "ambiguous", None, [a.cluster_id for a in live]
    return "not_tracked", None, []


def classify(session: Session, llm, m: dict, text: str) -> AtsRead:
    user = f"From: {m['from']}\nSubject: {m['subject']}\n\n{(text or m.get('snippet', ''))[:3500]}"
    return llm.structured("fast", system=SYSTEM, user=user, schema=AtsRead, purpose="m4.read_ats_email",
                          session=session, subject=("gmail", m["id"])).parsed


# --- outside applications (owner decisions 2026-10-19) ----------------------------------------------

def _outside_since() -> datetime | None:
    d = get_config().apply.track_outside_since
    from zoneinfo import ZoneInfo

    # midnight in YOUR time zone (app.yaml), not UTC
    return datetime(d.year, d.month, d.day, tzinfo=ZoneInfo(get_config().app.timezone)) if d else None


def _is_new(received: datetime) -> bool:
    since = _outside_since()
    return since is not None and received >= since


def company_for_outside(session: Session, read: AtsRead, sender_domain: str | None) -> Company | None:
    """A company you applied to that the agent doesn't know: handed to Module 1 like a scout's find
    ("you applied here") and judged by the usual rules; if it passes, its jobs are watched."""
    from app.modules.m1_companies.candidate import Candidate
    from app.modules.m1_companies.identity import intake
    from app.models.company import CompanySighting

    if not read.company.strip():
        return None
    ats = {d.lower() for d in get_config().apply.ats_senders}
    from app.modules.m1_companies.identity import _clean_domain

    # an ATS or webmail sender domain never identifies the company
    usable = sender_domain and sender_domain not in ats and sender_domain not in _WEBMAIL
    domain = _clean_domain(sender_domain) if usable else None
    external_id = domain or normalize_name(read.company)
    intake(session, Candidate(scout="applied", external_id=external_id[:255], name=read.company.strip(),
                              domain=domain, data={"applied": True, "job_title": read.job_title}))
    sighting = session.scalar(select(CompanySighting).where(CompanySighting.scout == "applied",
                                                            CompanySighting.external_id == external_id[:255]))
    return session.get(Company, sighting.company_id) if sighting else None


_WEBMAIL = {"gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "yahoo.com"}


def outside_role(session: Session, company: Company, title: str | None) -> int:
    """The role an outside application hangs on: one cluster per (company, title)."""
    name = (title or "").strip() or NOT_NAMED
    fingerprint = (OUTSIDE_PREFIX + (" ".join(sorted(_tokens(name))) or "role-not-named"))[:300]
    cl = session.scalar(select(JobCluster).where(JobCluster.company_id == company.id,
                                                 JobCluster.fingerprint == fingerprint))
    if cl is None:
        cl = JobCluster(company_id=company.id, fingerprint=fingerprint, title=name[:500], size=0)
        session.add(cl)
        session.flush()
    return cl.id


def _apply_to(session: Session, row: InboxMessage, cluster_id: int, kind: str, *, outside: bool,
              summary: str | None, company: Company) -> None:
    app = get_or_create(session, cluster_id, on_list=False)
    if outside:
        app.outside = True
    row.application_id = app.id
    link = gmail.thread_link(row.thread_id) if row.thread_id else None
    status = KIND_TO_STATUS[kind]
    if set_status(session, app, status, source="email", at=row.received_at or _now(), summary=summary, link=link,
                  message_id=row.message_id):
        if kind == "received" and not app.receipt_link:
            app.receipt_link = link
        if status in ("interviewing", "offer", "rejected"):
            notify(session, "update", "happened", f"{company.name}: {status.replace('_', ' ')}",
                   detail=summary, link=link, subject=("job_cluster", str(cluster_id)))


def _ask(session: Session, row: InboxMessage, company: Company, subject: str, kind: str) -> None:
    roles = [session.get(JobCluster, cid) for cid in row.candidates]
    choices = " · ".join(f"[{r.id}] {r.title}" for r in roles if r is not None)
    detail = f"“{subject[:120]}” ({kind}). "
    detail += (f"Could be: {choices}. Pick one: assign {row.no} <role id>" if choices
               else f"It doesn't match any role I know at {company.name}.")
    detail += f" · another job: assign {row.no} --other \"Job title\" · not yours: assign {row.no} --ignore"
    notify(session, "needs_you", "needs_you", f"{company.name}: which role is this email about?", detail=detail,
           link=gmail.thread_link(row.thread_id) if row.thread_id else None, subject=("inbox", str(row.no)))


def handle(session: Session, m: dict, read: AtsRead) -> str:
    """Apply one sorted email. Returns the outcome recorded.

    Your roles are matched whatever the date. For anything else, only emails from `track_outside_since` on
    count (older ones are past applications): a named job you applied to becomes an outside application;
    an unnamed receipt becomes "(role not named)"; an unmatched interview/offer is put to you; other unmatched
    news (rejection, in review) is recorded quietly."""
    sender = normalize_domain(parseaddr(m["from"])[1]) or None
    received = datetime.fromtimestamp(m["internal_ms"] / 1000, tz=timezone.utc) if m.get("internal_ms") else _now()
    row = InboxMessage(message_id=m["id"], thread_id=m.get("thread_id"), received_at=received, sender_domain=sender,
                       kind=read.kind, outcome="not_application", candidates=[])
    session.add(row)
    session.flush()  # gives the row its short number
    if not read.about_application or read.kind == "other" or read.confidence < 0.6:
        return row.outcome
    outcome, cluster_id, company, candidates = match_role(session, read, sender)
    if company is None and read.company.strip():
        # Not found by sender or unique name: the identity rules may still know it (fuzzy name match).
        from app.modules.m1_companies.identity import find_by_name

        company = find_by_name(session, read.company.strip(), domain=None)
        if company is not None:
            outcome, cluster_id, candidates = match_in_company(session, read, company)
    if outcome in ("matched", "ambiguous") or not _is_new(received):
        return _settle(session, row, m, read, outcome, cluster_id, company, candidates)  # past apps end here
    if company is None:                                # a company the agent doesn't know at all
        if not (read.job_title or read.kind in ("received", "interview", "offer")):
            return _settle(session, row, m, read, outcome, None, None, [])
        company = company_for_outside(session, read, sender)
        if company is None:
            return _settle(session, row, m, read, outcome, None, None, [])
        # intake may have resolved it to a company the agent already has: match its roles first
        outcome, cluster_id, candidates = match_in_company(session, read, company)
        if outcome in ("matched", "ambiguous"):
            return _settle(session, row, m, read, outcome, cluster_id, company, candidates)
    row.company_id = company.id
    if read.job_title or read.kind == "received":
        _apply_to(session, row, _outside_target(session, company, read.job_title), read.kind, outside=True,
                  summary=read.summary, company=company)
        row.outcome = "matched"
    elif read.kind in ("interview", "offer"):          # too important to file quietly
        row.outcome = "ambiguous"
        _ask(session, row, company, m["subject"], read.kind)
    else:
        row.outcome = outcome
    return row.outcome


def _settle(session: Session, row: InboxMessage, m: dict, read: AtsRead, outcome: str, cluster_id: int | None,
            company: Company | None, candidates: list[int]) -> str:
    row.outcome, row.candidates = outcome, candidates
    row.company_id = company.id if company else None
    if outcome == "matched":
        _apply_to(session, row, cluster_id, read.kind, outside=False, summary=read.summary, company=company)
    elif outcome == "ambiguous":
        _ask(session, row, company, m["subject"], read.kind)
    return outcome


def _outside_target(session: Session, company: Company, title: str | None) -> int:
    """An outside role for this title. If an earlier receipt was filed as "(role not named)" at this company
    and is still live, this is very likely the same application: name it instead of starting a second one."""
    if title and title.strip():
        for app in _live_apps(session, company):
            cl = session.get(JobCluster, app.cluster_id)
            if app.outside and cl is not None and cl.title == NOT_NAMED:
                fingerprint = (OUTSIDE_PREFIX + " ".join(sorted(_tokens(title))))[:300]
                taken = session.scalar(select(JobCluster.id).where(JobCluster.company_id == company.id,
                                                                   JobCluster.fingerprint == fingerprint))
                if taken is None:
                    cl.title, cl.fingerprint = title.strip()[:500], fingerprint
                    return cl.id
    return outside_role(session, company, title)


class AssignError(ValueError):
    pass


def assign(session: Session, no: int, *, cluster_id: int | None = None, other_title: str | None = None,
           ignore: bool = False) -> str:
    """Your answer to "which role is this email about?": a role, another job (tracked as an outside
    application), or ignore. Applies the email's news (received / interview / rejection…) to it."""
    row = session.scalar(select(InboxMessage).where(InboxMessage.no == no))
    if row is None:
        raise AssignError(f"no email number {no}")
    if row.outcome != "ambiguous":
        raise AssignError(f"email {no} doesn't need an answer (it's {row.outcome})")
    if ignore:
        row.outcome = "ignored"
    else:
        if cluster_id is None and not other_title:
            raise AssignError("say which role (a number), --other \"Job title\", or --ignore")
        if cluster_id is not None and session.get(JobCluster, cluster_id) is None:
            raise AssignError(f"no role {cluster_id}")
        if cluster_id is not None:
            company = session.get(Company, session.get(JobCluster, cluster_id).company_id)
        else:
            company = session.get(Company, row.company_id) if row.company_id else None
            if company is None:
                raise AssignError("can't tell the company for this email; pick a role number instead")
        target = cluster_id if cluster_id is not None else outside_role(session, company, other_title)
        _apply_to(session, row, target, row.kind or "received", outside=cluster_id is None, summary="you said which role",
                  company=company)
        row.outcome = "assigned"
    from app.models.notification import Notification

    for n in session.scalars(select(Notification).where(Notification.subject_type == "inbox",
                                                        Notification.subject_id == str(no),
                                                        Notification.read_at.is_(None))).all():
        n.read_at = _now()
    return row.outcome


def scan_inbox(*, searcher=gmail.search, meta=gmail.message_meta, text=gmail.message_text, llm=None) -> dict:
    """The narrow search (hiring-system senders + tracked companies, last N days), each email once."""
    cfg = get_config().apply
    counts: Counter = Counter()
    with session_scope() as s:
        acct = gmail.account(s)
        if acct is None or acct.status != "ok":
            return {"skipped": "Gmail not connected"}
        if llm is None:
            if not LLM.available():
                return {"skipped": "needs ANTHROPIC_API_KEY"}
            llm = LLM()
        ours = set(s.scalars(select(OutreachEmail.gmail_thread_id).where(OutreachEmail.gmail_thread_id.is_not(None))).all())
        hits = searcher(s, build_query(s), cfg.scan_per_run * 3)
        seen = set(s.scalars(select(InboxMessage.message_id).where(
            InboxMessage.message_id.in_([h["id"] for h in hits]))).all()) if hits else set()
        todo = [h for h in hits if h["id"] not in seen and h["thread_id"] not in ours][-cfg.scan_per_run:]  # oldest batch
    for h in reversed(todo):  # oldest first: "received" before "interview" when both are new
        try:
            with session_scope() as s:
                m = meta(s, h["id"])
                body = ""
                try:
                    body = text(s, h["id"])
                except Exception:
                    pass  # the snippet will do
                outcome = handle(s, m, classify(s, llm, m, body))
                counts[outcome] += 1
        except Exception as e:
            counts["errors"] += 1
            log.warning("inbox message %s: %r", h["id"], e)
    return dict(counts)

