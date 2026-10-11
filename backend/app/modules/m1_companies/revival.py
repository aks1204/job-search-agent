"""Rejected companies can return (spec §1.4). One re-evaluation per trigger:

(a) a fact relevant to the failure reason changes — e.g. a startup rejected for
    viability shows up hiring on a new job board;
(b) the rules section behind the failure reason changes;
(c) owner override (handled in override.py).

Permanent grounds (category blocklist, owner thumbs-down) never auto-revive.
"""

from collections import Counter

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config_loader import get_config
from app.core import audit
from app.models.company import STATUS_CANDIDATE, STATUS_REJECTED, Company
from app.modules.m1_companies.checks import SECTION_OF
from app.modules.m1_companies.override import latest_verdict

# Fresh sightings from these scouts are hiring signals.
HIRING_SCOUTS = {"yc", "hn", "adzuna", "jooble", "remotive"}


def _requeue(session: Session, company: Company, reason: str, why: str) -> None:
    company.status = STATUS_CANDIDATE
    company.reeval_reason = reason
    audit.record(
        session, "m1", "company_requeued", f"Re-evaluating {company.name}: {why}",
        subject_type="company", subject_id=str(company.id), detail={"trigger": reason},
    )


def on_new_sighting(session: Session, company: Company, scout: str) -> bool:
    """Trigger (a): a startup rejected as 'not viable' is seen hiring again."""
    if company.status != STATUS_REJECTED or scout not in HIRING_SCOUTS:
        return False
    verdict = latest_verdict(session, company.id)
    if verdict is None or verdict.permanent or verdict.decided_by != "machine" or verdict.failed_check != "viability":
        return False
    _requeue(session, company, "refresh", f"new hiring signal from {scout}")
    return True


# Facts each failure reason depends on: a change in one of these earns a re-check.
RELEVANT_FACTS = {
    "avoid_list": {"business_model"},
    "geography": {"india_presence", "hires_remote_from_india", "sponsors_visa"},
    "viability": {"actively_hiring", "raised_within_window", "distress"},
    "novelty": {"what_they_build", "categories"},
    "brand": {"employee_estimate", "company_stage"},
}


def on_facts_changed(session: Session, company: Company, changed: set[str]) -> bool:
    """Trigger (a), refresh path: a fact behind the failure reason changed."""
    if company.status != STATUS_REJECTED or not changed:
        return False
    verdict = latest_verdict(session, company.id)
    if verdict is None or verdict.permanent or verdict.decided_by != "machine":
        return False
    hit = changed & RELEVANT_FACTS.get(verdict.failed_check or "", set())
    if not hit:
        return False
    _requeue(session, company, "refresh", "fact card changed: " + ", ".join(sorted(hit)))
    return True


def check_rules_changes(session: Session) -> Counter:
    """Trigger (b): requeue machine rejections whose deciding rules section has changed."""
    cfg = get_config()
    current = {s: cfg.rules_section_version(s) for s in ("shared", "startup", "established")}
    counts: Counter = Counter()
    for company in session.scalars(select(Company).where(Company.status == STATUS_REJECTED)).all():
        verdict = latest_verdict(session, company.id)
        if verdict is None or verdict.permanent or verdict.decided_by != "machine":
            continue
        section = SECTION_OF.get(verdict.failed_check or "")
        if section and verdict.rules_versions.get(section) != current[section]:
            _requeue(session, company, "rules_change", f"the '{section}' rules changed")
            counts["requeued"] += 1
    return counts
