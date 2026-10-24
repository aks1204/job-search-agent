"""Sending (spec §3.3). Only approved emails, one at a time, through an unbypassable throttle stack:

  your pause switch → bounce breaker → quiet hours (recipient's local time, best effort)
  → global cap 12/day → per-company cap 2/day → 30-60 min stagger → kill-switch (a reply holds the company)

Waves: day 0 / day 1 / day 2 after you finish reviewing, each only while nobody at the company has replied.
Every send is audited (recipient, wave, draft version, outcome).

Reply & bounce watch (only threads the app started): any human reply holds every unsent email to that
company and tells you (Part 3d will sort replies into interested / redirect / out-of-office… and act on
them as the spec says); a bounce kills that address, distrusts a guessed pattern, and feeds the breaker.
Bounce breaker: 2+ bounces, or >5% once 30+ are sent → unsent queue frozen → self-cleanup (re-check every
queued address) → self-resume after 6h when the queue is clean. A second trip within 7 days that a
conservative fix can't explain waits for you.
"""

import logging
import random
import re
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config_loader import get_config
from app.core import audit
from app.core.http import PoliteClient
from app.core.redis_client import get_redis
from app.db import session_scope
from app.models.company import STATUS_BLOCKED, Company
from app.models.contact import Contact, ContactEmail, MailDomain, RoleChannel
from app.models.outreach import OutreachCampaign, OutreachEmail, SendBreaker
from app.modules.m2_jobs.profile_text import resume_path
from app.modules.m3_outreach import gmail
from app.modules.m3_outreach.campaigns import SENDABLE_CONFIDENCE, emails, render_email
from app.modules.m3_outreach.template import load_template
from app.modules.m3_outreach.verify import verify_candidates

log = logging.getLogger(__name__)

PAUSE_KEY = "m3:sending_paused"
NEXT_SEND_KEY = "m3:next_send_at"
BOUNCE_FROM = re.compile(r"mailer-daemon|postmaster|mail delivery (subsystem|system)", re.I)
BOUNCE_SUBJECT = re.compile(r"delivery status notification|undeliver|delivery (has )?failed|returned mail|"
                            r"failure notice|could not be delivered|address not found", re.I)

# Best-effort recipient time zones from a profile's location (spec: "recipient-local best-effort").
_TZ_HINTS = [
    (r"india|bengaluru|bangalore|mumbai|delhi|gurgaon|gurugram|noida|hyderabad|pune|chennai|kolkata", "Asia/Kolkata"),
    (r"singapore", "Asia/Singapore"), (r"dubai|abu dhabi|uae", "Asia/Dubai"),
    (r"london|united kingdom|england|scotland", "Europe/London"), (r"dublin|ireland", "Europe/Dublin"),
    (r"berlin|munich|frankfurt|germany|paris|france|amsterdam|netherlands|zurich|switzerland", "Europe/Berlin"),
    (r"new york|boston|new jersey|atlanta|miami|toronto|washington|philadelphia|indianapolis|indiana", "America/New_York"),
    (r"chicago|texas|dallas|austin|houston|san antonio|minneapolis", "America/Chicago"),
    (r"denver|colorado|phoenix|arizona", "America/Denver"),
    (r"san francisco|seattle|los angeles|california|bay area|palo alto|mountain view|san jose", "America/Los_Angeles"),
    (r"sydney|melbourne|australia", "Australia/Sydney"),
]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def recipient_tz(contact: Contact | None) -> ZoneInfo:
    where = f"{contact.location or ''} {contact.geo or ''}".lower() if contact else ""
    for pattern, tz in _TZ_HINTS:
        if re.search(rf"\b({pattern})\b", where):  # whole words: "Indiana" is not "India"
            return ZoneInfo(tz)
    return ZoneInfo(get_config().app.timezone)


def in_quiet_hours(now_utc: datetime, tz: ZoneInfo) -> bool:
    q = get_config().app.quiet_hours
    t = now_utc.astimezone(tz).time()
    return (t >= q.start or t < q.end) if q.start > q.end else (q.start <= t < q.end)


def start_of_day_utc(now_utc: datetime) -> datetime:
    tz = ZoneInfo(get_config().app.timezone)
    local = now_utc.astimezone(tz)
    return datetime.combine(local.date(), time(0), tzinfo=tz).astimezone(timezone.utc)


def paused() -> bool:
    return bool(get_redis().exists(PAUSE_KEY))


def set_paused(on: bool) -> None:
    if on:
        get_redis().set(PAUSE_KEY, "1")
    else:
        get_redis().delete(PAUSE_KEY)


