"""The evaluation queue: daily cap, priority order, one transaction per company."""

import logging
from collections import Counter
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config_loader import get_config
from app.core import audit
from app.core.http import PoliteClient
from app.core.llm import LLM, LLMUnavailable
from app.db import session_scope
from app.models.company import STATUS_CANDIDATE, Company, CompanySighting
from app.models.verdict import CompanyVerdict
from app.modules.m1_companies.evaluator import evaluate
from app.settings import redact

log = logging.getLogger(__name__)

MAX_CONSECUTIVE_ERRORS = 3


def start_of_today_utc() -> datetime:
    tz = ZoneInfo(get_config().app.timezone)
    local_midnight = datetime.combine(datetime.now(tz).date(), time.min, tzinfo=tz)
    return local_midnight.astimezone(timezone.utc)


def judged_today(session: Session) -> int:
    return session.scalar(
        select(func.count()).select_from(CompanyVerdict).where(
            CompanyVerdict.decided_by == "machine", CompanyVerdict.created_at >= start_of_today_utc()
        )
    ) or 0


def pick_candidates(session: Session, n: int) -> list[int]:
    """Re-evaluations first, then by best source (seeds, hn, yc, …), then oldest; failed ones last."""
    cfg = get_config()
    priority = {s: i for i, s in enumerate(cfg.rules.evaluation.source_priority)}
    stmt = (
        select(Company.id, Company.reeval_reason, Company.first_seen_at, func.array_agg(CompanySighting.scout))
        .join(CompanySighting, CompanySighting.company_id == Company.id)
        .where(Company.status == STATUS_CANDIDATE)
        .group_by(Company.id)
    )
    if cfg.owner is None or cfg.owner.placeholder_fields():
        # Established companies are judged against the owner's real history; wait for it.
        stmt = stmt.where(Company.type != "established")
    rows = session.execute(stmt).all()

    def key(row):
        _, reeval, first_seen, scouts = row
        failed = reeval == "error"
        requeued = reeval is not None and not failed
        best = min((priority.get(s, len(priority)) for s in scouts), default=len(priority))
        return (failed, not requeued, best, first_seen)

    return [row[0] for row in sorted(rows, key=key)[:n]]


def run_batch(*, limit: int | None = None, force: bool = False, company_ids: list[int] | None = None) -> dict:
    rules = get_config().rules.evaluation
    counts: Counter = Counter()
    if not LLM.available():
        return {"skipped": "ANTHROPIC_API_KEY not set"}

    with session_scope() as s:
        remaining = rules.max_new_per_day - judged_today(s)
        if remaining <= 0 and not force and not company_ids:
            return {"skipped": f"daily limit reached ({rules.max_new_per_day})"}
        n = limit or rules.batch_size
        if not force and not company_ids:
            n = min(n, remaining)
        ids = company_ids or pick_candidates(s, n)

    llm = LLM()
    errors_in_a_row = 0
    total_cost = 0.0
    with PoliteClient() as http:
        for company_id in ids:
            trigger = "manual" if company_ids else "new"
            try:
                with session_scope() as s:
                    company = s.get(Company, company_id)
                    if company is None or company.domain is None:
                        continue
                    if company.reeval_reason in ("refresh", "rules_change"):
                        trigger = company.reeval_reason
                    verdict = evaluate(s, company, llm=llm, http=http, trigger=trigger)
                    counts["passed" if verdict.passed else ("blocked" if verdict.permanent else "rejected")] += 1
                    total_cost += verdict.cost_usd
                errors_in_a_row = 0
            except LLMUnavailable:
                counts["skipped_no_key"] += 1
                break
            except Exception as e:  # one company's failure must not stop the queue (LLMError, network, …)
                errors_in_a_row += 1
                counts["errors"] += 1
                log.warning("evaluation of company %s failed: %s", company_id, e)
                with session_scope() as s:
                    company = s.get(Company, company_id)
                    if company is not None:
                        company.reeval_reason = "error"  # moves to the back of the queue
                        audit.record(s, "m1", "evaluation_failed", f"Couldn't evaluate {company.name}: {redact(str(e))[:300]}",
                                     subject_type="company", subject_id=str(company_id))
                if errors_in_a_row >= MAX_CONSECUTIVE_ERRORS:
                    counts["stopped_after_errors"] = 1
                    break
    return {**counts, "cost_usd": round(total_cost, 4)}
