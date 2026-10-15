"""module 2: job postings and sibling clusters

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-16
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def _ts(name: str, default: bool = False) -> sa.Column:
    if default:
        return sa.Column(name, sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())
    return sa.Column(name, sa.DateTime(timezone=True))


def upgrade() -> None:
    op.create_table(
        "job_clusters",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("company_id", sa.BigInteger, sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False),
        sa.Column("fingerprint", sa.String(300), nullable=False),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("canonical_job_id", sa.BigInteger),
        sa.Column("size", sa.Integer, nullable=False, server_default=sa.text("0")),
        _ts("created_at", default=True),
        _ts("updated_at", default=True),
        sa.UniqueConstraint("company_id", "fingerprint", name="uq_cluster_company_fingerprint"),
    )
    op.create_table(
        "job_postings",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("raw_posting_id", sa.BigInteger, sa.ForeignKey("raw_postings.id", ondelete="CASCADE"),
                  nullable=False, unique=True),
        sa.Column("company_id", sa.BigInteger, sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False),
        sa.Column("cluster_id", sa.BigInteger, sa.ForeignKey("job_clusters.id", ondelete="SET NULL")),
        sa.Column("method", sa.String(16), nullable=False),
        sa.Column("external_id", sa.String(255), nullable=False),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("title_norm", sa.Text, nullable=False),
        sa.Column("location", sa.Text),
        sa.Column("is_india", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("is_remote", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("employment_type", sa.String(16), nullable=False, server_default=sa.text("'unknown'")),
        _ts("posted_at"),
        sa.Column("salary", JSONB),
        sa.Column("url", sa.Text),
        sa.Column("description", sa.Text),
        sa.Column("description_status", sa.String(12), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("lead_only", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("fingerprint", sa.String(300), nullable=False),
        sa.Column("content_hash", sa.String(32), nullable=False),
        sa.Column("status", sa.String(8), nullable=False, server_default=sa.text("'open'")),
        sa.Column("filter_status", sa.String(8), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("filter_reasons", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        _ts("changed_at"),
        _ts("first_seen_at", default=True),
        _ts("last_seen_at", default=True),
        _ts("closed_at"),
    )
    op.create_index("ix_jobs_company", "job_postings", ["company_id"])
    op.create_index("ix_jobs_cluster", "job_postings", ["cluster_id"])
    op.create_index("ix_jobs_filter", "job_postings", ["filter_status", "status"])


def downgrade() -> None:
    op.drop_table("job_postings")
    op.drop_table("job_clusters")
