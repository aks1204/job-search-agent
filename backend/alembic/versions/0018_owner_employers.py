"""owner rule: current and past employers are never watched — their websites

Revision ID: 0018
Revises: 0017
Create Date: 2026-10-24
"""

import sqlalchemy as sa
from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "owner_employers",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("normalized", sa.String(255), nullable=False, unique=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("domain", sa.String(253)),
        sa.Column("source", sa.String(12), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("attempts", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("told_at", sa.DateTime(timezone=True)),
    )


def downgrade() -> None:
    op.drop_table("owner_employers")
