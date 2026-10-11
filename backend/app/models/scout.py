"""Scout bookkeeping: one row per run (health), one state row per scout (incremental memory)."""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Index, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class ScoutRun(Base):
    __tablename__ = "scout_runs"
    __table_args__ = (Index("ix_scout_runs_scout_started", "scout", "started_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    scout: Mapped[str] = mapped_column(String(32))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16))  # running | ok | failed | skipped
    counts: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))
    error: Mapped[str | None] = mapped_column(Text)


class ScoutState(Base):
    __tablename__ = "scout_state"

    scout: Mapped[str] = mapped_column(String(32), primary_key=True)
    state: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
