"""Campaigns and the approval flow (spec §3.3, owner-specified).

For each drafted role a campaign is prepared: every recipient's final email (recipient, subject, body,
resume) → you step through them → Approve / Edit / Skip each → once all are reviewed, the wave
scheduler starts. Human gate is permanent: there is no auto-send mode.

Waves: day 0 = the first two people, day 1 = the next two, day 2 = the rest — each wave only while
nobody at the company has replied. A designated address (or hiring mailbox) is a one-email campaign.
Addresses that are only low-confidence (catch-all domains, unverifiable) need your explicit go-ahead
unless a lookup provider vouches for them. Your edits are kept as style examples for future drafts.
"""

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config_loader import get_config
from app.core import audit
from app.models.contact import Bench, ContactEmail, RoleChannel
from app.models.draft import OutreachDraft
from app.models.outreach import DraftEdit, OutreachCampaign, OutreachEmail
from app.modules.m3_outreach.bench import usable_members
from app.modules.m3_outreach.drafting import greeting_name
from app.modules.m3_outreach.template import Template, fill

ACTIVE = ("pending_review", "scheduled", "running", "paused")
SENDABLE_CONFIDENCE = ("proven", "verified")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def needs_go_ahead(email: OutreachEmail) -> bool:
    return email.confidence not in SENDABLE_CONFIDENCE and not email.vouched


def wave_for(index: int) -> int:
    first, second = get_config().outreach.sending.wave_sizes
    return 0 if index < first else 1 if index < first + second else 2


def render_email(t: Template, campaign: OutreachCampaign, email: OutreachEmail) -> tuple[str, str]:
    """(subject, body) exactly as this recipient gets it: only the greeting differs."""
    first = greeting_name(email.to_name)
    greeting = fill(t.settings.greeting, {"first_name": first}) if first else t.settings.greeting_without_name
    return campaign.subject, campaign.body.replace("{greeting}", greeting)


def _recipients(session: Session, channel: RoleChannel) -> list[dict]:
    if channel.mode in ("designated", "mailbox") and channel.designated_email:
        row = session.scalar(select(ContactEmail).where(ContactEmail.company_id == channel.company_id,
                                                        ContactEmail.email == channel.designated_email))
        conf = row.confidence if row else "unchecked"
        # A designated address is the company's own instruction: it counts as published, but a
        # bounce-proof check still matters, so unverified ones need your go-ahead like any other.
        return [{"contact_id": None, "address_id": row.id if row else None, "to_email": channel.designated_email,
                 "to_name": None, "confidence": conf, "vouched": bool(row and row.vouched)}]
    if channel.mode == "bench" and channel.bench_id:
        return [{"contact_id": c.id, "address_id": a.id, "to_email": a.email, "to_name": c.full_name,
                 "confidence": a.confidence, "vouched": a.vouched}
                for _, c, a in usable_members(session, session.get(Bench, channel.bench_id))]
    return []


def prepare_campaigns(session: Session, cluster_ids: list[int] | None = None) -> dict:
    """New drafts → campaigns waiting for your review. Refreshes a not-yet-reviewed campaign when its
    draft is rewritten."""
    counts = {"prepared": 0, "refreshed": 0}
    q = select(OutreachDraft)
    if cluster_ids:
        q = q.where(OutreachDraft.cluster_id.in_(cluster_ids))
    for draft in session.scalars(q).all():
        channel = session.get(RoleChannel, draft.cluster_id)
        if channel is None or channel.mode not in ("designated", "bench", "mailbox"):
            continue
        campaigns = session.scalars(select(OutreachCampaign).where(OutreachCampaign.cluster_id == draft.cluster_id)
                                    .order_by(OutreachCampaign.id.desc())).all()
        current = campaigns[0] if campaigns else None
        if current and current.status == "pending_review" and draft.version > current.draft_version \
                and (not current.edited or get_config().outreach.drafts.redraft_edited_drafts):
            _load_draft(current, draft)
            current.edited = False
            for e in session.scalars(select(OutreachEmail).where(OutreachEmail.campaign_id == current.id)).all():
                e.decision, e.allow_low = "pending", False   # you approved the old text, not this one
            counts["refreshed"] += 1
            continue
        if current is not None:
            continue  # one campaign per role; a later reopening scenario starts a new one (Part 3d / Module 4)
        recipients = _recipients(session, channel)
        if not recipients:
            continue
        campaign = OutreachCampaign(cluster_id=draft.cluster_id, company_id=channel.company_id, draft_id=draft.id,
                                    draft_version=draft.version, subject=draft.subject, body=draft.body,
                                    attach_resume=draft.attach_resume, status="pending_review")
        session.add(campaign)
        session.flush()
        for i, r in enumerate(recipients):
            session.add(OutreachEmail(campaign_id=campaign.id, position=i + 1, wave=wave_for(i), decision="pending",
                                      status="queued", **r))
        counts["prepared"] += 1
        audit.record(session, "m3", "campaign_prepared",
                     f"Outreach for role {draft.cluster_id} is ready for your review ({len(recipients)} email(s))",
                     subject_type="job_cluster", subject_id=str(draft.cluster_id))
    session.flush()
    return counts


