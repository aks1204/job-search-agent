"""foundation: pgvector, memory_entries, audit_log

Revision ID: 0001
Revises:
Create Date: 2026-10-04
"""

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import JSONB

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

EMBEDDING_DIM = 384  # frozen copy of app.constants.EMBEDDING_DIM at the time of this migration
EMPTY_JSON = sa.text("'{}'::jsonb")


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "memory_entries",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("meaning", sa.Text, nullable=False),
        sa.Column("embedding", Vector(EMBEDDING_DIM)),
        sa.Column("content", JSONB, nullable=False, server_default=EMPTY_JSON),
        sa.Column("context", JSONB, nullable=False, server_default=EMPTY_JSON),
        sa.Column("provenance", JSONB, nullable=False, server_default=EMPTY_JSON),
        sa.Column("evergreen", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("usage_count", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("last_used_at", sa.DateTime(timezone=True)),
        sa.Column("status", sa.String(20), nullable=False, server_default=sa.text("'active'")),
        sa.Column("superseded_by_id", sa.BigInteger, sa.ForeignKey("memory_entries.id")),
        sa.Column("pinned", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("kind IN ('answer', 'precedent', 'taste_signal')", name="ck_memory_kind"),
        sa.CheckConstraint(
            "status IN ('active', 'needs_reconfirm', 'superseded', 'archived')", name="ck_memory_status"
        ),
    )
    op.create_index("ix_memory_kind_status", "memory_entries", ["kind", "status"])
    op.create_index(
        "ix_memory_embedding_hnsw",
        "memory_entries",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )

    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("module", sa.String(32), nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("subject_type", sa.String(32)),
        sa.Column("subject_id", sa.String(128)),
        sa.Column("summary", sa.Text, nullable=False),
        sa.Column("detail", JSONB, nullable=False, server_default=EMPTY_JSON),
        sa.Column("is_judgment", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("rules_version", sa.String(64)),
        sa.Column("overridden_at", sa.DateTime(timezone=True)),
        sa.Column("override_memory_id", sa.BigInteger, sa.ForeignKey("memory_entries.id")),
    )
    op.create_index("ix_audit_created_at", "audit_log", ["created_at"])
    op.create_index("ix_audit_subject", "audit_log", ["subject_type", "subject_id"])
    op.create_index("ix_audit_module_action", "audit_log", ["module", "action"])


def downgrade() -> None:
    op.drop_table("audit_log")
    op.drop_table("memory_entries")
