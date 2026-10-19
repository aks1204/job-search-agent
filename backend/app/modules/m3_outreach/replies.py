"""Threads + replies (spec §3.4). Only threads the agent started are read.

Each new message in such a thread is sorted (cheap AI read, confidence-gated):
  interested / forwarded (to the team) / question / rejection → cancel every unsent email to that company,
      even approved ones (kill-switch), and ping you with the Gmail link
  ooo (out-of-office)    → nothing cancelled; that person is skipped until they're back
  redirect ("not me")    → nothing cancelled; that person is marked wrong-target; the person they name joins
      the bench at the front and gets an email in the next wave — after you approve it
  auto_other             → logged only (ticket receipts, no-reply notices)
  unsure                 → the company is paused and you see the raw email, with no suggested action

The agent NEVER writes replies and NEVER sends follow-ups: a human reply is your conversation. When the
waves end with no human answer, the campaign is stamped "silent" and you're told once.
"""

import logging
import re
from datetime import date, datetime, time, timedelta, timezone
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config_loader import get_config
from app.core import audit
from app.core.http import PoliteClient
from app.core.llm import LLM
from app.core.notify import notify
from app.models.company import Company
from app.models.contact import Bench, BenchMember, Contact, ContactEmail, RoleChannel
from app.models.outreach import OutreachCampaign, OutreachEmail
from app.modules.m3_outreach import gmail
from app.modules.m3_outreach.addresses import best_address, find_address
from app.modules.m3_outreach.bench import company_class
from app.modules.m3_outreach.campaigns import emails, stop_company
from app.modules.m3_outreach.names import name_key, split_name
from app.modules.m3_outreach.verify import verify_candidates

log = logging.getLogger(__name__)

HUMAN_ENGAGED = ("interested", "forwarded", "question", "rejection")
OUTCOME = {"interested": "interested", "forwarded": "forwarded", "question": "question", "rejection": "rejected"}


class ReplyRead(BaseModel):
    category: Literal["interested", "forwarded", "question", "rejection", "ooo", "redirect", "auto_other", "unsure"]
    confidence: float = Field(description="0 to 1: how sure you are of the category.")
    summary: str = Field(description="What the reply says, in at most 20 words.")
    back_on: str = Field(description="Out-of-office: the return date as YYYY-MM-DD if stated, else ''.")
    referred_name: str = Field(description="Redirect, or an out-of-office naming a stand-in ('contact X in my absence'): that person's full name, else ''.")
    referred_email: str = Field(description="That person's email address if given (redirect or out-of-office stand-in), else ''.")
    referred_title: str = Field(description="That person's job title if given, else ''.")


SYSTEM = """You sort one reply to a job seeker's cold email about a job opening. Categories:
- interested: wants to talk, asks for a call/time, invites them to apply or interview, or is moving it forward
- forwarded: says they've passed the email to the hiring team / hiring manager (engaged, not a "not me")
- question: asks the candidate something (availability, notice period, salary, experience) without a yes or no
- rejection: not a fit, role filled or closed, not hiring, or asks not to be contacted
- ooo: an automatic out-of-office / vacation / leave reply (if it names someone to contact meanwhile, give
  their name, email and title in the referred_ fields)
- redirect: says they are not the right person and/or points to someone else, without having forwarded it
- auto_other: any other automatic message (ticket received, no-reply notice, mailbox full)
- unsure: none of these fits clearly
Read only the newest message (quoted earlier emails are not part of it). Answer in the requested JSON format."""

_QUOTE_START = re.compile(r"^(on .{5,200} wrote:|-{2,}\s*original message\s*-{2,}|from:\s.+|sent from my \w+)", re.I)


def strip_quoted(text: str) -> str:
    out = []
    for line in (text or "").replace("\r\n", "\n").split("\n"):
        if _QUOTE_START.match(line.strip()):
            break
        if not line.lstrip().startswith(">"):
            out.append(line)
    return "\n".join(out).strip()


