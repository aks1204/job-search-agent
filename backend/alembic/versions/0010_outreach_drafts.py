"""module 3b: outreach drafts (one per role)

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-18
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "outreach_drafts",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("cluster_id", sa.BigInteger, sa.ForeignKey("job_clusters.id", ondelete="CASCADE"),
                  nullable=False, unique=True),
        sa.Column("version", sa.Integer, nullable=False, server_default=sa.text("1")),
        sa.Column("scenario", sa.String(24), nullable=False),
        sa.Column("role_name", sa.Text, nullable=False),
        sa.Column("subject", sa.Text, nullable=False),
        sa.Column("body", sa.Text, nullable=False),
        sa.Column("intro", sa.Text, nullable=False),
        sa.Column("points", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("req_ids", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("attach_resume", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("status", sa.String(10), nullable=False),
        sa.Column("truth", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("inputs_version", sa.String(32), nullable=False),
        sa.Column("cost_usd", sa.Float, nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("outreach_drafts")
