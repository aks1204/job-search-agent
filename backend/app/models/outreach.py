"""Module 3 Part 3c: the connected mailbox, campaigns (one per role), each email in them, your edits
(style examples for future drafts) and the bounce breaker's state."""

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class MailAccount(Base):
    __tablename__ = "mail_accounts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    provider: Mapped[str] = mapped_column(String(16), default="gmail")
    email: Mapped[str] = mapped_column(String(320))
    refresh_token_enc: Mapped[str] = mapped_column(Text)            # encrypted with APP_SECRET_KEY
    scopes: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="ok")    # ok | reconnect (sign-in revoked/expired)
    connected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


# Campaign status: pending_review → scheduled (all reviewed, at least one approved) → running → done
#                  | cancelled (all skipped / you stopped it) | paused (a reply came in: your call)
class OutreachCampaign(Base):
    __tablename__ = "outreach_campaigns"
    __table_args__ = (Index("ix_campaigns_status", "status"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    cluster_id: Mapped[int] = mapped_column(ForeignKey("job_clusters.id", ondelete="CASCADE"))
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"))
    draft_id: Mapped[int] = mapped_column(ForeignKey("outreach_drafts.id", ondelete="CASCADE"))
    draft_version: Mapped[int] = mapped_column(Integer)
    subject: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text)                          # shared text; "{greeting}" per recipient
    edited: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    attach_resume: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    status: Mapped[str] = mapped_column(String(16), default="pending_review")
    status_reason: Mapped[str | None] = mapped_column(Text)
    armed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Part 3d: how it ended, for the reopening matrix and pipeline: interested | question | rejected |
    # redirected | silent | stopped
    outcome: Mapped[str | None] = mapped_column(String(12))
    outcome_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


# Email decision: pending | approved | skipped.   Status: queued → sent | cancelled | failed;  sent → bounced
class OutreachEmail(Base):
    __tablename__ = "outreach_emails"
    __table_args__ = (Index("ix_outreach_emails_campaign", "campaign_id"), Index("ix_outreach_emails_status", "status"))

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("outreach_campaigns.id", ondelete="CASCADE"))
    position: Mapped[int] = mapped_column(Integer)                   # 1, 2, … in send order
    wave: Mapped[int] = mapped_column(Integer)                       # 0 = day 0, 1 = day 1, 2 = day 2
    contact_id: Mapped[int | None] = mapped_column(ForeignKey("contacts.id", ondelete="SET NULL"))
    address_id: Mapped[int | None] = mapped_column(ForeignKey("contact_emails.id", ondelete="SET NULL"))
    to_email: Mapped[str] = mapped_column(String(320))
    to_name: Mapped[str | None] = mapped_column(String(255))
    confidence: Mapped[str] = mapped_column(String(10))              # address confidence when prepared
    vouched: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    decision: Mapped[str] = mapped_column(String(10), default="pending")
    allow_low: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))  # your go-ahead
    status: Mapped[str] = mapped_column(String(12), default="queued")
    status_reason: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    gmail_message_id: Mapped[str | None] = mapped_column(String(64))
    gmail_thread_id: Mapped[str | None] = mapped_column(String(64))
    sent_subject: Mapped[str | None] = mapped_column(Text)
    sent_body: Mapped[str | None] = mapped_column(Text)               # audit: exactly what went out
    replied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Part 3d: what the reply was (interested | question | rejection | ooo | redirect | auto_other | unsure)
    reply_category: Mapped[str | None] = mapped_column(String(12))
    reply_summary: Mapped[str | None] = mapped_column(Text)
    reply_from: Mapped[str | None] = mapped_column(String(320))
    auto_reply_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))   # out-of-office etc.
    handled_ids: Mapped[list] = mapped_column(JSONB, default=list, server_default=text("'[]'::jsonb"))  # Gmail message ids seen
    bounced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_counted: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class DraftEdit(Base):
    """What the AI wrote vs what you sent instead: shown to future drafts as style examples."""

    __tablename__ = "draft_edits"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    cluster_id: Mapped[int] = mapped_column(ForeignKey("job_clusters.id", ondelete="CASCADE"))
    before_subject: Mapped[str] = mapped_column(Text)
    before_body: Mapped[str] = mapped_column(Text)
    after_subject: Mapped[str] = mapped_column(Text)
    after_body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SendBreaker(Base):
    """Bounce breaker (one row, id 1). Counts restart from `reset_at` after each resume."""

    __tablename__ = "send_breaker"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    state: Mapped[str] = mapped_column(String(12), default="ok")     # ok | tripped | needs_owner
    reason: Mapped[str | None] = mapped_column(Text)
    tripped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reset_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    history: Mapped[list] = mapped_column(JSONB, default=list, server_default=text("'[]'::jsonb"))
