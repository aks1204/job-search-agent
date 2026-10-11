"""watchlist: company feeds and raw postings

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-11
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def _ts(name: str, default: bool = False) -> sa.Column:
    if default:
        return sa.Column(name, sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())
    return sa.Column(name, sa.DateTime(timezone=True))


def upgrade() -> None:
    op.create_table(
        "company_feeds",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("company_id", sa.BigInteger, sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False),
        sa.Column("method", sa.String(16), nullable=False),
        sa.Column("slug", sa.String(255)),
        sa.Column("site", sa.String(255)),
        sa.Column("host", sa.String(255)),
        sa.Column("careers_url", sa.Text),
        sa.Column("ownership_evidence", sa.Text),
        _ts("verified_at"),
        sa.Column("cadence", sa.String(8), nullable=False, server_default=sa.text("'active'")),
        sa.Column("health", sa.String(8), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("needs_reactivation", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("open_jobs", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("consecutive_failures", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("last_error", sa.Text),
        _ts("activated_at", default=True),
        _ts("last_sync_at"),
        _ts("last_success_at"),
        _ts("last_change_at"),
        _ts("next_sync_at"),
        sa.UniqueConstraint("company_id", name="uq_feed_company"),
    )
    op.create_index("ix_feeds_next_sync", "company_feeds", ["next_sync_at"])

    op.create_table(
        "raw_postings",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("feed_id", sa.BigInteger, sa.ForeignKey("company_feeds.id", ondelete="CASCADE"), nullable=False),
        sa.Column("company_id", sa.BigInteger, sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False),
        sa.Column("external_id", sa.String(255), nullable=False),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("location", sa.Text),
        sa.Column("url", sa.Text),
        sa.Column("department", sa.Text),
        sa.Column("employment_type", sa.String(64)),
        sa.Column("posted_at", sa.String(64)),
        sa.Column("description", sa.Text),
        sa.Column("payload", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("content_hash", sa.String(32), nullable=False),
        sa.Column("lead_only", sa.Boolean, nullable=False, server_default=sa.text("false")),
        _ts("first_seen_at", default=True),
        _ts("last_seen_at", default=True),
        _ts("changed_at"),
        _ts("closed_at"),
        sa.UniqueConstraint("feed_id", "external_id", name="uq_posting_feed_external"),
    )
    op.create_index("ix_postings_company_open", "raw_postings", ["company_id", "closed_at"])


def downgrade() -> None:
    op.drop_table("raw_postings")
    op.drop_table("company_feeds")