def _load_draft(campaign: OutreachCampaign, draft: OutreachDraft) -> None:
    campaign.subject, campaign.body = draft.subject, draft.body
    campaign.draft_id, campaign.draft_version, campaign.attach_resume = draft.id, draft.version, draft.attach_resume


def emails(session: Session, campaign: OutreachCampaign) -> list[OutreachEmail]:
    return session.scalars(select(OutreachEmail).where(OutreachEmail.campaign_id == campaign.id)
                           .order_by(OutreachEmail.position)).all()


class ReviewError(ValueError):
    pass


def _editable(campaign: OutreachCampaign) -> None:
    if campaign.status != "pending_review":
        raise ReviewError(f"campaign {campaign.id} is {campaign.status}; only campaigns waiting for review can change")


def decide(session: Session, campaign: OutreachCampaign, email: OutreachEmail, action: str, *,
           allow_low: bool = False) -> None:
    """Approve or skip one email. Low-confidence addresses need allow_low (your explicit go-ahead).
    In a campaign already under way, only newly added emails (e.g. the person a redirect named) can be decided."""
    late = campaign.status in ("scheduled", "running") and email.decision == "pending"
    if not late:
        _editable(campaign)
    if action == "approve":
        if email.confidence == "invalid":
            raise ReviewError(f"the mail server says {email.to_email} doesn't exist; it can't be approved")
        if needs_go_ahead(email) and not allow_low:
            raise ReviewError(f"{email.to_email} is only {email.confidence}-confidence (couldn't be proven); "
                              "approve it with --allow-low if you're sure")
        email.decision, email.allow_low = "approved", allow_low or not needs_go_ahead(email)
    elif action == "skip":
        email.decision = "skipped"
        email.status, email.status_reason = "cancelled", "skipped by you"
    else:
        raise ReviewError("action must be approve or skip")
    audit.record(session, "m3", f"email_{action}", f"You chose {action} for {email.to_email} (campaign {campaign.id})",
                 subject_type="campaign", subject_id=str(campaign.id))
    if not late:  # a late addition just joins its wave; the campaign's schedule stays as it is
        _maybe_arm(session, campaign)


def _maybe_arm(session: Session, campaign: OutreachCampaign) -> None:
    """All reviewed → the wave scheduler starts (or the campaign ends if everything was skipped)."""
    all_emails = emails(session, campaign)
    if any(e.decision == "pending" for e in all_emails):
        return
    approved = [e for e in all_emails if e.decision == "approved"]
    if not approved:
        campaign.status, campaign.status_reason, campaign.ended_at = "cancelled", "every email skipped", _now()
        return
    # Re-number waves over the approved emails only, so day 0 always has the first two approved.
    for i, e in enumerate(approved):
        e.wave = wave_for(i)
    campaign.status, campaign.armed_at = "scheduled", _now()
    audit.record(session, "m3", "campaign_armed",
                 f"Campaign {campaign.id} approved: {len(approved)} email(s) will go out in waves",
                 subject_type="campaign", subject_id=str(campaign.id))


