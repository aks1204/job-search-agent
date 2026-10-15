"""Module 2 Part 2c: one score per sibling cluster, and the owner's feedback on routed cards."""

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class JobScore(Base):
    __tablename__ = "job_scores"
    __table_args__ = (Index("ix_scores_band", "band", "score"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    cluster_id: Mapped[int] = mapped_column(ForeignKey("job_clusters.id", ondelete="CASCADE"), unique=True)
    assessment_id: Mapped[int] = mapped_column(ForeignKey("job_assessments.id", ondelete="CASCADE"))
    similarity: Mapped[float] = mapped_column(Float)             # stage 1: resume/profile vs posting
    similarity_version: Mapped[str] = mapped_column(String(32))
    graded: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    grade: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))  # stage 2 output
    grade_version: Mapped[str | None] = mapped_column(String(32))  # resume + profile + weights the grade used
    graded_content_hash: Mapped[str | None] = mapped_column(String(32))
    score: Mapped[int] = mapped_column(Integer)
    band: Mapped[str] = mapped_column(String(10))                 # fast_lane | inbox | maybe | archive
    breakdown: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0, server_default=text("0"))
    owner_action: Mapped[str | None] = mapped_column(String(12))  # latest feedback; skip/down hide, rescue lifts
    banded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # set by Module 6
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class JobFeedback(Base):
    """Skip / act / thumbs with reasons — the signal that tunes scoring weights (spec §2.1 Kind 3)."""

    __tablename__ = "job_feedback"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    cluster_id: Mapped[int] = mapped_column(ForeignKey("job_clusters.id", ondelete="CASCADE"))
    action: Mapped[str] = mapped_column(String(12))  # act | skip | thumbs_up | thumbs_down | maybe_rescue
    reason: Mapped[str | None] = mapped_column(Text)
    score_at_time: Mapped[int | None] = mapped_column(Integer)
    band_at_time: Mapped[str | None] = mapped_column(String(10))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
