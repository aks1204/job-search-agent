"""module 5a: owner profiles (read from the resume, confirmed by you)

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-21
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "owner_profiles",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("data", JSONB, nullable=False),
        sa.Column("resume_read", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("notes", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("resume_hash", sa.String(64)),
        sa.Column("confirmed_fields", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("confirmed_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_owner_profiles_status", "owner_profiles", ["status", "id"])


def downgrade() -> None:
    op.drop_table("owner_profiles")