def breaker(session: Session) -> SendBreaker:
    b = session.get(SendBreaker, 1)
    if b is None:
        b = SendBreaker(id=1, state="ok", history=[], reset_at=_now())
        session.add(b)
        session.flush()
    return b


def wave_due_at(campaign: OutreachCampaign, wave: int) -> datetime:
    return campaign.armed_at + timedelta(days=wave)


def _sent_since(session: Session, since: datetime, company_id: int | None = None) -> int:
    q = select(func.count()).select_from(OutreachEmail).where(OutreachEmail.sent_at >= since)
    if company_id is not None:
        q = q.join(OutreachCampaign, OutreachCampaign.id == OutreachEmail.campaign_id) \
             .where(OutreachCampaign.company_id == company_id)
    return session.scalar(q) or 0


def _queue_clean(session: Session) -> bool:
    for e in session.scalars(select(OutreachEmail).where(OutreachEmail.status == "queued",
                                                         OutreachEmail.decision == "approved")).all():
        row = session.get(ContactEmail, e.address_id) if e.address_id else None
        conf = row.confidence if row else e.confidence
        if conf not in SENDABLE_CONFIDENCE and not e.vouched:
            return False
    return True


# --- sending ---------------------------------------------------------------------------------------

def next_candidates(session: Session, now: datetime) -> list[tuple[OutreachEmail, OutreachCampaign]]:
    rows = session.execute(
        select(OutreachEmail, OutreachCampaign).join(OutreachCampaign, OutreachCampaign.id == OutreachEmail.campaign_id)
        .join(Company, Company.id == OutreachCampaign.company_id)
        .where(OutreachEmail.status == "queued", OutreachEmail.decision == "approved",
               OutreachCampaign.status.in_(("scheduled", "running")),
               Company.status != STATUS_BLOCKED)    # last line of defence: never email a blocked company
        .order_by(OutreachCampaign.armed_at, OutreachEmail.wave, OutreachEmail.position)
    ).all()
    return [(e, c) for e, c in rows if wave_due_at(c, e.wave) <= now]


def _company_silent(session: Session, campaign: OutreachCampaign) -> bool:
    """Before a later wave: re-read the company's threads right now (not just the last check)."""
    sent = session.scalars(select(OutreachEmail).join(OutreachCampaign, OutreachCampaign.id == OutreachEmail.campaign_id)
                           .where(OutreachCampaign.company_id == campaign.company_id,
                                  OutreachEmail.gmail_thread_id.is_not(None))).all()
    for e in sent:
        check_thread(session, e)
    return session.get(OutreachCampaign, campaign.id).status in ("scheduled", "running")


def send_one(session: Session, e: OutreachEmail, c: OutreachCampaign, *, sender=gmail.send) -> str:
    """Send one approved email (all throttles already passed). Returns the outcome."""
    row = session.get(ContactEmail, e.address_id) if e.address_id else None
    if row is not None and row.confidence == "invalid":
        e.status, e.status_reason = "cancelled", "address found to be invalid before sending"
        return "cancelled"
    if row is not None and row.confidence not in SENDABLE_CONFIDENCE and not (e.allow_low or e.vouched):
        e.status, e.status_reason = "cancelled", "address could no longer be proven; needs your go-ahead"
        return "cancelled"
    acct = gmail.account(session)
    owner = get_config().owner
    subject, body = render_email(load_template(), c, e)
    msg = gmail.build_message(sender=acct.email, sender_name=owner.name if owner else None, to=e.to_email,
                              subject=subject, body=body, attachment=resume_path() if c.attach_resume else None)
    message_id, thread_id = sender(session, msg)
    now = _now()
    e.status, e.sent_at, e.gmail_message_id, e.gmail_thread_id = "sent", now, message_id, thread_id
    e.sent_subject, e.sent_body = subject, body
    c.status = "running"
    audit.record(session, "m3", "email_sent", f"Sent to {e.to_email} (wave {e.wave + 1}, campaign {c.id})",
                 subject_type="campaign", subject_id=str(c.id),
                 detail={"to": e.to_email, "wave": e.wave, "draft_version": c.draft_version, "edited": c.edited,
                         "message_id": message_id, "thread_id": thread_id})
    _maybe_finish(session, c)
    return "sent"


def _maybe_finish(session: Session, c: OutreachCampaign) -> None:
    if not any(x.status == "queued" for x in emails(session, c)):
        c.status, c.ended_at = "done", _now()
        c.status_reason = c.status_reason or "all waves sent"


