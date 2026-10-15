"""Module 2 job postings (Part 2a: normalised + Kind-1 filtered)."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_session
from app.models import Company, JobPosting
from app.modules.m2_jobs.pipeline import funnel

router = APIRouter()


@router.get("/jobs")
def list_jobs(
    filter_status: str | None = Query("passed", description="passed | failed | pending"),
    company_id: int | None = None,
    include_leads: bool = True,
    limit: int = Query(100, le=1000),
    offset: int = 0,
    session: Session = Depends(get_session),
) -> list[dict]:
    q = select(JobPosting, Company).join(Company, Company.id == JobPosting.company_id).where(JobPosting.status == "open")
    if filter_status:
        q = q.where(JobPosting.filter_status == filter_status)
    if company_id is not None:
        q = q.where(JobPosting.company_id == company_id)
    if not include_leads:
        q = q.where(JobPosting.lead_only.is_(False))
    rows = session.execute(q.order_by(JobPosting.first_seen_at.desc()).limit(limit).offset(offset)).all()
    return [
        {"id": j.id, "company": c.name, "title": j.title, "location": j.location, "is_india": j.is_india,
         "is_remote": j.is_remote, "employment_type": j.employment_type, "posted_at": j.posted_at,
         "salary": j.salary, "url": j.url, "lead_only": j.lead_only, "cluster_id": j.cluster_id,
         "filter_status": j.filter_status, "filter_reasons": j.filter_reasons,
         "description_status": j.description_status}
        for j, c in rows
    ]


@router.get("/jobs/funnel")
def jobs_funnel(session: Session = Depends(get_session)) -> dict:
    return funnel(session)
