"""module 5c: reasons on feedback (your chips + the agent's probable reasons)

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-21
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("job_feedback", sa.Column("chips", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")))
    op.add_column("job_feedback", sa.Column("inferred", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")))


def downgrade() -> None:
    op.drop_column("job_feedback", "inferred")
    op.drop_column("job_feedback", "chips")