def send_tick(*, now: datetime | None = None, sender=gmail.send) -> dict:
    """Called every few minutes. Sends at most one email per call."""
    now = now or _now()
    rules = get_config().outreach.sending
    with session_scope() as s:
        if paused():
            return {"skipped": "sending paused by you"}
        acct = gmail.account(s)
        if acct is None or acct.status != "ok":
            return {"skipped": "Gmail not connected"}
        b = breaker(s)
        if b.state == "tripped" and b.tripped_at and now - b.tripped_at >= timedelta(hours=rules.breaker_cooldown_hours) \
                and _queue_clean(s):
            b.state, b.reset_at, b.reason = "ok", now, None
            audit.record(s, "m3", "breaker_resumed", "Sending resumed by itself: queue re-checked and clean")
        if b.state != "ok":
            return {"skipped": f"bounce breaker {b.state}: {b.reason}"}
        nxt = get_redis().get(NEXT_SEND_KEY)
        if nxt and datetime.fromisoformat(nxt) > now:
            return {"skipped": f"stagger: next send after {datetime.fromisoformat(nxt):%H:%M} UTC"}
        day = start_of_day_utc(now)
        if _sent_since(s, day) >= rules.global_per_day:
            return {"skipped": f"daily cap reached ({rules.global_per_day})"}

        for e, c in next_candidates(s, now):
            if _sent_since(s, day, c.company_id) >= rules.per_company_per_day:
                continue
            contact = s.get(Contact, e.contact_id) if e.contact_id else None
            if contact is not None and contact.away_until and contact.away_until > now:
                continue  # out of office: their email waits until they're back
            if contact is not None and contact.status != "active":
                e.status, e.status_reason = "cancelled", f"contact is {contact.status}"
                continue
            if in_quiet_hours(now, recipient_tz(contact)):
                continue
            if e.wave > 0 and not _company_silent(s, c):
                continue
            try:
                outcome = send_one(s, e, c, sender=sender)
            except gmail.GmailNotConnected as err:
                return {"skipped": str(err)}
            except gmail.GmailError as err:
                log.warning("send failed for email %s: %s", e.id, err)
                get_redis().set(NEXT_SEND_KEY, (now + timedelta(minutes=15)).isoformat())
                return {"error": str(err)[:200]}
            if outcome == "sent":
                gap = random.randint(*rules.stagger_minutes)
                get_redis().set(NEXT_SEND_KEY, (now + timedelta(minutes=gap)).isoformat())
                return {"sent": e.to_email, "campaign": c.id, "next_in_minutes": gap}
        return {"nothing_due": True}


# --- replies, bounces, delivery ---------------------------------------------------------------------

def check_thread(session: Session, e: OutreachEmail, *, reader=gmail.thread, classifier=None) -> str | None:
    """Look at one thread the app started; act on every message not seen before (Part 3d sorts replies).
    Returns 'bounce' / 'reply' when something new happened."""
    from app.modules.m3_outreach import replies

    classifier = classifier or replies.default_classifier
    msgs = reader(session, e.gmail_thread_id)
    e.last_checked_at = _now()
    outcome = None
    for m in msgs:
        if m["sent_by_me"] or m["id"] == e.gmail_message_id or m["id"] in (e.handled_ids or []):
            continue
        e.handled_ids = [*(e.handled_ids or []), m["id"]]
        if BOUNCE_FROM.search(m["from"]) or BOUNCE_SUBJECT.search(m["subject"]):
            if e.bounced_at is None:
                handle_bounce(session, e, m)
                outcome = outcome or "bounce"
            continue
        replies.handle_reply(session, e, m, classifier(session, e, m))
        outcome = outcome or "reply"
    return outcome


def handle_bounce(session: Session, e: OutreachEmail, m: dict) -> None:
    now = _now()
    e.status, e.bounced_at, e.status_reason = "bounced", now, f"bounced: {m['snippet'][:200]}"
    source = None
    if e.address_id and (row := session.get(ContactEmail, e.address_id)):
        row.bounced_count += 1
        row.confidence, source = "invalid", row.source
        if row.source == "constructed":  # untrust the guessed pattern for this domain
            md = session.get(MailDomain, row.email.split("@", 1)[1])
            if md is not None:
                md.pattern_distrusted = True
    c = session.get(OutreachCampaign, e.campaign_id)
    if (ch := session.get(RoleChannel, c.cluster_id)) is not None and ch.mode == "bench":
        ch.next_retry_at = now  # hunt a replacement on the next contacts round
    audit.record(session, "m3", "email_bounced", f"{e.to_email} bounced (campaign {c.id})",
                 subject_type="campaign", subject_id=str(c.id), detail={"source": source})
    evaluate_breaker(session, source)


