"""Module 2: canonical job postings and sibling clusters (spec §2.2).

One JobPosting per raw posting (the source row), in a single normalised shape.
Postings with the same company + normalised title + location form a cluster:
siblings (same role, different requisition IDs) are scored once and shown as one
card; job-site leads join the cluster of the canonical posting they echo.
"""

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class JobCluster(Base):
    __tablename__ = "job_clusters"
    __table_args__ = (UniqueConstraint("company_id", "fingerprint", name="uq_cluster_company_fingerprint"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"))
    fingerprint: Mapped[str] = mapped_column(String(300))
    title: Mapped[str] = mapped_column(Text)
    canonical_job_id: Mapped[int | None] = mapped_column(BigInteger)  # best non-lead posting in the cluster
    size: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class JobPosting(Base):
    __tablename__ = "job_postings"
    __table_args__ = (
        Index("ix_jobs_company", "company_id"),
        Index("ix_jobs_cluster", "cluster_id"),
        Index("ix_jobs_filter", "filter_status", "status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    raw_posting_id: Mapped[int] = mapped_column(ForeignKey("raw_postings.id", ondelete="CASCADE"), unique=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"))
    cluster_id: Mapped[int | None] = mapped_column(ForeignKey("job_clusters.id", ondelete="SET NULL"))
    method: Mapped[str] = mapped_column(String(16))  # the feed's reading method (greenhouse, workday, aggregator…)
    external_id: Mapped[str] = mapped_column(String(255))
    title: Mapped[str] = mapped_column(Text)
    title_norm: Mapped[str] = mapped_column(Text)
    location: Mapped[str | None] = mapped_column(Text)
    is_india: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    is_remote: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    employment_type: Mapped[str] = mapped_column(String(16), default="unknown", server_default=text("'unknown'"))
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    salary: Mapped[dict | None] = mapped_column(JSONB)  # {min, max, currency, period, raw}
    url: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    # present (came with the listing) | fetched | pending | unavailable (no detail endpoint) | failed
    description_status: Mapped[str] = mapped_column(String(12), default="pending", server_default=text("'pending'"))
    lead_only: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    fingerprint: Mapped[str] = mapped_column(String(300))
    content_hash: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(8), default="open", server_default=text("'open'"))  # open | closed
    # Kind-1 hard filters: pending | passed | failed (with reasons)
    filter_status: Mapped[str] = mapped_column(String(8), default="pending", server_default=text("'pending'"))
    filter_reasons: Mapped[list] = mapped_column(JSONB, default=list, server_default=text("'[]'::jsonb"))
    changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # material change → re-score once
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
