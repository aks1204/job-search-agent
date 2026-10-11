"""Companies (identity = registrable domain), every scout sighting of them, and contact leads."""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

# Lifecycle. Part 1a writes the first two; later parts add the rest.
#   unresolved  seen by name only — no domain yet, so not in the candidate pipe
#   candidate   identity known, waiting for evaluation (Part 1b)
#   passed / rejected / watching / dormant / archived / blocked   (Parts 1b, 1c)
STATUS_UNRESOLVED = "unresolved"
STATUS_CANDIDATE = "candidate"

COMPANY_TYPES = ("startup", "established", "unknown")


class Company(Base):
    __tablename__ = "companies"
    __table_args__ = (
        Index("ix_companies_status", "status"),
        Index(
            "ix_companies_name_trgm",
            "normalized_name",
            postgresql_using="gin",
            postgresql_ops={"normalized_name": "gin_trgm_ops"},
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    domain: Mapped[str | None] = mapped_column(String(253), unique=True)
    # How we know the domain: source (scout supplied it) | seed | search (inferred) | manual
    domain_source: Mapped[str | None] = mapped_column(String(16))
    name: Mapped[str] = mapped_column(String(255))
    normalized_name: Mapped[str] = mapped_column(String(255))
    aliases: Mapped[list] = mapped_column(JSONB, default=list, server_default=text("'[]'::jsonb"))
    website: Mapped[str | None] = mapped_column(Text)
    type: Mapped[str] = mapped_column(String(16), default="unknown", server_default=text("'unknown'"))
    type_source: Mapped[str | None] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(20), default=STATUS_UNRESOLVED)
    # Union of what each scout knows, keyed by scout name (latest sighting wins per scout).
    source_data: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))
    resolution_attempts: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    next_resolution_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class CompanySighting(Base):
    """One thing a scout saw (a YC listing, an aggregator job, an HN comment, a seed row).

    (scout, external_id) is the scout's memory: seen before -> only last_seen moves.
    Aggregator job sightings double as breadcrumbs for Part 1c's coverage cross-check.
    """

    __tablename__ = "company_sightings"
    __table_args__ = (
        UniqueConstraint("scout", "external_id", name="uq_sighting_scout_external"),
        Index("ix_sightings_company", "company_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"))
    scout: Mapped[str] = mapped_column(String(32))
    external_id: Mapped[str] = mapped_column(String(255))
    name_seen: Mapped[str] = mapped_column(String(255))
    domain_seen: Mapped[str | None] = mapped_column(String(253))
    url: Mapped[str | None] = mapped_column(Text)
    data: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ContactLead(Base):
    """Addresses found in passing (HN comments, postings) — stored now, used by Module 3."""

    __tablename__ = "contact_leads"
    __table_args__ = (UniqueConstraint("company_id", "email", name="uq_contact_lead_company_email"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"))
    email: Mapped[str] = mapped_column(String(320))
    name: Mapped[str | None] = mapped_column(String(255))
    role: Mapped[str | None] = mapped_column(String(255))
    source: Mapped[str] = mapped_column(String(32))
    context: Mapped[str | None] = mapped_column(Text)
    found_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
