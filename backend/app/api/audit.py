from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_session
from app.models import AuditLog

router = APIRouter()


@router.get("/audit")
def list_audit(
    module: str | None = None,
    action: str | None = None,
    subject_type: str | None = None,
    subject_id: str | None = None,
    judgments_only: bool = False,
    limit: int = Query(50, le=500),
    offset: int = 0,
    session: Session = Depends(get_session),
) -> list[dict]:
    stmt = select(AuditLog)
    for column, value in (
        (AuditLog.module, module), (AuditLog.action, action),
        (AuditLog.subject_type, subject_type), (AuditLog.subject_id, subject_id),
    ):
        if value is not None:
            stmt = stmt.where(column == value)
    if judgments_only:
        stmt = stmt.where(AuditLog.is_judgment.is_(True))
    rows = session.scalars(stmt.order_by(AuditLog.created_at.desc()).limit(limit).offset(offset)).all()
    return [
        {"id": a.id, "at": a.created_at, "module": a.module, "action": a.action, "summary": a.summary,
         "subject_type": a.subject_type, "subject_id": a.subject_id, "detail": a.detail,
         "is_judgment": a.is_judgment, "overridden_at": a.overridden_at}
        for a in rows
    ]
