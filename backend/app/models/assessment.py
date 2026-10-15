"""Module 2 Part 2b: one AI read + Kind-2/level/pay rules per sibling cluster, and pay-conversion factors."""

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Float, ForeignKey, Integer, String, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class JobAssessment(Base):
    """What the AI read in a role's description, and what the rules made of it.

    Keyed by cluster: siblings (same role, several req IDs) are assessed once.
    Re-run when the canonical posting's content or the owner's profile changes.
    """

    __tablename__ = "job_assessments"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    cluster_id: Mapped[int] = mapped_column(ForeignKey("job_clusters.id", ondelete="CASCADE"), unique=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("job_postings.id", ondelete="CASCADE"))  # the posting read
    content_hash: Mapped[str] = mapped_column(String(32))
    profile_version: Mapped[str] = mapped_column(String(32))  # what the AI was told about the candidate
    rules_version: Mapped[str] = mapped_column(String(32))    # rules + profile values applied to the read
    read: Mapped[dict] = mapped_column(JSONB)  # the AI's structured read
    outcome: Mapped[str] = mapped_column(String(10))  # passed | rejected
    reasons: Mapped[list] = mapped_column(JSONB, default=list, server_default=text("'[]'::jsonb"))
    flags: Mapped[list] = mapped_column(JSONB, default=list, server_default=text("'[]'::jsonb"))
    penalties: Mapped[list] = mapped_column(JSONB, default=list, server_default=text("'[]'::jsonb"))
    comp: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))
    level: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))
    lead_only: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class ExchangeFactor(Base):
    """World Bank PPP (local currency per international $) and exchange rate (local currency per US$)."""

    __tablename__ = "exchange_factors"

    currency: Mapped[str] = mapped_column(String(3), primary_key=True)
    country_iso3: Mapped[str] = mapped_column(String(3))
    ppp_lcu_per_intl: Mapped[float] = mapped_column(Float)
    fx_lcu_per_usd: Mapped[float] = mapped_column(Float)
    year: Mapped[int] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(String(16))  # worldbank | builtin
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
