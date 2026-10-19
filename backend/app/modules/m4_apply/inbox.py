"""Module 4: watch for proof and status (owner decision 2026-10-19: a narrow inbox search).

Only emails FROM hiring systems (config/apply.yaml `ats_senders`) or from companies you're tracking, from the
last `lookback_days`, are looked at — each once. Threads the agent started are left to Part 3d. Nothing from
the email is stored except facts: which role, what happened, when, and a link to it in Gmail.

Each email gets a cheap AI read (is it about an application? received / in review / assessment / interview /
offer / rejection; which company, job title, job ID), then deterministic matching to a role:
job ID → title within the company → the company's only open application. Unclear → you're asked.
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
from app.models.job import JobPosting
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


def match_role(session: Session, read: AtsRead, sender_domain: str | None) -> tuple[str, int | None, Company | None]:
    """('matched', cluster_id, company) | ('ambiguous', None, company) | ('unmatched', None, None)."""
    company = _company_for(session, read, sender_domain)
    if company is None:
        return "unmatched", None, None
    jobs = session.scalars(select(JobPosting).where(JobPosting.company_id == company.id,
                                                    JobPosting.cluster_id.is_not(None))).all()
    if read.job_id:
        want = read.job_id.strip().upper()
        hits = {j.cluster_id for j in jobs if req_id(j).upper() == want or want in (j.external_id or "").upper()}
        if len(hits) == 1:
            return "matched", hits.pop(), company
    if read.job_title:
        want = _tokens(read.job_title)
        scored: dict[int, float] = {}
        for j in jobs:
            have = _tokens(j.title)
            if want and have:
                scored[j.cluster_id] = max(scored.get(j.cluster_id, 0), len(want & have) / len(want))
        ranked = sorted(scored.items(), key=lambda x: -x[1])
        if ranked and ranked[0][1] >= 0.6 and (len(ranked) == 1 or ranked[0][1] > ranked[1][1]):
            return "matched", ranked[0][0], company
    open_apps = session.scalars(select(Application).where(Application.company_id == company.id,
                                                          Application.status.not_in(("skipped", "closed", "rejected", "offer")))).all()
    if len(open_apps) == 1:
        return "matched", open_apps[0].cluster_id, company
    return "ambiguous", None, company


def classify(session: Session, llm, m: dict, text: str) -> AtsRead:
    user = f"From: {m['from']}\nSubject: {m['subject']}\n\n{(text or m.get('snippet', ''))[:3500]}"
    return llm.structured("fast", system=SYSTEM, user=user, schema=AtsRead, purpose="m4.read_ats_email",
                          session=session, subject=("gmail", m["id"])).parsed


def handle(session: Session, m: dict, read: AtsRead) -> str:
    """Apply one sorted email to the matching application. Returns the outcome recorded."""
    sender = normalize_domain(parseaddr(m["from"])[1]) or None
    received = datetime.fromtimestamp(m["internal_ms"] / 1000, tz=timezone.utc) if m.get("internal_ms") else _now()
    row = InboxMessage(message_id=m["id"], received_at=received, sender_domain=sender, kind=read.kind,
                       outcome="not_application")
    session.add(row)
    if not read.about_application or read.kind == "other" or read.confidence < 0.6:
        return row.outcome
    outcome, cluster_id, company = match_role(session, read, sender)
    row.outcome = outcome
    link = gmail.thread_link(m["thread_id"]) if m.get("thread_id") else None
    if outcome != "matched":
        if outcome == "ambiguous":
            notify(session, "needs_you", "needs_you", f"{company.name}: which role is this email about?",
                   detail=f"“{m['subject'][:120]}” ({read.kind}). Mark it with: applied <role id>", link=link)
        return outcome
    app = get_or_create(session, cluster_id, on_list=False)  # tracked even if it wasn't on your list
    row.application_id = app.id
    status = KIND_TO_STATUS[read.kind]
    if set_status(session, app, status, source="email", at=received, summary=read.summary, link=link,
                  message_id=m["id"]):
        if read.kind == "received" and not app.receipt_link:
            app.receipt_link = link
        if status in ("interviewing", "offer", "rejected"):
            notify(session, "update", "happened", f"{company.name}: {status.replace('_', ' ')}",
                   detail=read.summary, link=link, subject=("job_cluster", str(cluster_id)))
    return outcome


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

