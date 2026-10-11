"""Watchlist tracking records (spec §1.5) and the raw postings they produce (consumed by Module 2)."""

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

# Reading methods, cheapest reliable rung first (spec §1.5 step 2).
METHODS = ("greenhouse", "lever", "ashby", "smartrecruiters", "workable", "recruitee", "workday", "oracle", "eightfold", "adapter", "aggregator")


class CompanyFeed(Base):
    __tablename__ = "company_feeds"
    __table_args__ = (
        UniqueConstraint("company_id", name="uq_feed_company"),
        Index("ix_feeds_next_sync", "next_sync_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"))
    method: Mapped[str] = mapped_column(String(16))
    slug: Mapped[str | None] = mapped_column(String(255))
    site: Mapped[str | None] = mapped_column(String(255))  # Workday site name
    host: Mapped[str | None] = mapped_column(String(255))  # Workday "tenant.wdN"
    careers_url: Mapped[str | None] = mapped_column(Text)
    ownership_evidence: Mapped[str | None] = mapped_column(Text)
    # Every job-system vendor recognised during discovery (readable or not) — drives the coverage report.
    detected_vendors: Mapped[list] = mapped_column(JSONB, default=list, server_default=text("'[]'::jsonb"))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cadence: Mapped[str] = mapped_column(String(8), default="active", server_default=text("'active'"))
    health: Mapped[str] = mapped_column(String(8), default="pending", server_default=text("'pending'"))
    needs_reactivation: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    open_jobs: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    last_error: Mapped[str | None] = mapped_column(Text)
    activated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_change_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # a job opened/closed/changed
    next_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RawPosting(Base):
    """A job as the feed reported it. Module 2 normalises, dedupes and scores these."""

    __tablename__ = "raw_postings"
    __table_args__ = (
        UniqueConstraint("feed_id", "external_id", name="uq_posting_feed_external"),
        Index("ix_postings_company_open", "company_id", "closed_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    feed_id: Mapped[int] = mapped_column(ForeignKey("company_feeds.id", ondelete="CASCADE"))
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"))
    external_id: Mapped[str] = mapped_column(String(255))
    title: Mapped[str] = mapped_column(Text)
    location: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(Text)
    department: Mapped[str | None] = mapped_column(Text)
    employment_type: Mapped[str | None] = mapped_column(String(64))
    posted_at: Mapped[str | None] = mapped_column(String(64))  # as reported; Module 2 parses
    description: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))
    content_hash: Mapped[str] = mapped_column(String(32))
    # Aggregator-floor rows are leads only: never acted on until a canonical posting confirms them.
    lead_only: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