def classify(session: Session, llm, text: str, our_subject: str, campaign_id: int) -> ReplyRead:
    if llm is None:
        return ReplyRead(category="unsure", confidence=0, summary=text[:120], back_on="", referred_name="",
                         referred_email="", referred_title="")
    read = llm.structured("fast", system=SYSTEM, user=f"Our email's subject: {our_subject}\n\n--- the reply ---\n{text[:4000]}",
                          schema=ReplyRead, purpose="m3.read_reply", session=session,
                          subject=("campaign", str(campaign_id))).parsed
    if read.confidence < get_config().outreach.replies.min_confidence:
        read = read.model_copy(update={"category": "unsure"})
    return read


def default_classifier(session: Session, e: OutreachEmail, m: dict) -> ReplyRead:
    text = ""
    try:
        text = strip_quoted(gmail.message_text(session, m["id"]))
    except Exception as err:  # fall back to Gmail's snippet
        log.info("couldn't read reply %s: %s", m["id"], err)
    llm = LLM() if LLM.available() else None
    try:
        return classify(session, llm, text or m.get("snippet", ""), e.sent_subject or m.get("subject", ""), e.campaign_id)
    except Exception as err:  # an AI hiccup must never be read as "keep sending": treat as unsure
        log.warning("reply classification failed: %r", err)
        return classify(session, None, text or m.get("snippet", ""), "", e.campaign_id)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _who(m: dict) -> str:
    return re.sub(r"\s*<[^>]+>", "", m.get("from", "")).strip() or m.get("from", "someone")


def handle_reply(session: Session, e: OutreachEmail, m: dict, read: ReplyRead) -> str:
    """Act on one sorted reply. Returns the category acted on."""
    now = _now()
    c = session.get(OutreachCampaign, e.campaign_id)
    company = session.get(Company, c.company_id)
    contact = session.get(Contact, e.contact_id) if e.contact_id else None
    addr = session.get(ContactEmail, e.address_id) if e.address_id else None
    link = gmail.thread_link(e.gmail_thread_id)
    cat, who = read.category, _who(m)
    e.reply_from, e.reply_summary = m.get("from", "")[:320], read.summary[:500]
    if e.reply_category in HUMAN_ENGAGED and cat in ("ooo", "auto_other"):
        pass  # keep the human answer as this email's category
    else:
        e.reply_category = cat
    if addr is not None:  # any answer from that mailbox proves the address
        addr.confidence = "proven"
        if cat not in ("ooo", "auto_other"):
            addr.replied_count += 1

    if cat in HUMAN_ENGAGED:
        e.replied_at = e.replied_at or now
        n = stop_company(session, c.company_id, f"{who} replied ({cat})")
        c.outcome, c.outcome_at = OUTCOME[cat], now
        notify(session, "ping", "happened", f"{company.name}: {who} replied — {cat}",
               detail=f"{read.summary} {f'({n} unsent email(s) to {company.name} cancelled.)' if n else ''}".strip(),
               link=link, subject=("campaign", str(c.id)))
    elif cat == "ooo":
        e.auto_reply_at = now
        back = _parse_date(read.back_on) or (date.today() + timedelta(days=7))
        if contact is not None:
            contact.away_until = datetime.combine(back + timedelta(days=1), time(9), tzinfo=timezone.utc)
        if read.referred_name or read.referred_email:
            # "Contact X in my absence": X is suggested for this campaign only (often an assistant or a
            # colleague covering urgent matters), never auto-sent and not added to the bench.
            added, how = _add_referred(session, c, read, to_bench=False)
            stand_in = read.referred_name or read.referred_email
            detail = f"{who} is away until {back:%d %b} and named {stand_in} to contact meanwhile. "
            detail += (f"An email to them is waiting for your approval (review {c.id}; {how}) — "
                       "skip it if they're not involved in hiring." if added is not None
                       else f"No email prepared: {how}. Add an address with `add-contact` if you want to write to them.")
            notify(session, "needs_you", "needs_you", f"{company.name}: out-of-office names a stand-in",
                   detail=detail, link=link, subject=("campaign", str(c.id)))
    elif cat == "redirect":
        e.replied_at = e.replied_at or now
        if contact is not None:
            contact.status, contact.status_reason = "wrong_person", f"said not the right person: {read.summary[:200]}"
        added, how = (_add_referred(session, c, read) if (read.referred_name or read.referred_email)
                      else (None, ""))
        detail = read.summary + (f" They named {read.referred_name or read.referred_email}." if how else "")
        if added is not None:
            detail += f" An email to them is waiting for your approval (review {c.id}; {how})."
        elif how:
            detail += f" No email prepared: {how}. Add an address with `add-contact` if you have one."
        notify(session, "ping", "needs_you" if added is not None else "happened",
               f"{company.name}: {who} says they're not the right person", detail=detail, link=link,
               subject=("campaign", str(c.id)))
    elif cat == "unsure":
        e.replied_at = e.replied_at or now
        held = stop_company(session, c.company_id, f"{who} replied; couldn't tell what it means — your call",
                            pause=True)
        notify(session, "ping", "needs_you", f"{company.name}: a reply from {who} needs your eyes",
               detail=f"“{(m.get('snippet') or read.summary)[:300]}” — {held} unsent email(s) held. "
                      f"Then: outreach release|stop {company.domain}", link=link, subject=("campaign", str(c.id)))
    audit.record(session, "m3", f"reply_{cat}", f"{company.name}: reply from {who} sorted as {cat} "
                 f"({read.confidence:.0%}): {read.summary}", subject_type="campaign", subject_id=str(c.id),
                 is_judgment=True, detail={"from": m.get("from"), "category": cat, "confidence": read.confidence,
                                           "thread_id": e.gmail_thread_id, "message_id": m["id"]})
    return cat


