"""module 4: applications, application events, inbox messages seen

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-19
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "applications",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("cluster_id", sa.BigInteger, sa.ForeignKey("job_clusters.id", ondelete="CASCADE"),
                  nullable=False, unique=True),
        sa.Column("company_id", sa.BigInteger, sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("on_list", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("links", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("links_checked_at", sa.DateTime(timezone=True)),
        sa.Column("queued_at", sa.DateTime(timezone=True)),
        sa.Column("applied_at", sa.DateTime(timezone=True)),
        sa.Column("applied_how", sa.String(12)),
        sa.Column("receipt_link", sa.Text),
        sa.Column("status_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("last_heard_at", sa.DateTime(timezone=True)),
        sa.Column("note", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_applications_status", "applications", ["status"])
    op.create_table(
        "application_events",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("application_id", sa.BigInteger, sa.ForeignKey("applications.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("kind", sa.String(12), nullable=False),
        sa.Column("source", sa.String(8), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("summary", sa.Text),
        sa.Column("link", sa.Text),
        sa.Column("message_id", sa.String(64)),
    )
    op.create_index("ix_application_events_app", "application_events", ["application_id", "at"])
    op.create_table(
        "inbox_messages",
        sa.Column("message_id", sa.String(64), primary_key=True),
        sa.Column("received_at", sa.DateTime(timezone=True)),
        sa.Column("sender_domain", sa.String(253)),
        sa.Column("kind", sa.String(12)),
        sa.Column("application_id", sa.BigInteger, sa.ForeignKey("applications.id", ondelete="SET NULL")),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("inbox_messages")
    op.drop_table("application_events")
    op.drop_table("applications")
