"""company judgment: fact cards, verdicts, llm call ledger

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-11
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("companies", sa.Column("llm_resolution_tried_at", sa.DateTime(timezone=True)))
    op.add_column("companies", sa.Column("reeval_reason", sa.String(16)))
    op.add_column("companies", sa.Column("enriched_at", sa.DateTime(timezone=True)))

    op.create_table(
        "company_facts",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("company_id", sa.BigInteger, sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False),
        sa.Column("key", sa.String(48), nullable=False),
        sa.Column("value", JSONB),
        sa.Column("confidence", sa.String(10), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("company_id", "key", name="uq_company_fact_key"),
    )

    op.create_table(
        "company_verdicts",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("company_id", sa.BigInteger, sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("passed", sa.Boolean, nullable=False),
        sa.Column("profile", sa.String(16), nullable=False),
        sa.Column("failed_check", sa.String(32)),
        sa.Column("summary", sa.Text, nullable=False),
        sa.Column("checks", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("judgment", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("permanent", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("trigger", sa.String(16), nullable=False),
        sa.Column("decided_by", sa.String(8), nullable=False),
        sa.Column("rules_versions", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("precedent_ids", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("cost_usd", sa.Float, nullable=False, server_default=sa.text("0")),
    )
    op.create_index("ix_verdicts_company_created", "company_verdicts", ["company_id", "created_at"])

    op.create_table(
        "llm_calls",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("purpose", sa.String(48), nullable=False),
        sa.Column("model", sa.String(64), nullable=False),
        sa.Column("input_tokens", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("output_tokens", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("cache_read_tokens", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("cache_write_tokens", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("cost_usd", sa.Numeric(10, 6), nullable=False, server_default=sa.text("0")),
        sa.Column("refused", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("subject_type", sa.String(32)),
        sa.Column("subject_id", sa.String(128)),
        sa.Column("request_id", sa.String(64)),
    )
    op.create_index("ix_llm_calls_created_at", "llm_calls", ["created_at"])
    op.create_index("ix_llm_calls_subject", "llm_calls", ["subject_type", "subject_id"])


def downgrade() -> None:
    op.drop_table("llm_calls")
    op.drop_table("company_verdicts")
    op.drop_table("company_facts")
    op.drop_column("companies", "enriched_at")
    op.drop_column("companies", "reeval_reason")
    op.drop_column("companies", "llm_resolution_tried_at")
