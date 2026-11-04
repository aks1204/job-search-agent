"""Part 6e: you correct how the agent read a reply (from the Audit log). The email's reply is re-sorted, the
outreach follows what a reply like that means, and your correction is kept in Memory as a precedent.

What follows, by the right reading:
- a person answered (interested, forwarded it on, a question, a no): every unsent email to that company is
  cancelled, as the agent does for any human answer — nobody is emailed again after they've answered;
- it wasn't a person's answer after all: the reply no longer counts as one. Emails already cancelled stay
  cancelled (they can't be sent "late"); to write again, prepare new drafts from the role's page.
"""

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import audit
from app.core.memory import remember
from app.models import AuditLog, Company, OutreachCampaign, OutreachEmail
from app.modules.m3_outreach.campaigns import ACTIVE, stop_company
from app.modules.m3_outreach.replies import HUMAN_ENGAGED, OUTCOME

CATEGORIES = {"interested": "Interested", "forwarded": "Forwarded it on", "question": "A question",
              "rejection": "A no", "ooo": "Out of office", "redirect": "Not the right person",
              "auto_other": "Another automatic message", "unsure": "Unclear"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def target(session: Session, entry: AuditLog) -> tuple[OutreachCampaign, OutreachEmail, Company]:
    if not (entry.action.startswith("reply_") and entry.is_judgment) or entry.action == "reply_resorted":
        raise ValueError("this entry isn't a reply the agent read")
    if entry.overridden_at is not None:
        raise ValueError("you've already corrected this one")
    c = session.get(OutreachCampaign, int(entry.subject_id)) if (entry.subject_id or "").isdigit() else None
    thread = (entry.detail or {}).get("thread_id")
    e = session.scalars(select(OutreachEmail).where(OutreachEmail.campaign_id == c.id,
                                                    OutreachEmail.gmail_thread_id == thread)
                        .order_by(OutreachEmail.id).limit(1)).first() if c is not None and thread else None
    if e is None:
        raise ValueError("the email this reply answered is gone")
    return c, e, session.get(Company, c.company_id)


def effects(session: Session, entry: AuditLog, new: str) -> list[str]:
    """What re-sorting will do, for the Confirm step."""
    c, e, company = target(session, entry)
    old = (entry.detail or {}).get("category")
    lines = [f"The reply from {e.reply_from or 'them'} is re-sorted: "
             f"{CATEGORIES.get(old, old)} → {CATEGORIES[new]}."]
    if new in HUMAN_ENGAGED:
        queued = sum(1 for x in session.scalars(select(OutreachEmail).join(OutreachCampaign).where(
            OutreachCampaign.company_id == company.id, OutreachCampaign.status.in_(ACTIVE),
            OutreachEmail.status == "queued")).all())
        lines.append(f"A person answered, so every unsent email to {company.name} is cancelled"
                     + (f" ({queued} now)." if queued else " (none are waiting now)."))
    elif old in HUMAN_ENGAGED:
        lines.append("It no longer counts as an answer. Emails already cancelled stay cancelled; "
                     "to write again, prepare new drafts from the role's page.")
    lines.append("Your correction is kept in Memory as an example of how to read replies like this.")
    return lines


def resort(session: Session, entry: AuditLog, new: str) -> str:
    if new not in CATEGORIES:
        raise ValueError("pick what the reply really was")
    c, e, company = target(session, entry)
    old = (entry.detail or {}).get("category")
    if new == old:
        raise ValueError(f"it's already sorted as {CATEGORIES[new].lower()}")
    now = _now()
    e.reply_category = new
    cancelled = 0
    if new in HUMAN_ENGAGED:
        e.replied_at = e.replied_at or entry.created_at or now
        c.outcome, c.outcome_at = OUTCOME[new], now
        cancelled = stop_company(session, company.id, f"you re-sorted a reply as {CATEGORIES[new].lower()}")
    elif old in HUMAN_ENGAGED:                     # not a person's answer after all
        e.replied_at = None
        if new in ("ooo", "auto_other"):
            e.auto_reply_at = e.auto_reply_at or entry.created_at or now
        if c.outcome == OUTCOME.get(old):
            c.outcome, c.outcome_at = None, None
    precedent = remember(
        session, "precedent",
        f"A reply to outreach at {company.name} that the agent read as {CATEGORIES.get(old, old)} was really "
        f"{CATEGORIES[new]}: {e.reply_summary or ''}".strip(),
        {"category": new, "machine_said": old, "summary": e.reply_summary},
        context={"module": "m3", "kind": "reply", "company_id": company.id},
        provenance={"source": "owner_override", "audit_id": entry.id}, evergreen=True)
    entry.overridden_at, entry.override_memory_id = now, precedent.id
    audit.record(session, "m3", "reply_resorted",
                 f"You re-sorted a reply from {e.reply_from or 'them'} at {company.name}: "
                 f"{CATEGORIES.get(old, old)} → {CATEGORIES[new]}", subject_type="campaign", subject_id=str(c.id),
                 detail={"from": old, "to": new, "corrects": entry.id, "precedent_id": precedent.id,
                         "cancelled": cancelled})
    return (f"Re-sorted as {CATEGORIES[new].lower()}."
            + (f" {cancelled} unsent email(s) to {company.name} cancelled." if cancelled else "")
            + " Kept in Memory.")
