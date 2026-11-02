"""reopening matrix: when a closed posting came back; who was emailed about a role in an earlier campaign

Revision ID: 0020
Revises: 0019
Create Date: 2026-11-02
"""

import sqlalchemy as sa
from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("job_postings", sa.Column("reopened_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("outreach_emails", sa.Column("previously_emailed_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("outreach_emails", "previously_emailed_at")
    op.drop_column("job_postings", "reopened_at")
