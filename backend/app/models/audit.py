"""Audit log (spec §6.4): every consequential machine decision — what, why, what it affected."""

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class AuditLog(Base):
    __tablename__ = "audit_log"
    __table_args__ = (
        Index("ix_audit_created_at", "created_at"),
        Index("ix_audit_subject", "subject_type", "subject_id"),
        Index("ix_audit_module_action", "module", "action"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    module: Mapped[str] = mapped_column(String(32))
    action: Mapped[str] = mapped_column(String(64))
    subject_type: Mapped[str | None] = mapped_column(String(32))
    subject_id: Mapped[str | None] = mapped_column(String(128))
    # Plain-language one-liner shown in the log; `detail` holds the expandable reasoning.
    summary: Mapped[str] = mapped_column(Text)
    detail: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))
    # Judgment entries get a one-tap override in the UI; the override becomes a precedent.
    is_judgment: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    rules_version: Mapped[str | None] = mapped_column(String(64))
    overridden_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    override_memory_id: Mapped[int | None] = mapped_column(ForeignKey("memory_entries.id"))
