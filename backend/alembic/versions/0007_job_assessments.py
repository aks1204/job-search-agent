"""module 2b: job assessments (AI read + rules) and pay-conversion factors

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-16
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

EMPTY_LIST, EMPTY_OBJ = sa.text("'[]'::jsonb"), sa.text("'{}'::jsonb")


def upgrade() -> None:
    op.create_table(
        "job_assessments",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("cluster_id", sa.BigInteger, sa.ForeignKey("job_clusters.id", ondelete="CASCADE"),
                  nullable=False, unique=True),
        sa.Column("job_id", sa.BigInteger, sa.ForeignKey("job_postings.id", ondelete="CASCADE"), nullable=False),
        sa.Column("content_hash", sa.String(32), nullable=False),
        sa.Column("profile_version", sa.String(32), nullable=False),
        sa.Column("rules_version", sa.String(32), nullable=False),
        sa.Column("read", JSONB, nullable=False),
        sa.Column("outcome", sa.String(10), nullable=False),
        sa.Column("reasons", JSONB, nullable=False, server_default=EMPTY_LIST),
        sa.Column("flags", JSONB, nullable=False, server_default=EMPTY_LIST),
        sa.Column("penalties", JSONB, nullable=False, server_default=EMPTY_LIST),
        sa.Column("comp", JSONB, nullable=False, server_default=EMPTY_OBJ),
        sa.Column("level", JSONB, nullable=False, server_default=EMPTY_OBJ),
        sa.Column("lead_only", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("cost_usd", sa.Float, nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "exchange_factors",
        sa.Column("currency", sa.String(3), primary_key=True),
        sa.Column("country_iso3", sa.String(3), nullable=False),
        sa.Column("ppp_lcu_per_intl", sa.Float, nullable=False),
        sa.Column("fx_lcu_per_usd", sa.Float, nullable=False),
        sa.Column("year", sa.Integer, nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("exchange_factors")
    op.drop_table("job_assessments")