def _parse_date(s: str) -> date | None:
    try:
        return date.fromisoformat(s.strip()) if s and s.strip() else None
    except ValueError:
        return None


HOW_FOUND = {"published": "given in the reply", "manual": "added by you", "hunter": "found via Hunter",
             "apollo": "found via Apollo", "constructed": "built from the company's address pattern"}


def _new_contact(session: Session, c: OutreachCampaign, read: ReplyRead, parts, to_bench: bool) -> Contact:
    contact = Contact(company_id=c.company_id, full_name=read.referred_name, name_key=name_key(read.referred_name),
                      first_name=parts[0], last_name=parts[1], title=read.referred_title or None,
                      kind="recruiting" if to_bench else "other", source="referral")  # stand-ins: not for benches
    session.add(contact)
    session.flush()
    return contact


def _add_referred(session: Session, c: OutreachCampaign, read: ReplyRead, *,
                  to_bench: bool = True) -> tuple[OutreachEmail | None, str]:
    """The named person gets a pending email in the next wave (your approval needed); a redirect's right
    person also joins the bench at the front. With only a name, the address is looked up like any contact's
    (Hunter → the company's pattern, checked with the mail server). Returns (email or None, how/why note)."""
    email = (read.referred_email or "").strip().lower()
    parts = split_name(read.referred_name) if read.referred_name else None
    company = session.get(Company, c.company_id)
    contact, existing = None, None
    if email and "@" in email:
        existing = session.scalar(select(ContactEmail).where(ContactEmail.company_id == c.company_id,
                                                             ContactEmail.email == email))
        contact = session.get(Contact, existing.contact_id) if existing and existing.contact_id else None
        if contact is None and parts:
            contact = _new_contact(session, c, read, parts, to_bench)
        if existing is None:
            existing = ContactEmail(company_id=c.company_id, contact_id=contact.id if contact else None, email=email,
                                    source="published", checks={"referred": True})
            session.add(existing)
            session.flush()
            with PoliteClient() as http:
                verify_candidates(session, http, [existing])
    elif parts:
        same = session.scalars(select(Contact).where(Contact.company_id == c.company_id,
                                                     Contact.name_key == name_key(read.referred_name),
                                                     Contact.status != "departed")).all()
        if len(same) > 1:  # can't tell which one they meant: never guess (spec: identity is never the name alone)
            return None, f"there are {len(same)} people called {read.referred_name} at {company.name}, so no address was guessed"
        contact = same[0] if same else _new_contact(session, c, read, parts, to_bench)
        existing = best_address(session, contact)
        if existing is None or existing.confidence == "invalid":
            with PoliteClient() as http:
                existing = find_address(session, http, contact, company, allow_enrich=True,
                                        startup=company_class(session, company) == "startup")
        if existing is None or existing.confidence == "invalid":
            return None, f"no working address could be found for {read.referred_name}"
        email = existing.email
    else:
        return None, "no name or address was given"
    if existing.confidence == "invalid":
        return None, f"the mail server says {email} doesn't exist"
    if any(x.to_email == email for x in emails(session, c)):
        return None, f"{email} is already in this campaign"
    channel = session.get(RoleChannel, c.cluster_id)
    if to_bench and contact is not None and channel is not None and channel.bench_id:
        bench = session.get(Bench, channel.bench_id)
        if session.scalar(select(BenchMember).where(BenchMember.bench_id == bench.id,
                                                    BenchMember.contact_id == contact.id)) is None:
            for m in session.scalars(select(BenchMember).where(BenchMember.bench_id == bench.id)).all():
                m.rank += 1
            session.add(BenchMember(bench_id=bench.id, contact_id=contact.id, slot_kind="recruiting", rank=1,
                                    co_suspects=[]))
    wave = min(((_now() - c.armed_at).days if c.armed_at else 0) + 1, 2)
    # Front of the next wave: take the first queued slot at or after that wave; everyone after moves down one.
    later = [x for x in emails(session, c) if x.status == "queued" and x.wave >= wave]
    position = min((x.position for x in later), default=None)
    if position is None:
        position = (session.scalar(select(func.max(OutreachEmail.position)).where(OutreachEmail.campaign_id == c.id)) or 0) + 1
    else:
        for x in emails(session, c):
            if x.position >= position:
                x.position += 1
    new = OutreachEmail(campaign_id=c.id, position=position, wave=wave, contact_id=contact.id if contact else None,
                        address_id=existing.id, to_email=email, to_name=contact.full_name if contact else None,
                        confidence=existing.confidence, vouched=existing.vouched, decision="pending", status="queued")
    session.add(new)
    if c.status == "done":  # the waves had finished: reopen so the new email can go once approved
        c.status, c.ended_at = "running", None
    session.flush()
    how = HOW_FOUND.get(existing.source, existing.source)
    sure = {"proven": "proven", "verified": "confirmed by the mail server"}.get(existing.confidence,
                                                                            "NOT confirmed (needs --allow-low)")
    return new, f"address {email}: {how}, {sure}"


def stamp_silence(session: Session, now: datetime | None = None) -> int:
    """Waves done, nobody answered for `silent_after_days` → 'silent' (for the reopening matrix), told once."""
    now = now or _now()
    days = get_config().outreach.replies.silent_after_days
    n = 0
    for c in session.scalars(select(OutreachCampaign).where(OutreachCampaign.status == "done",
                                                            OutreachCampaign.outcome.is_(None))).all():
        sent = [e for e in emails(session, c) if e.sent_at is not None]
        if not sent or any(e.replied_at for e in sent):
            continue
        if max(e.sent_at for e in sent) > now - timedelta(days=days):
            continue
        c.outcome, c.outcome_at = "silent", now
        company = session.get(Company, c.company_id)
        notify(session, "update", "happened", f"{company.name}: no responses",
               detail=f"{len(sent)} email(s) sent, nobody answered in {days} days — follow up yourself if you'd like.",
               subject=("campaign", str(c.id)))
        n += 1
    return n
