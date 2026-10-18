"""module 3c: mailbox, campaigns, outreach emails, draft edits, bounce breaker

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-18
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def _ts(name: str, nullable: bool = False):
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable,
                     server_default=None if nullable else sa.func.now())


def upgrade() -> None:
    op.create_table(
        "mail_accounts",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("provider", sa.String(16), nullable=False, server_default=sa.text("'gmail'")),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("refresh_token_enc", sa.Text, nullable=False),
        sa.Column("scopes", sa.Text, nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default=sa.text("'ok'")),
        _ts("connected_at"),
        _ts("updated_at"),
    )
    op.create_table(
        "outreach_campaigns",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("cluster_id", sa.BigInteger, sa.ForeignKey("job_clusters.id", ondelete="CASCADE"), nullable=False),
        sa.Column("company_id", sa.BigInteger, sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False),
        sa.Column("draft_id", sa.BigInteger, sa.ForeignKey("outreach_drafts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("draft_version", sa.Integer, nullable=False),
        sa.Column("subject", sa.Text, nullable=False),
        sa.Column("body", sa.Text, nullable=False),
        sa.Column("edited", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("attach_resume", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("status", sa.String(16), nullable=False, server_default=sa.text("'pending_review'")),
        sa.Column("status_reason", sa.Text),
        _ts("armed_at", True),
        _ts("ended_at", True),
        _ts("created_at"),
        _ts("updated_at"),
    )
    op.create_index("ix_campaigns_status", "outreach_campaigns", ["status"])
    op.create_table(
        "outreach_emails",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("campaign_id", sa.BigInteger, sa.ForeignKey("outreach_campaigns.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("position", sa.Integer, nullable=False),
        sa.Column("wave", sa.Integer, nullable=False),
        sa.Column("contact_id", sa.BigInteger, sa.ForeignKey("contacts.id", ondelete="SET NULL")),
        sa.Column("address_id", sa.BigInteger, sa.ForeignKey("contact_emails.id", ondelete="SET NULL")),
        sa.Column("to_email", sa.String(320), nullable=False),
        sa.Column("to_name", sa.String(255)),
        sa.Column("confidence", sa.String(10), nullable=False),
        sa.Column("vouched", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("decision", sa.String(10), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("allow_low", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("status", sa.String(12), nullable=False, server_default=sa.text("'queued'")),
        sa.Column("status_reason", sa.Text),
        _ts("sent_at", True),
        sa.Column("gmail_message_id", sa.String(64)),
        sa.Column("gmail_thread_id", sa.String(64)),
        sa.Column("sent_subject", sa.Text),
        sa.Column("sent_body", sa.Text),
        _ts("replied_at", True),
        _ts("bounced_at", True),
        sa.Column("delivered_counted", sa.Boolean, nullable=False, server_default=sa.text("false")),
        _ts("last_checked_at", True),
        _ts("created_at"),
        _ts("updated_at"),
    )
    op.create_index("ix_outreach_emails_campaign", "outreach_emails", ["campaign_id"])
    op.create_index("ix_outreach_emails_status", "outreach_emails", ["status"])
    op.create_table(
        "draft_edits",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("cluster_id", sa.BigInteger, sa.ForeignKey("job_clusters.id", ondelete="CASCADE"), nullable=False),
        sa.Column("before_subject", sa.Text, nullable=False),
        sa.Column("before_body", sa.Text, nullable=False),
        sa.Column("after_subject", sa.Text, nullable=False),
        sa.Column("after_body", sa.Text, nullable=False),
        _ts("created_at"),
    )
    op.create_table(
        "send_breaker",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("state", sa.String(12), nullable=False, server_default=sa.text("'ok'")),
        sa.Column("reason", sa.Text),
        _ts("tripped_at", True),
        _ts("reset_at"),
        sa.Column("history", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
    )


def downgrade() -> None:
    for table in ("send_breaker", "draft_edits", "outreach_emails", "outreach_campaigns", "mail_accounts"):
        op.drop_table(table)
