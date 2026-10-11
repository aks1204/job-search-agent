"""Watchlist feeds and the raw postings they produce."""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.celery_app import celery_app
from app.db import get_session
from app.models import Company, CompanyFeed, RawPosting

router = APIRouter()


def feed_dict(f: CompanyFeed) -> dict:
    return {
        "id": f.id, "method": f.method, "slug": f.slug, "site": f.site, "careers_url": f.careers_url,
        "health": f.health, "cadence": f.cadence, "open_jobs": f.open_jobs,
        "ownership_evidence": f.ownership_evidence, "verified_at": f.verified_at,
        "last_sync_at": f.last_sync_at, "last_success_at": f.last_success_at, "last_change_at": f.last_change_at,
        "next_sync_at": f.next_sync_at, "consecutive_failures": f.consecutive_failures, "last_error": f.last_error,
        "needs_reactivation": f.needs_reactivation,
    }


@router.get("/feeds")
def list_feeds(
    health: str | None = None,
    method: str | None = None,
    limit: int = Query(100, le=1000),
    session: Session = Depends(get_session),
) -> dict:
    q = select(CompanyFeed, Company).join(Company, Company.id == CompanyFeed.company_id)
    if health:
        q = q.where(CompanyFeed.health == health)
    if method:
        q = q.where(CompanyFeed.method == method)
    rows = session.execute(q.order_by(Company.name).limit(limit)).all()
    by_method = dict(session.execute(select(CompanyFeed.method, func.count()).group_by(CompanyFeed.method)).all())
    by_health = dict(session.execute(select(CompanyFeed.health, func.count()).group_by(CompanyFeed.health)).all())
    return {
        "summary": {"by_method": by_method, "by_health": by_health},
        "items": [{**feed_dict(f), "company": {"id": c.id, "name": c.name, "domain": c.domain, "status": c.status}}
                  for f, c in rows],
    }


@router.get("/coverage")
def coverage_report(session: Session = Depends(get_session)) -> dict:
    """How kept companies' jobs are read, and which job-system reader would close the most gaps."""
    from app.modules.m1_companies.watch.coverage import coverage

    return coverage(session)


@router.get("/postings")
def list_postings(
    company_id: int | None = None,
    open_only: bool = True,
    include_leads: bool = False,
    limit: int = Query(100, le=1000),
    offset: int = 0,
    session: Session = Depends(get_session),
) -> list[dict]:
    q = select(RawPosting, Company).join(Company, Company.id == RawPosting.company_id)
    if company_id is not None:
        q = q.where(RawPosting.company_id == company_id)
    if open_only:
        q = q.where(RawPosting.closed_at.is_(None))
    if not include_leads:
        q = q.where(RawPosting.lead_only.is_(False))
    rows = session.execute(q.order_by(RawPosting.first_seen_at.desc()).limit(limit).offset(offset)).all()
    return [
        {"id": p.id, "company": c.name, "title": p.title, "location": p.location, "url": p.url,
         "department": p.department, "posted_at": p.posted_at, "first_seen_at": p.first_seen_at,
         "closed_at": p.closed_at, "lead_only": p.lead_only}
        for p, c in rows
    ]


@router.post("/companies/{company_id}/activate", status_code=202)
def activate_now(company_id: int, session: Session = Depends(get_session)) -> dict:
    """(Re-)run careers discovery + feed setup for one company."""
    if session.get(Company, company_id) is None:
        raise HTTPException(404, "company not found")
    task = celery_app.send_task("m1.activate_batch", args=[[company_id]])
    return {"queued": company_id, "task_id": task.id}
