"""A company's brand level relative to the owner's career: up | lateral | down | unknown.

The owner's own call (`cli brand <domain> up|lateral|down "<reason>"`) wins; otherwise the most
recent Module 1 brand judgment. Used by the lateral rule, the services penalty and scoring.
"""

from typing import Literal

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core import audit
from app.core.memory import remember
from app.models.assessment import JobAssessment
from app.models.company import Company
from app.models.job import JobPosting
from app.models.verdict import CompanyVerdict
from app.modules.m1_companies.facts import fact_card, upsert_fact, value

Level = Literal["up", "lateral", "down"]
OWNER_FACT = "owner_brand_level"


def brand_level(session: Session, company: Company) -> str:
    owner_call = value(fact_card(session, company.id), OWNER_FACT)
    if owner_call in ("up", "lateral", "down"):
        return owner_call
    for verdict in session.scalars(
        select(CompanyVerdict).where(CompanyVerdict.company_id == company.id, CompanyVerdict.decided_by == "machine")
        .order_by(CompanyVerdict.created_at.desc(), CompanyVerdict.id.desc())
    ):
        direction = (verdict.judgment or {}).get("direction")
        if direction in ("up", "lateral", "down"):
            return direction
    return "unknown"


def set_brand(session: Session, company: Company, level: Level, reason: str) -> int:
    """Record the owner's brand call; returns how many job assessments will be re-checked (free)."""
    reason = reason.strip() or "no reason given"
    # Stored as a verified fact: Module 1 refreshes can't overwrite it (facts never downgrade).
    upsert_fact(session, company.id, OWNER_FACT, level, "verified", "owner")
    what = value(fact_card(session, company.id), "what_they_build")
    remember(
        session, "precedent",
        f"{company.name} (established; {what}). Owner rates its brand as a {level.upper()} move vs their "
        f"current employer — {reason}",
        {"brand_level": level, "reason": reason},
        context={"module": "m1", "profile": "established", "company_id": company.id, "domain": company.domain},
        provenance={"source": "owner_brand_call"}, evergreen=True,
    )
    audit.record(session, "m2", "brand_set", f"You rated {company.name}'s brand: {level} — {reason}",
                 subject_type="company", subject_id=str(company.id), detail={"level": level, "reason": reason})
    # Job rules read the brand level: mark this company's assessments for a free re-apply.
    job_ids = select(JobPosting.id).where(JobPosting.company_id == company.id)
    result = session.execute(update(JobAssessment).where(JobAssessment.job_id.in_(job_ids))
                             .values(rules_version="brand-changed").execution_options(synchronize_session=False))
    return result.rowcount or 0
