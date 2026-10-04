# Import every model here so Alembic sees the full metadata.
from app.models.audit import AuditLog
from app.models.memory import MemoryEntry

__all__ = ["AuditLog", "MemoryEntry"]
