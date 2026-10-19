"""module 4: outside applications; numbered inbox questions

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-19
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("applications", sa.Column("outside", sa.Boolean, nullable=False, server_default=sa.text("false")))
    op.add_column("inbox_messages", sa.Column("no", sa.BigInteger, sa.Identity(), nullable=False))
    op.create_unique_constraint("uq_inbox_messages_no", "inbox_messages", ["no"])
    op.add_column("inbox_messages", sa.Column("thread_id", sa.String(64)))
    op.add_column("inbox_messages", sa.Column("company_id", sa.BigInteger,
                                              sa.ForeignKey("companies.id", ondelete="SET NULL")))
    op.add_column("inbox_messages", sa.Column("candidates", JSONB, nullable=False,
                                              server_default=sa.text("'[]'::jsonb")))


def downgrade() -> None:
    op.drop_column("inbox_messages", "candidates")
    op.drop_column("inbox_messages", "company_id")
    op.drop_column("inbox_messages", "thread_id")
    op.drop_constraint("uq_inbox_messages_no", "inbox_messages")
    op.drop_column("inbox_messages", "no")
    op.drop_column("applications", "outside")
