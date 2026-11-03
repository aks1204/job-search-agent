"""Module 6d: every summary mail and alert the agent sent you — when, its subject and counts (never its text).
The last summary's time is where the next one starts; the roles an alert named are never alerted again."""

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Index, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class MailLog(Base):
    __tablename__ = "mail_log"
    __table_args__ = (Index("ix_mail_log_kind_sent", "kind", "sent_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    kind: Mapped[str] = mapped_column(String(10))                 # summary | alert
    preview: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))  # "Send me it now"
    subject: Mapped[str] = mapped_column(Text)
    counts: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
