"""Module 5b: the agent's view of your career direction, and its suggestions waiting for your yes or no.

Your actual families / target roles / sideways appetite / track live in your confirmed profile (they're
your decisions). The agent's view only explains them and proposes changes; nothing is added or removed
from what the agent looks for without your yes (owner decision 2026-10-21)."""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class CareerDirection(Base):
    __tablename__ = "career_directions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    status: Mapped[str] = mapped_column(String(12))        # current | superseded
    view: Mapped[dict] = mapped_column(JSONB)              # position, trajectory, families + reasons, titles, …
    profile_id: Mapped[int | None] = mapped_column(ForeignKey("owner_profiles.id", ondelete="SET NULL"))
    resume_hash: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DirectionSuggestion(Base):
    __tablename__ = "direction_suggestions"
    __table_args__ = (Index("ix_direction_suggestions_status", "status"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    direction_id: Mapped[int] = mapped_column(ForeignKey("career_directions.id", ondelete="CASCADE"))
    # add (a family / target role you don't have) | remove (one you have) | include (one you excluded) |
    # appetite | track
    kind: Mapped[str] = mapped_column(String(12))
    target: Mapped[str] = mapped_column(String(16))        # progression | lateral | excluded | target_roles | …
    item: Mapped[str] = mapped_column(Text)
    why: Mapped[str] = mapped_column(Text)
    strength: Mapped[str] = mapped_column(String(10))      # strong | moderate
    status: Mapped[str] = mapped_column(String(10))        # open | accepted | declined | stale
    resume_hash: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
