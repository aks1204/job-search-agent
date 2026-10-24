"""outreach: every address an HN post asks applicants to use gets its own email

Revision ID: 0019
Revises: 0018
Create Date: 2026-10-25
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("role_channels", sa.Column("also_emails", JSONB, nullable=False,
                                             server_default=sa.text("'[]'::jsonb")))


def downgrade() -> None:
    op.drop_column("role_channels", "also_emails")
