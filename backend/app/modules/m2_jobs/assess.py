"""Part 2b batch: one AI read per sibling cluster, then the rules. Daily-capped, re-run on change only."""

import hashlib
import json
import logging
from collections import Counter

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.config_loader import get_config
from app.core.llm import LLM, LLMError
from app.db import session_scope
from app.models.assessment import JobAssessment
from app.models.company import Company
from app.models.job import JobCluster, JobPosting
from app.models.llm_call import LLMCall
from app.modules.m1_companies.override import latest_verdict
from app.modules.m1_companies.queue import start_of_today_utc
from app.modules.m2_jobs.rates import factors
from app.modules.m2_jobs.reader import SYSTEM, candidate_brief, job_brief, read_model
from app.modules.m2_jobs.rules import Context, assess

log = logging.getLogger(__name__)

READY_DESCRIPTIONS = ("present", "fetched", "unavailable", "failed")  # i.e. not still waiting for a fetch


def _hash(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:32]


def profile_version() -> str:
    """What the AI is told about the candidate. Changes here need a new (paid) read."""
    owner = get_config().owner
    return _hash([SYSTEM, candidate_brief(owner) if owner else ""])


def rules_version() -> str:
    """Everything the free rules use. Changes here only re-apply rules to the stored read."""
    cfg = get_config()
    owner = cfg.owner.model_dump(mode="json") if cfg.owner else {}
    return _hash([owner, cfg.job_rules.kind2.model_dump(mode="json"), cfg.job_rules.pay.model_dump(mode="json")])


def _evaluate(session: Session, job: JobPosting, read: dict):
    """Run the rules on a read. Pure lookups only — call before the row is (re)attached to the session."""
    cfg = get_config()
    company = session.get(Company, job.company_id)
    return assess(read, cfg.owner, cfg.job_rules, _context(session, company, job), factors(session))


def _apply(session: Session, row: JobAssessment, job: JobPosting, read: dict) -> None:
    outcome = _evaluate(session, job, read)
    row.outcome, row.reasons, row.flags = outcome.outcome, outcome.reasons, outcome.flags
    row.penalties, row.comp, row.level = outcome.penalties, outcome.comp, outcome.level
    row.rules_version = rules_version()


def reapply_rules(session: Session, limit: int = 2000) -> int:
    """Free: re-run the rules on stored reads after a rules/profile-value edit (no AI call)."""
    current = rules_version()
    rows = session.execute(
        select(JobAssessment, JobPosting).join(JobPosting, JobPosting.id == JobAssessment.job_id)
        .where(JobAssessment.rules_version != current).limit(limit)
    ).all()
    for row, job in rows:
        _apply(session, row, job, row.read)
    return len(rows)


def reads_today(session: Session) -> int:
    """Paid AI reads today (free rule re-applications don't count)."""
    return session.scalar(select(func.count()).select_from(LLMCall).where(
        LLMCall.purpose == "m2.read_job", LLMCall.created_at >= start_of_today_utc())) or 0


def due_clusters(session: Session, version: str, limit: int) -> list[tuple[JobCluster, JobPosting]]:
    """Clusters whose canonical posting passed Kind 1 and has a settled description, and that have no
    assessment yet, or one made for different content or a different profile."""
    canonical = JobPosting.id == JobCluster.canonical_job_id
    rows = session.execute(
        select(JobCluster, JobPosting)
        .join(JobPosting, canonical)
        .outerjoin(JobAssessment, JobAssessment.cluster_id == JobCluster.id)
        .where(JobPosting.status == "open", JobPosting.filter_status == "passed",
               JobPosting.description_status.in_(READY_DESCRIPTIONS),
               or_(JobAssessment.id.is_(None), JobAssessment.content_hash != JobPosting.content_hash,
                   JobAssessment.profile_version != version))
        .order_by(JobPosting.lead_only, JobPosting.is_india.desc(), JobPosting.first_seen_at.desc())
        .limit(limit)
    ).all()
    return [(c, j) for c, j in rows]


def _context(session: Session, company: Company, job: JobPosting) -> Context:
    verdict = latest_verdict(session, company.id)
    brand_up = bool(verdict and verdict.passed and (verdict.judgment or {}).get("direction") == "up")
    return Context(company_domain=company.domain, brand_step_up=brand_up,
                   job_is_india=job.is_india, job_is_remote=job.is_remote, stated_salary=job.salary)


def assess_one(session: Session, llm, cluster: JobCluster, job: JobPosting, version: str) -> JobAssessment:
    cfg = get_config()
    owner = cfg.owner
    company = session.get(Company, job.company_id)
    schema = read_model(tuple(owner.level_ladder))
    result = llm.structured(
        "fast", system=SYSTEM + "\n\nThe candidate:\n" + candidate_brief(owner), schema=schema,
        purpose="m2.read_job", user=job_brief(company.name, job), session=session, subject=("job", str(job.id)),
    )
    read = result.parsed.model_dump(mode="json")

    # Evaluate BEFORE adding a new row: the rule lookups autoflush, and a half-filled row would be
    # inserted with empty NOT NULL columns.
    outcome = _evaluate(session, job, read)
    row = session.scalar(select(JobAssessment).where(JobAssessment.cluster_id == cluster.id))
    if row is None:
        row = JobAssessment(cluster_id=cluster.id)
    row.job_id, row.content_hash, row.profile_version = job.id, job.content_hash, version
    row.read, row.lead_only, row.cost_usd = read, job.lead_only, round(result.cost_usd, 6)
    row.outcome, row.reasons, row.flags = outcome.outcome, outcome.reasons, outcome.flags
    row.penalties, row.comp, row.level = outcome.penalties, outcome.comp, outcome.level
    row.rules_version = rules_version()
    session.add(row)
    session.flush()
    return row


def run_assessments(*, limit: int | None = None, force: bool = False) -> dict:
    cfg = get_config()
    rules = cfg.job_rules.kind2
    if cfg.owner is None or not cfg.owner.level_ladder:
        return {"skipped": "owner_stub.yaml needs level_ladder / current_level for the AI read"}
    if not LLM.available():
        return {"skipped": "ANTHROPIC_API_KEY not set"}
    version = profile_version()
    counts: Counter = Counter()
    with session_scope() as s:
        if reapplied := reapply_rules(s):
            counts["rules_reapplied"] = reapplied
    with session_scope() as s:
        remaining = rules.max_reads_per_day - reads_today(s)
        n = limit or rules.reads_per_tick
        if not force:
            if remaining <= 0:
                return {"skipped": f"daily read limit reached ({rules.max_reads_per_day})"}
            n = min(n, remaining)
        due = [(c.id, j.id) for c, j in due_clusters(s, version, n)]

    llm = LLM()
    cost = 0.0
    errors = 0
    for cluster_id, job_id in due:
        try:
            with session_scope() as s:
                row = assess_one(s, llm, s.get(JobCluster, cluster_id), s.get(JobPosting, job_id), version)
                counts[row.outcome] += 1
                cost += row.cost_usd
        except Exception as e:  # one bad job must not stop the round (LLMError, data issues, …)
            errors += 1
            counts["errors"] += 1
            log.warning("job read failed for cluster %s: %s", cluster_id, e if isinstance(e, LLMError) else repr(e))
            if errors >= 3:
                counts["stopped_after_errors"] = 1
                break
    return {**counts, "cost_usd": round(cost, 4)}
