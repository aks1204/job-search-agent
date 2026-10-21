"""Module 5a: your profile — read from your resume, confirmed by you, then used by every module.

One row per version: a `draft` (read from the resume, waiting for you), the `confirmed` one in use, and
older `superseded` ones (kept for the audit trail only; the resume itself is replaced in place)."""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Index, String, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class OwnerProfile(Base):
    __tablename__ = "owner_profiles"
    __table_args__ = (Index("ix_owner_profiles_status", "status", "id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    status: Mapped[str] = mapped_column(String(12))               # draft | confirmed | superseded
    data: Mapped[dict] = mapped_column(JSONB)                     # the profile (owner_stub.yaml's shape)
    resume_read: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))
    notes: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))  # from / changed / gaps
    resume_hash: Mapped[str | None] = mapped_column(String(64))   # which resume.pdf it was read from
    # When you last confirmed the time-sensitive answers (salary, notice period): {"field": "YYYY-MM-DD"}
    confirmed_fields: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
