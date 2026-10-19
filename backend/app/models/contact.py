"""Module 3 Part 3a: people at companies, their addresses, what we know about each mail domain,
benches (who a company's outreach can use) and each role's channel (designated address or bench)."""

from datetime import datetime

from sqlalchemy import (
    BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func, text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

# Contact status: active | frozen (same-name collision: never guessed at) | departed (positive proof
# only — ambiguity never retires anyone) | wrong_person (said "not me" — set by Part 3d)
# Address confidence, best first: proven (mailbox history: delivered/replied) | verified (mailbox
# confirmed, domain not catch-all) | low (catch-all, or couldn't be checked) | unchecked | invalid
CONFIDENCE_RANK = {"proven": 4, "verified": 3, "low": 2, "unchecked": 1, "invalid": 0}


class Contact(Base):
    __tablename__ = "contacts"
    __table_args__ = (Index("ix_contacts_company_name", "company_id", "name_key"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"))
    full_name: Mapped[str] = mapped_column(String(255))
    name_key: Mapped[str] = mapped_column(String(255))          # normalised "first last"
    first_name: Mapped[str] = mapped_column(String(120))
    last_name: Mapped[str] = mapped_column(String(120))
    title: Mapped[str | None] = mapped_column(String(500))
    location: Mapped[str | None] = mapped_column(String(255))
    linkedin_url: Mapped[str | None] = mapped_column(String(500), unique=True)
    kind: Mapped[str] = mapped_column(String(16))                # founder | recruiting | hiring_manager | other
    function: Mapped[str | None] = mapped_column(String(32))
    geo: Mapped[str | None] = mapped_column(String(32))          # india | remote | elsewhere
    seniority: Mapped[int] = mapped_column(Integer, default=2, server_default=text("2"))  # 1 junior … 5 exec
    source: Mapped[str] = mapped_column(String(16))              # search | published | hn | manual
    status: Mapped[str] = mapped_column(String(16), default="active", server_default=text("'active'"))
    status_reason: Mapped[str | None] = mapped_column(Text)
    snippet: Mapped[str | None] = mapped_column(Text)            # the public search snippet we judged from
    person_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    away_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # out-of-office: wait till back
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class ContactEmail(Base):
    __tablename__ = "contact_emails"
    __table_args__ = (UniqueConstraint("company_id", "email", name="uq_contact_email_company"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"))
    contact_id: Mapped[int | None] = mapped_column(ForeignKey("contacts.id", ondelete="SET NULL"))  # None = shared mailbox
    email: Mapped[str] = mapped_column(String(320))
    source: Mapped[str] = mapped_column(String(16))              # published | hunter | apollo | constructed | manual
    pattern: Mapped[str | None] = mapped_column(String(32))      # the format it was built from (constructed)
    confidence: Mapped[str] = mapped_column(String(10), default="unchecked", server_default=text("'unchecked'"))
    vouched: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))  # provider stands by it
    checks: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Mailbox history (filled by Part 3c/3d) — overrides every other signal.
    delivered_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    replied_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    bounced_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class MailDomain(Base):
    """What we know about one company mail domain: MX, catch-all, the learned address pattern."""

    __tablename__ = "mail_domains"

    domain: Mapped[str] = mapped_column(String(253), primary_key=True)
    has_mx: Mapped[bool | None] = mapped_column(Boolean)
    mx_hosts: Mapped[list] = mapped_column(JSONB, default=list, server_default=text("'[]'::jsonb"))
    mx_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    catch_all: Mapped[bool | None] = mapped_column(Boolean)       # True ⇒ every address there is low-confidence
    catch_all_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pattern: Mapped[str | None] = mapped_column(String(32))
    pattern_source: Mapped[str | None] = mapped_column(String(320))  # the real address it was learned from
    pattern_distrusted: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class EnrichmentLookup(Base):
    """Every paid-credit lookup, cached forever — including 'nothing found'."""

    __tablename__ = "enrichment_lookups"
    __table_args__ = (UniqueConstraint("provider", "key", name="uq_enrichment_provider_key"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    provider: Mapped[str] = mapped_column(String(24))            # hunter_finder | hunter_domain | apollo
    key: Mapped[str] = mapped_column(String(600))                # domain|name_key, or domain
    found: Mapped[bool] = mapped_column(Boolean)
    result: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Bench(Base):
    __tablename__ = "benches"
    __table_args__ = (UniqueConstraint("company_id", "key", name="uq_bench_company_key"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"))
    key: Mapped[str] = mapped_column(String(64))                 # "all", or "<function>/<geo>" for MNCs
    shape: Mapped[str] = mapped_column(String(12))               # startup | mid_size | mnc
    searched: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))  # slot kind → last search
    filled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class BenchMember(Base):
    __tablename__ = "bench_members"
    __table_args__ = (UniqueConstraint("bench_id", "contact_id", name="uq_bench_member"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    bench_id: Mapped[int] = mapped_column(ForeignKey("benches.id", ondelete="CASCADE"))
    contact_id: Mapped[int] = mapped_column(ForeignKey("contacts.id", ondelete="CASCADE"))
    slot_kind: Mapped[str] = mapped_column(String(16))
    rank: Mapped[int] = mapped_column(Integer, default=0)        # rotation order, 1 = first
    # Same-name people we can't tell apart share this one slot, frozen (spec: don't guess).
    co_suspects: Mapped[list] = mapped_column(JSONB, default=list, server_default=text("'[]'::jsonb"))
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RoleChannel(Base):
    """How outreach for one role (sibling cluster) reaches the company."""

    __tablename__ = "role_channels"

    cluster_id: Mapped[int] = mapped_column(ForeignKey("job_clusters.id", ondelete="CASCADE"), primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"))
    mode: Mapped[str] = mapped_column(String(12))                # designated | bench | none
    designated_email: Mapped[str | None] = mapped_column(String(320))
    designated_context: Mapped[str | None] = mapped_column(Text)
    bench_id: Mapped[int | None] = mapped_column(ForeignKey("benches.id", ondelete="SET NULL"))
    manual: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))  # owner override
    note: Mapped[str | None] = mapped_column(Text)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
