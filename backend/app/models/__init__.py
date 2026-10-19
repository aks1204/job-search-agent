# Import every model here so Alembic sees the full metadata.
from app.models.application import Application, ApplicationEvent, InboxMessage
from app.models.assessment import ExchangeFactor, JobAssessment
from app.models.audit import AuditLog
from app.models.company import Company, CompanySighting, ContactLead
from app.models.contact import Bench, BenchMember, Contact, ContactEmail, EnrichmentLookup, MailDomain, RoleChannel
from app.models.draft import OutreachDraft
from app.models.feed import CompanyFeed, RawPosting
from app.models.job import JobCluster, JobPosting
from app.models.llm_call import LLMCall
from app.models.memory import MemoryEntry
from app.models.notification import Notification
from app.models.outreach import DraftEdit, MailAccount, OutreachCampaign, OutreachEmail, SendBreaker
from app.models.score import JobFeedback, JobScore
from app.models.scout import ScoutRun, ScoutState
from app.models.verdict import CompanyFact, CompanyVerdict

__all__ = [
    "Application", "ApplicationEvent", "AuditLog", "Bench", "BenchMember", "Company", "CompanyFact", "CompanyFeed", "CompanySighting", "CompanyVerdict", "Contact", "ContactEmail",
    "ContactLead", "DraftEdit", "EnrichmentLookup", "MailAccount", "OutreachCampaign", "OutreachEmail", "SendBreaker", "MailDomain", "OutreachDraft", "RoleChannel",
    "ExchangeFactor", "InboxMessage", "JobAssessment", "JobCluster", "JobFeedback", "JobPosting", "JobScore", "LLMCall", "MemoryEntry", "Notification", "RawPosting", "ScoutRun", "ScoutState",
]
