"""Verdicts, overrides and AI spend."""

from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.celery_app import celery_app
from app.db import get_session
from app.models import Company, CompanyVerdict, LLMCall
from app.modules.m1_companies.override import override
from app.modules.m1_companies.queue import judged_today, start_of_today_utc
from app.config_loader import get_config

router = APIRouter()


def verdict_dict(v: CompanyVerdict, company: Company | None = None) -> dict:
    out = {
        "id": v.id, "at": v.created_at, "passed": v.passed, "profile": v.profile,
        "failed_check": v.failed_check, "summary": v.summary, "permanent": v.permanent,
        "trigger": v.trigger, "decided_by": v.decided_by, "checks": v.checks,
        "judgment": v.judgment, "cost_usd": v.cost_usd,
    }
    if company is not None:
        out["company"] = {"id": company.id, "name": company.name, "domain": company.domain}
    return out


@router.get("/verdicts")
def list_verdicts(
    passed: bool | None = None,
    decided_by: Literal["machine", "owner"] | None = None,
    limit: int = Query(50, le=500),
    offset: int = 0,
    session: Session = Depends(get_session),
) -> list[dict]:
    stmt = select(CompanyVerdict, Company).join(Company, Company.id == CompanyVerdict.company_id)
    if passed is not None:
        stmt = stmt.where(CompanyVerdict.passed.is_(passed))
    if decided_by:
        stmt = stmt.where(CompanyVerdict.decided_by == decided_by)
    rows = session.execute(stmt.order_by(CompanyVerdict.created_at.desc()).limit(limit).offset(offset)).all()
    return [verdict_dict(v, c) for v, c in rows]


class OverrideIn(BaseModel):
    decision: Literal["pass", "fail"]
    reason: str
    permanent: bool = False  # thumbs-down: never auto-revive


@router.post("/companies/{company_id}/override")
def override_company(company_id: int, body: OverrideIn, session: Session = Depends(get_session)) -> dict:
    company = session.get(Company, company_id)
    if company is None:
        raise HTTPException(404, "company not found")
    v = override(session, company, body.decision, body.reason, permanent=body.permanent)
    return verdict_dict(v, company)


@router.post("/companies/{company_id}/evaluate", status_code=202)
def evaluate_now(company_id: int, session: Session = Depends(get_session)) -> dict:
    """Re-run the evaluator for one company now (outside the daily limit)."""
    if session.get(Company, company_id) is None:
        raise HTTPException(404, "company not found")
    task = celery_app.send_task("m1.evaluate_batch_ids", args=[[company_id]])
    return {"queued": company_id, "task_id": task.id}


@router.get("/llm/usage")
def llm_usage(session: Session = Depends(get_session)) -> dict:
    now = datetime.now(timezone.utc)

    def spend(since: datetime) -> dict:
        row = session.execute(
            select(func.count(), func.coalesce(func.sum(LLMCall.cost_usd), 0))
            .where(LLMCall.created_at >= since)
        ).one()
        return {"calls": row[0], "cost_usd": round(float(row[1]), 4)}

    by_purpose = session.execute(
        select(LLMCall.purpose, func.count(), func.sum(LLMCall.cost_usd))
        .where(LLMCall.created_at >= now - timedelta(days=30)).group_by(LLMCall.purpose)
    ).all()
    return {
        "today": spend(start_of_today_utc()),
        "last_30_days": spend(now - timedelta(days=30)),
        "by_purpose_30d": {p: {"calls": n, "cost_usd": round(float(c or 0), 4)} for p, n, c in by_purpose},
        "companies_judged_today": judged_today(session),
        "daily_limit": get_config().rules.evaluation.max_new_per_day,
    }
