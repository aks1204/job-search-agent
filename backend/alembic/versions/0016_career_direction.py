"""module 5b: career direction (the agent's view) and suggestions for your yes/no

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-21
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "career_directions",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("view", JSONB, nullable=False),
        sa.Column("profile_id", sa.BigInteger, sa.ForeignKey("owner_profiles.id", ondelete="SET NULL")),
        sa.Column("resume_hash", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "direction_suggestions",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("direction_id", sa.BigInteger, sa.ForeignKey("career_directions.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("kind", sa.String(12), nullable=False),
        sa.Column("target", sa.String(16), nullable=False),
        sa.Column("item", sa.Text, nullable=False),
        sa.Column("why", sa.Text, nullable=False),
        sa.Column("strength", sa.String(10), nullable=False),
        sa.Column("status", sa.String(10), nullable=False),
        sa.Column("resume_hash", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("answered_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_direction_suggestions_status", "direction_suggestions", ["status"])


def downgrade() -> None:
    op.drop_table("direction_suggestions")
    op.drop_table("career_directions")