def evaluate_breaker(session: Session, source: str | None = None, *, http=None) -> str:
    rules = get_config().outreach.sending
    b = breaker(session)
    if b.state != "ok":
        return b.state
    sends = _sent_since(session, b.reset_at)
    bounces = session.scalar(select(func.count()).select_from(OutreachEmail)
                             .where(OutreachEmail.bounced_at >= b.reset_at)) or 0
    if bounces < rules.breaker_bounces and not (sends >= rules.breaker_min_sends and bounces / max(sends, 1) > rules.breaker_rate):
        return b.state
    now = _now()
    recent = [h for h in (b.history or []) if datetime.fromisoformat(h["at"]) > now - timedelta(days=7)]
    sources = sorted({row.source for row in session.scalars(
        select(ContactEmail).join(OutreachEmail, OutreachEmail.address_id == ContactEmail.id)
        .where(OutreachEmail.bounced_at >= b.reset_at)).all()})
    b.history = [*(b.history or []), {"at": now.isoformat(), "bounces": bounces, "sends": sends, "sources": sources}]
    b.tripped_at = now
    if recent and any(s != "constructed" for s in sources):
        # Repeat within 7 days and not explained by guessed addresses: a judgment call → yours.
        b.state, b.reason = "needs_owner", f"second bounce spike within 7 days ({bounces} bounces); check the queue, then resume"
    else:
        b.state, b.reason = "tripped", f"{bounces} bounce(s) in {sends} send(s); re-checking the queue, auto-resume after cooldown"
    audit.record(session, "m3", "breaker_tripped", f"Bounce breaker: {b.reason}", detail={"sources": sources})
    self_cleanup(session, http=http)
    return b.state


def self_cleanup(session: Session, *, http=None, **kw) -> dict:
    """Re-verify every queued address; drop the ones that fail (the rest of the app is unaffected)."""
    counts = {"rechecked": 0, "cancelled": 0}
    own_http = http is None
    http = http or PoliteClient()
    try:
        for e in session.scalars(select(OutreachEmail).where(OutreachEmail.status == "queued",
                                                             OutreachEmail.decision == "approved")).all():
            row = session.get(ContactEmail, e.address_id) if e.address_id else None
            if row is None or row.confidence == "proven":
                continue
            row.confidence = "unchecked"
            verify_candidates(session, http, [row], **kw)
            counts["rechecked"] += 1
            if row.confidence == "invalid" or (row.confidence not in SENDABLE_CONFIDENCE and not e.vouched):
                e.status, e.status_reason = "cancelled", f"held after bounces: address is now {row.confidence}"
                counts["cancelled"] += 1
    finally:
        if own_http:
            http.close()
    return counts


def mail_check(*, limit: int = 120, reader=gmail.thread) -> dict:
    """Every 15 minutes: replies and bounces on sent emails (last 30 days); delivered ⇒ address proven."""
    rules = get_config().outreach.sending
    counts = {"checked": 0, "replies": 0, "bounces": 0, "delivered": 0}
    now = _now()
    with session_scope() as s:
        acct = gmail.account(s)
        if acct is None or acct.status != "ok":
            return {"skipped": "Gmail not connected"}
        sent = s.scalars(select(OutreachEmail).where(
            OutreachEmail.status.in_(("sent",)), OutreachEmail.gmail_thread_id.is_not(None),
            OutreachEmail.sent_at >= now - timedelta(days=30))
            .order_by(OutreachEmail.last_checked_at.asc().nulls_first()).limit(limit)).all()
        for e in sent:
            try:
                outcome = check_thread(s, e, reader=reader)
            except gmail.GmailError as err:
                log.warning("thread check failed: %s", err)
                break
            counts["checked"] += 1
            if outcome:
                counts[f"{outcome}s" if outcome != "reply" else "replies"] += 1
            if e.status == "sent" and not e.delivered_counted and e.sent_at <= now - timedelta(hours=rules.delivered_after_hours):
                e.delivered_counted = True
                if e.address_id and (row := s.get(ContactEmail, e.address_id)):
                    row.delivered_count += 1
                    row.confidence = "proven"
                counts["delivered"] += 1
        from app.modules.m3_outreach.replies import stamp_silence

        counts["silent"] = stamp_silence(s, now)
    return counts
