"""module 2c: job scores and owner feedback

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-16
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

EMPTY_OBJ = sa.text("'{}'::jsonb")


def upgrade() -> None:
    op.create_table(
        "job_scores",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("cluster_id", sa.BigInteger, sa.ForeignKey("job_clusters.id", ondelete="CASCADE"),
                  nullable=False, unique=True),
        sa.Column("assessment_id", sa.BigInteger, sa.ForeignKey("job_assessments.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("similarity", sa.Float, nullable=False),
        sa.Column("similarity_version", sa.String(32), nullable=False),
        sa.Column("graded", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("grade", JSONB, nullable=False, server_default=EMPTY_OBJ),
        sa.Column("grade_version", sa.String(32)),
        sa.Column("graded_content_hash", sa.String(32)),
        sa.Column("score", sa.Integer, nullable=False),
        sa.Column("band", sa.String(10), nullable=False),
        sa.Column("breakdown", JSONB, nullable=False, server_default=EMPTY_OBJ),
        sa.Column("cost_usd", sa.Float, nullable=False, server_default=sa.text("0")),
        sa.Column("owner_action", sa.String(12)),
        sa.Column("banded_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("notified_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_scores_band", "job_scores", ["band", "score"])
    op.create_table(
        "job_feedback",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("cluster_id", sa.BigInteger, sa.ForeignKey("job_clusters.id", ondelete="CASCADE"), nullable=False),
        sa.Column("action", sa.String(12), nullable=False),
        sa.Column("reason", sa.Text),
        sa.Column("score_at_time", sa.Integer),
        sa.Column("band_at_time", sa.String(10)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("job_feedback")
    op.drop_table("job_scores")
