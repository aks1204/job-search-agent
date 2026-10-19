"""module 3d: reply classification, outcomes, out-of-office, notifications

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-19
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("outreach_emails", sa.Column("reply_category", sa.String(12)))
    op.add_column("outreach_emails", sa.Column("reply_summary", sa.Text))
    op.add_column("outreach_emails", sa.Column("reply_from", sa.String(320)))
    op.add_column("outreach_emails", sa.Column("auto_reply_at", sa.DateTime(timezone=True)))
    op.add_column("outreach_emails", sa.Column("handled_ids", JSONB, nullable=False,
                                               server_default=sa.text("'[]'::jsonb")))
    op.add_column("outreach_campaigns", sa.Column("outcome", sa.String(12)))
    op.add_column("outreach_campaigns", sa.Column("outcome_at", sa.DateTime(timezone=True)))
    op.add_column("contacts", sa.Column("away_until", sa.DateTime(timezone=True)))
    op.create_table(
        "notifications",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("kind", sa.String(12), nullable=False),
        sa.Column("section", sa.String(16), nullable=False),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("detail", sa.Text),
        sa.Column("link", sa.Text),
        sa.Column("subject_type", sa.String(32)),
        sa.Column("subject_id", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("delivered_at", sa.DateTime(timezone=True)),
        sa.Column("read_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_notifications_unread", "notifications", ["read_at", "created_at"])


def downgrade() -> None:
    op.drop_table("notifications")
    op.drop_column("contacts", "away_until")
    for col in ("outcome_at", "outcome"):
        op.drop_column("outreach_campaigns", col)
    for col in ("handled_ids", "auto_reply_at", "reply_from", "reply_summary", "reply_category"):
        op.drop_column("outreach_emails", col)
