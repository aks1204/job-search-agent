"""company discovery: companies, sightings, contact leads, scout runs/state

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-11
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

EMPTY_OBJ = sa.text("'{}'::jsonb")
EMPTY_LIST = sa.text("'[]'::jsonb")


def _ts(name: str, nullable: bool = False) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable, server_default=sa.func.now())


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    op.create_table(
        "companies",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("domain", sa.String(253), unique=True),
        sa.Column("domain_source", sa.String(16)),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("normalized_name", sa.String(255), nullable=False),
        sa.Column("aliases", JSONB, nullable=False, server_default=EMPTY_LIST),
        sa.Column("website", sa.Text),
        sa.Column("type", sa.String(16), nullable=False, server_default=sa.text("'unknown'")),
        sa.Column("type_source", sa.String(16)),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("source_data", JSONB, nullable=False, server_default=EMPTY_OBJ),
        sa.Column("resolution_attempts", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("next_resolution_at", sa.DateTime(timezone=True)),
        _ts("first_seen_at"),
        _ts("updated_at"),
    )
    op.create_index("ix_companies_status", "companies", ["status"])
    op.create_index(
        "ix_companies_name_trgm",
        "companies",
        ["normalized_name"],
        postgresql_using="gin",
        postgresql_ops={"normalized_name": "gin_trgm_ops"},
    )

    op.create_table(
        "company_sightings",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("company_id", sa.BigInteger, sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False),
        sa.Column("scout", sa.String(32), nullable=False),
        sa.Column("external_id", sa.String(255), nullable=False),
        sa.Column("name_seen", sa.String(255), nullable=False),
        sa.Column("domain_seen", sa.String(253)),
        sa.Column("url", sa.Text),
        sa.Column("data", JSONB, nullable=False, server_default=EMPTY_OBJ),
        _ts("first_seen_at"),
        _ts("last_seen_at"),
        sa.UniqueConstraint("scout", "external_id", name="uq_sighting_scout_external"),
    )
    op.create_index("ix_sightings_company", "company_sightings", ["company_id"])

    op.create_table(
        "contact_leads",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("company_id", sa.BigInteger, sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("name", sa.String(255)),
        sa.Column("role", sa.String(255)),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("context", sa.Text),
        _ts("found_at"),
        sa.UniqueConstraint("company_id", "email", name="uq_contact_lead_company_email"),
    )

    op.create_table(
        "scout_runs",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("scout", sa.String(32), nullable=False),
        _ts("started_at"),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("counts", JSONB, nullable=False, server_default=EMPTY_OBJ),
        sa.Column("error", sa.Text),
    )
    op.create_index("ix_scout_runs_scout_started", "scout_runs", ["scout", "started_at"])

    op.create_table(
        "scout_state",
        sa.Column("scout", sa.String(32), primary_key=True),
        sa.Column("state", JSONB, nullable=False, server_default=EMPTY_OBJ),
        _ts("updated_at"),
    )


def downgrade() -> None:
    op.drop_table("scout_state")
    op.drop_table("scout_runs")
    op.drop_table("contact_leads")
    op.drop_table("company_sightings")
    op.drop_table("companies")
