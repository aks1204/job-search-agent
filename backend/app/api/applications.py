"""Module 4: the apply list, applications and each role's story (outreach + application)."""

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_session
from app.models import Application, Company, JobCluster
from app.modules.m4_apply import tracker as T

router = APIRouter()


def _app_out(app: Application, cluster: JobCluster, company: Company) -> dict:
    return {"cluster_id": cluster.id, "company": company.name, "title": cluster.title, "status": app.status,
            "on_list": app.on_list, "links": app.links, "links_checked_at": app.links_checked_at,
            "queued_at": app.queued_at, "applied_at": app.applied_at, "applied_how": app.applied_how,
            "receipt_link": app.receipt_link, "status_at": app.status_at, "note": app.note}


@router.get("/apply-list")
def get_apply_list(session: Session = Depends(get_session)) -> dict:
    """Full cards (recent) + how many older ones are 'still pending' — the summary mail's apply section."""
    full, folded = T.apply_list(session)
    return {"cards": [_app_out(a, cl, co) for a, cl, co in full], "still_pending": folded}


@router.get("/applications")
def list_applications(status: str | None = Query(None), session: Session = Depends(get_session)) -> list[dict]:
    q = select(Application, JobCluster, Company).join(JobCluster, JobCluster.id == Application.cluster_id) \
        .join(Company, Company.id == Application.company_id).order_by(Application.status_at.desc())
    if status:
        q = q.where(Application.status == status)
    return [_app_out(a, cl, co) for a, cl, co in session.execute(q.limit(200)).all()]


class NoteIn(BaseModel):
    note: str | None = None


@router.post("/applications/{cluster_id}/applied")
def applied(cluster_id: int, body: NoteIn | None = None, session: Session = Depends(get_session)) -> dict:
    try:
        app = T.mark_applied(session, cluster_id, body.note if body else None)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    return {"cluster_id": cluster_id, "status": app.status, "applied_at": app.applied_at}


@router.post("/applications/{cluster_id}/skip")
def skip(cluster_id: int, body: NoteIn | None = None, session: Session = Depends(get_session)) -> dict:
    try:
        app = T.skip(session, cluster_id, (body and body.note) or "skipped")
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    return {"cluster_id": cluster_id, "status": app.status}


@router.get("/roles/{cluster_id}/story")
def role_story(cluster_id: int, session: Session = Depends(get_session)) -> list[dict]:
    return [{"at": at, "kind": kind, "text": text} for at, kind, text in T.story(session, cluster_id)]
