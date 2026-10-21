"""Module 2 job postings (Part 2a: normalised + Kind-1 filtered)."""

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
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


@router.get("/shortlist")
def shortlist(
    band: str | None = Query(None, description="fast_lane | inbox | maybe | archive (default: all but archive)"),
    limit: int = Query(50, le=500),
    session: Session = Depends(get_session),
) -> list[dict]:
    from app.models import JobAssessment, JobCluster, JobScore

    q = (select(JobScore, JobAssessment, JobPosting, Company, JobCluster)
         .join(JobAssessment, JobAssessment.id == JobScore.assessment_id)
         .join(JobPosting, JobPosting.id == JobAssessment.job_id)
         .join(Company, Company.id == JobPosting.company_id)
         .join(JobCluster, JobCluster.id == JobScore.cluster_id))
    q = q.where(JobScore.band == band) if band else q.where(JobScore.band != "archive")
    rows = session.execute(q.order_by(JobScore.score.desc()).limit(limit)).all()
    return [
        {"cluster_id": cl.id, "score": sc.score, "band": sc.band, "graded": sc.graded,
         "company": c.name, "title": j.title, "location": j.location, "url": j.url, "req_ids": cl.size,
         "one_liner": sc.grade.get("one_liner") or a.read.get("one_liner"), "reasons": sc.grade.get("reasons", []),
         "missing": sc.grade.get("missing", []), "flags": a.flags, "comp": a.comp, "level": a.level,
         "breakdown": sc.breakdown, "similarity": round(sc.similarity, 3)}
        for sc, a, j, c, cl in rows
    ]


class FeedbackIn(BaseModel):
    action: str  # act | skip | thumbs_up | thumbs_down | maybe_rescue
    reason: str | None = None
    chips: list[str] = []  # one-tap reasons: too_junior | too_senior | not_my_field | location | company | pay | hours_travel | other


@router.post("/jobs/{cluster_id}/feedback")
def job_feedback(cluster_id: int, body: FeedbackIn, session: Session = Depends(get_session)) -> dict:
    from app.models import JobScore
    from app.modules.m2_jobs.scoring import record_feedback

    row = session.scalar(select(JobScore).where(JobScore.cluster_id == cluster_id))
    if row is None:
        raise HTTPException(404, "no scored role with that cluster id")
    try:
        record_feedback(session, row, body.action, body.reason, chips=body.chips)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    return {"cluster_id": cluster_id, "score": row.score, "band": row.band}
