"""Owner overrides (spec §1.1): one tap flips a verdict and becomes a precedent for future judgments."""

from datetime import datetime, timezone
from typing import Literal

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core import audit
from app.core.memory import remember
from app.models import AuditLog
from app.models.company import STATUS_BLOCKED, STATUS_PASSED, STATUS_REJECTED, Company
from app.models.verdict import CompanyVerdict
from app.modules.m1_companies.facts import fact_card, value


def latest_verdict(session: Session, company_id: int) -> CompanyVerdict | None:
    return session.scalars(
        select(CompanyVerdict).where(CompanyVerdict.company_id == company_id)
        .order_by(CompanyVerdict.created_at.desc(), CompanyVerdict.id.desc()).limit(1)
    ).first()


def override(
    session: Session, company: Company, decision: Literal["pass", "fail"], reason: str, *, permanent: bool = False
) -> CompanyVerdict:
    reason = reason.strip() or "no reason given"
    previous = latest_verdict(session, company.id)
    profile = previous.profile if previous else (company.type if company.type != "unknown" else "startup")
    permanent = permanent and decision == "fail"
    what = value(fact_card(session, company.id), "what_they_build")

    precedent = remember(
        session,
        "precedent",
        f"{company.name} ({profile}; {what}). Owner decision: {'KEEP' if decision == 'pass' else 'REJECT'} — {reason}",
        {"decision": decision, "reason": reason,
         "machine_said": None if previous is None else {"passed": previous.passed, "summary": previous.summary}},
        context={"module": "m1", "profile": profile, "company_id": company.id, "domain": company.domain},
        provenance={"source": "owner_override", "previous_verdict_id": previous.id if previous else None},
        evergreen=True,
    )

    verdict = CompanyVerdict(
        company_id=company.id, passed=decision == "pass", profile=profile,
        failed_check=None if decision == "pass" else "owner_override",
        summary=f"Owner {'kept' if decision == 'pass' else 'rejected'}: {reason}",
        checks=[{"name": "owner_override", "outcome": decision, "reason": reason, "permanent": permanent}],
        judgment={}, permanent=permanent, trigger="override", decided_by="owner",
        rules_versions=previous.rules_versions if previous else {}, precedent_ids=[precedent.id],
    )
    session.add(verdict)
    session.flush()
    company.status = STATUS_PASSED if decision == "pass" else (STATUS_BLOCKED if permanent else STATUS_REJECTED)
    company.reeval_reason = None

    now = datetime.now(timezone.utc)
    session.execute(
        update(AuditLog)
        .where(AuditLog.subject_type == "company", AuditLog.subject_id == str(company.id),
               AuditLog.action == "company_verdict", AuditLog.overridden_at.is_(None))
        .values(overridden_at=now, override_memory_id=precedent.id)
    )
    audit.record(
        session, "m1", "verdict_overridden",
        f"You {'kept' if decision == 'pass' else 'rejected'} {company.name}"
        + (" permanently" if permanent else "") + f": {reason}",
        subject_type="company", subject_id=str(company.id),
        detail={"verdict_id": verdict.id, "precedent_id": precedent.id, "permanent": permanent},
    )
    return verdict
