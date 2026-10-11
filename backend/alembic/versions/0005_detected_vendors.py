"""feeds: detected job-system vendors (coverage report)

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-11
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "company_feeds",
        sa.Column("detected_vendors", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
    )
    # Fallback feeds predate vendor detection (and the Oracle reader): re-run their setup once.
    op.execute("UPDATE company_feeds SET needs_reactivation = true WHERE method = 'aggregator'")


def downgrade() -> None:
    op.drop_column("company_feeds", "detected_vendors")
