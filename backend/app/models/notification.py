"""Things the owner should hear about. Module 6 delivers them: kind `ping` as a real-time alert (spec §6.2:
only fast-lane finds and recruiter replies), the rest in the twice-daily summary mail."""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Notification(Base):
    __tablename__ = "notifications"
    __table_args__ = (Index("ix_notifications_unread", "read_at", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    kind: Mapped[str] = mapped_column(String(12))        # ping | needs_you | update
    section: Mapped[str] = mapped_column(String(16))     # summary-mail section: needs_you | happened | pipeline
    title: Mapped[str] = mapped_column(Text)
    detail: Mapped[str | None] = mapped_column(Text)
    link: Mapped[str | None] = mapped_column(Text)       # e.g. the Gmail thread
    subject_type: Mapped[str | None] = mapped_column(String(32))
    subject_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # set by Module 6
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
