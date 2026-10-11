# Import every model here so Alembic sees the full metadata.
from app.models.audit import AuditLog
from app.models.company import Company, CompanySighting, ContactLead
from app.models.memory import MemoryEntry
from app.models.scout import ScoutRun, ScoutState

__all__ = ["AuditLog", "Company", "CompanySighting", "ContactLead", "MemoryEntry", "ScoutRun", "ScoutState"]
