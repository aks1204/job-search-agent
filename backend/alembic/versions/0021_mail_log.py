"""summary mail and alerts: what was sent to you, and when

Revision ID: 0021
Revises: 0020
Create Date: 2026-11-04
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mail_log",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("kind", sa.String(10), nullable=False),
        sa.Column("preview", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("counts", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_mail_log_kind_sent", "mail_log", ["kind", "sent_at"])


def downgrade() -> None:
    op.drop_index("ix_mail_log_kind_sent", table_name="mail_log")
    op.drop_table("mail_log")
