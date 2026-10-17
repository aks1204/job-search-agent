"""module 3a: contacts, addresses, mail domains, enrichment cache, benches, role channels

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-18
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

EMPTY_OBJ = sa.text("'{}'::jsonb")
EMPTY_LIST = sa.text("'[]'::jsonb")


def _ts(name: str, nullable: bool = False):
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable,
                     server_default=None if nullable else sa.func.now())


def _company():
    return sa.Column("company_id", sa.BigInteger, sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False)


def upgrade() -> None:
    op.create_table(
        "contacts",
        sa.Column("id", sa.BigInteger, primary_key=True),
        _company(),
        sa.Column("full_name", sa.String(255), nullable=False),
        sa.Column("name_key", sa.String(255), nullable=False),
        sa.Column("first_name", sa.String(120), nullable=False),
        sa.Column("last_name", sa.String(120), nullable=False),
        sa.Column("title", sa.String(500)),
        sa.Column("location", sa.String(255)),
        sa.Column("linkedin_url", sa.String(500), unique=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("function", sa.String(32)),
        sa.Column("geo", sa.String(32)),
        sa.Column("seniority", sa.Integer, nullable=False, server_default=sa.text("2")),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default=sa.text("'active'")),
        sa.Column("status_reason", sa.Text),
        sa.Column("snippet", sa.Text),
        _ts("person_checked_at", True),
        _ts("first_seen_at"),
        _ts("updated_at"),
    )
    op.create_index("ix_contacts_company_name", "contacts", ["company_id", "name_key"])

    op.create_table(
        "contact_emails",
        sa.Column("id", sa.BigInteger, primary_key=True),
        _company(),
        sa.Column("contact_id", sa.BigInteger, sa.ForeignKey("contacts.id", ondelete="SET NULL")),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("pattern", sa.String(32)),
        sa.Column("confidence", sa.String(10), nullable=False, server_default=sa.text("'unchecked'")),
        sa.Column("vouched", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("checks", JSONB, nullable=False, server_default=EMPTY_OBJ),
        _ts("verified_at", True),
        sa.Column("delivered_count", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("replied_count", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("bounced_count", sa.Integer, nullable=False, server_default=sa.text("0")),
        _ts("created_at"),
        _ts("updated_at"),
        sa.UniqueConstraint("company_id", "email", name="uq_contact_email_company"),
    )
    op.create_index("ix_contact_emails_contact", "contact_emails", ["contact_id"])

    op.create_table(
        "mail_domains",
        sa.Column("domain", sa.String(253), primary_key=True),
        sa.Column("has_mx", sa.Boolean),
        sa.Column("mx_hosts", JSONB, nullable=False, server_default=EMPTY_LIST),
        _ts("mx_checked_at", True),
        sa.Column("catch_all", sa.Boolean),
        _ts("catch_all_checked_at", True),
        sa.Column("pattern", sa.String(32)),
        sa.Column("pattern_source", sa.String(320)),
        sa.Column("pattern_distrusted", sa.Boolean, nullable=False, server_default=sa.text("false")),
        _ts("updated_at"),
    )

    op.create_table(
        "enrichment_lookups",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("provider", sa.String(24), nullable=False),
        sa.Column("key", sa.String(600), nullable=False),
        sa.Column("found", sa.Boolean, nullable=False),
        sa.Column("result", JSONB, nullable=False, server_default=EMPTY_OBJ),
        _ts("created_at"),
        sa.UniqueConstraint("provider", "key", name="uq_enrichment_provider_key"),
    )

    op.create_table(
        "benches",
        sa.Column("id", sa.BigInteger, primary_key=True),
        _company(),
        sa.Column("key", sa.String(64), nullable=False),
        sa.Column("shape", sa.String(12), nullable=False),
        sa.Column("searched", JSONB, nullable=False, server_default=EMPTY_OBJ),
        _ts("filled_at", True),
        _ts("created_at"),
        sa.UniqueConstraint("company_id", "key", name="uq_bench_company_key"),
    )

    op.create_table(
        "bench_members",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("bench_id", sa.BigInteger, sa.ForeignKey("benches.id", ondelete="CASCADE"), nullable=False),
        sa.Column("contact_id", sa.BigInteger, sa.ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("slot_kind", sa.String(16), nullable=False),
        sa.Column("rank", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("co_suspects", JSONB, nullable=False, server_default=EMPTY_LIST),
        _ts("added_at"),
        sa.UniqueConstraint("bench_id", "contact_id", name="uq_bench_member"),
    )

    op.create_table(
        "role_channels",
        sa.Column("cluster_id", sa.BigInteger, sa.ForeignKey("job_clusters.id", ondelete="CASCADE"), primary_key=True),
        _company(),
        sa.Column("mode", sa.String(12), nullable=False),
        sa.Column("designated_email", sa.String(320)),
        sa.Column("designated_context", sa.Text),
        sa.Column("bench_id", sa.BigInteger, sa.ForeignKey("benches.id", ondelete="SET NULL")),
        sa.Column("manual", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("note", sa.Text),
        _ts("next_retry_at", True),
        _ts("decided_at"),
        _ts("updated_at"),
    )


def downgrade() -> None:
    for table in ("role_channels", "bench_members", "benches", "enrichment_lookups", "mail_domains",
                  "contact_emails", "contacts"):
        op.drop_table(table)
