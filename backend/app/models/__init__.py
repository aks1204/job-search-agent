# Import every model here so Alembic sees the full metadata.
from app.models.audit import AuditLog
from app.models.company import Company, CompanySighting, ContactLead
from app.models.feed import CompanyFeed, RawPosting
from app.models.job import JobCluster, JobPosting
from app.models.llm_call import LLMCall
from app.models.memory import MemoryEntry
from app.models.scout import ScoutRun, ScoutState
from app.models.verdict import CompanyFact, CompanyVerdict

__all__ = [
    "AuditLog", "Company", "CompanyFact", "CompanyFeed", "CompanySighting", "CompanyVerdict", "ContactLead",
    "JobCluster", "JobPosting", "LLMCall", "MemoryEntry", "RawPosting", "ScoutRun", "ScoutState",
]
