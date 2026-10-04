"""Open memory layer (spec §5.3): one table + pgvector.

Module 1 uses it for precedents (owner overrides fed back into judgments);
Module 5 builds the full answers / taste-signal behaviour on top.
"""

from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.constants import EMBEDDING_DIM
from app.db import Base

MEMORY_KINDS = ("answer", "precedent", "taste_signal")
MEMORY_STATUSES = ("active", "needs_reconfirm", "superseded", "archived")


class MemoryEntry(Base):
    __tablename__ = "memory_entries"
    __table_args__ = (
        CheckConstraint(f"kind IN {MEMORY_KINDS}", name="ck_memory_kind"),
        CheckConstraint(f"status IN {MEMORY_STATUSES}", name="ck_memory_status"),
        Index("ix_memory_kind_status", "kind", "status"),
        Index(
            "ix_memory_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))
    meaning: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM))
    content: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))
    context: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))
    provenance: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))
    evergreen: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    usage_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), default="active", server_default=text("'active'"))
    superseded_by_id: Mapped[int | None] = mapped_column(ForeignKey("memory_entries.id"))
    pinned: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
