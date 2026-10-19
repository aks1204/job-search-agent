"""Module 4: applications (one per role), their timeline, and the inbox emails already looked at."""

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Identity, Index, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

# Status, in order: queued (on your apply list) → applied → confirmed (the hiring system acknowledged it)
# → in_review → interviewing → offer | rejected | silent.   Off the list without applying: skipped | closed.
STATUS_RANK = {"queued": 0, "applied": 1, "confirmed": 2, "in_review": 3, "interviewing": 4,
               "offer": 5, "rejected": 5, "silent": 5, "skipped": 5, "closed": 5}


class Application(Base):
    __tablename__ = "applications"
    __table_args__ = (Index("ix_applications_status", "status"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    cluster_id: Mapped[int] = mapped_column(ForeignKey("job_clusters.id", ondelete="CASCADE"), unique=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(String(12))
    on_list: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))  # gets reminders
    outside: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))  # not from the agent's list
    links: Mapped[list] = mapped_column(JSONB, default=list, server_default=text("'[]'::jsonb"))  # [{req_id,url,ok,why}]
    links_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    queued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    applied_how: Mapped[str | None] = mapped_column(String(12))        # email (receipt found) | you (marked)
    receipt_link: Mapped[str | None] = mapped_column(Text)              # Gmail link to the confirmation email
    status_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_heard_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class ApplicationEvent(Base):
    __tablename__ = "application_events"
    __table_args__ = (Index("ix_application_events_app", "application_id", "at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    application_id: Mapped[int] = mapped_column(ForeignKey("applications.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(12))        # a status name, or "note"
    source: Mapped[str] = mapped_column(String(8))       # email | you | agent
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    summary: Mapped[str | None] = mapped_column(Text)
    link: Mapped[str | None] = mapped_column(Text)
    message_id: Mapped[str | None] = mapped_column(String(64))


class InboxMessage(Base):
    """A hiring-system email the agent has already looked at (so each is read once). Facts only — no content."""

    __tablename__ = "inbox_messages"

    message_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    no: Mapped[int] = mapped_column(BigInteger, Identity(), unique=True)  # short number for `assign`
    thread_id: Mapped[str | None] = mapped_column(String(64))
    candidates: Mapped[list] = mapped_column(JSONB, default=list, server_default=text("'[]'::jsonb"))  # roles it could be
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id", ondelete="SET NULL"))
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sender_domain: Mapped[str | None] = mapped_column(String(253))
    kind: Mapped[str | None] = mapped_column(String(12))
    application_id: Mapped[int | None] = mapped_column(ForeignKey("applications.id", ondelete="SET NULL"))
    # matched | not_tracked | unmatched | ambiguous (you're asked) | assigned | ignored | not_application
    outcome: Mapped[str] = mapped_column(String(16))
    seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
