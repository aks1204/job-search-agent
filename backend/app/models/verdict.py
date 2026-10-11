"""Fact cards and verdicts (spec §1.3–1.4).

A verdict is re-runnable: f(fact card, rules version, precedents). We keep every
verdict (history); the latest one per company is the current decision.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, String, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

CONFIDENCES = ("verified", "inferred", "unknown")


class CompanyFact(Base):
    __tablename__ = "company_facts"
    __table_args__ = (UniqueConstraint("company_id", "key", name="uq_company_fact_key"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"))
    key: Mapped[str] = mapped_column(String(48))
    value: Mapped[Any] = mapped_column(JSONB)
    confidence: Mapped[str] = mapped_column(String(10))  # verified | inferred | unknown
    source: Mapped[str] = mapped_column(String(32))  # yc | hn | adzuna | website | llm_knowledge | news | …
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CompanyVerdict(Base):
    __tablename__ = "company_verdicts"
    __table_args__ = (Index("ix_verdicts_company_created", "company_id", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    passed: Mapped[bool] = mapped_column(Boolean)
    profile: Mapped[str] = mapped_column(String(16))  # startup | established
    # Name of the check that decided a failure (None on pass): avoid_list, geography,
    # category_blocklist, viability, novelty, brand, owner_override.
    failed_check: Mapped[str | None] = mapped_column(String(32))
    summary: Mapped[str] = mapped_column(Text)
    checks: Mapped[list] = mapped_column(JSONB, default=list, server_default=text("'[]'::jsonb"))
    judgment: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))
    # Permanent grounds never auto-revive: category blocklist, owner thumbs-down.
    permanent: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    trigger: Mapped[str] = mapped_column(String(16))  # new | refresh | rules_change | override | manual
    decided_by: Mapped[str] = mapped_column(String(8))  # machine | owner
    rules_versions: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))
    precedent_ids: Mapped[list] = mapped_column(JSONB, default=list, server_default=text("'[]'::jsonb"))
    cost_usd: Mapped[float] = mapped_column(default=0.0, server_default=text("0"))