def edit(session: Session, campaign: OutreachCampaign, subject: str, body: str) -> None:
    """Your edit replaces the shared text for every recipient. Approvals so far are reset (you approve
    the exact final text), and the before/after pair is kept as a style example for future drafts."""
    _editable(campaign)
    if "{greeting}" not in body:
        raise ReviewError("keep the {greeting} line: it becomes 'Hi <first name>,' for each person")
    subject, body = subject.strip(), body.strip("\n")
    if not subject or not body.strip():
        raise ReviewError("subject and body can't be empty")
    if (subject, body) == (campaign.subject, campaign.body):
        return
    draft = session.get(OutreachDraft, campaign.draft_id)
    session.add(DraftEdit(cluster_id=campaign.cluster_id, before_subject=draft.subject, before_body=draft.body,
                          after_subject=subject, after_body=body))
    campaign.subject, campaign.body, campaign.edited = subject, body, True
    for e in emails(session, campaign):
        if e.decision == "approved":
            e.decision = "pending"
    audit.record(session, "m3", "campaign_edited", f"You edited the email for campaign {campaign.id}",
                 subject_type="campaign", subject_id=str(campaign.id))


def export_text(campaign: OutreachCampaign) -> str:
    return (f"Subject: {campaign.subject}\n"
            "# Edit the subject line above and the email below. Keep the {greeting} line as it is.\n"
            "# Lines starting with # are ignored.\n"
            "----\n" + campaign.body + "\n")


def import_text(text: str) -> tuple[str, str]:
    lines = [ln for ln in text.replace("\r\n", "\n").split("\n") if not ln.startswith("#")]
    if not lines or not lines[0].lower().startswith("subject:") or "----" not in lines:
        raise ReviewError("the file must start with 'Subject: …' and keep the ---- line before the email")
    subject = lines[0].split(":", 1)[1].strip()
    body = "\n".join(lines[lines.index("----") + 1:]).strip("\n")
    return subject, body


def stop_company(session: Session, company_id: int, reason: str, *, pause: bool = False,
                 outcome: str | None = None) -> int:
    """Kill-switch: cancel (or, with pause, hold) every unsent email to this company, even approved ones."""
    n = 0
    for c in session.scalars(select(OutreachCampaign).where(OutreachCampaign.company_id == company_id,
                                                            OutreachCampaign.status.in_(ACTIVE))).all():
        if pause:
            c.status, c.status_reason = "paused", reason
            n += sum(1 for e in emails(session, c) if e.status == "queued")
            continue
        for e in emails(session, c):
            if e.status == "queued":
                e.status, e.status_reason = "cancelled", reason
                n += 1
        c.status, c.status_reason, c.ended_at = "cancelled", reason, _now()
        if outcome and not c.outcome:
            c.outcome, c.outcome_at = outcome, _now()
    return n


def resume_company(session: Session, company_id: int) -> int:
    n = 0
    for c in session.scalars(select(OutreachCampaign).where(OutreachCampaign.company_id == company_id,
                                                            OutreachCampaign.status == "paused")).all():
        if c.armed_at is None:
            c.status = "pending_review"
        else:
            c.status = "running" if any(e.status == "sent" for e in emails(session, c)) else "scheduled"
        c.status_reason = None
        n += 1
    return n



def redraft(session: Session, campaign: OutreachCampaign, llm) -> OutreachCampaign:
    """Your Redraft button: a fresh draft from your current resume and profile replaces the campaign's text —
    including your edits (they stay saved as style examples). Approvals are reset."""
    from app.modules.m3_outreach.drafting import write_draft
    from app.modules.m3_outreach.pipeline import pursued_roles

    _editable(campaign)
    row = next(iter(pursued_roles(session, [campaign.cluster_id])), None)
    if row is None:
        raise ReviewError("this role isn't open any more")
    draft = write_draft(session, llm, *row)
    _load_draft(campaign, draft)
    campaign.edited = False
    for e in emails(session, campaign):
        if e.decision == "approved":
            e.decision = "pending"
    audit.record(session, "m3", "campaign_redrafted", f"You asked for a fresh draft for campaign {campaign.id}",
                 subject_type="campaign", subject_id=str(campaign.id))
    return campaign
