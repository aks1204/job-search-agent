"""Maintenance tab: how each source of companies is doing, and where it breaks."""

from dataclasses import asdict

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.db import get_session
from app.modules.maintenance.sources import build

router = APIRouter()


@router.get("/maintenance/sources")
def sources(days: int | None = Query(90, ge=1, le=3650, description="look-back window; omit for 90"),
            all_time: bool = False, session: Session = Depends(get_session)) -> dict:
    report = build(session, days=None if all_time else days)
    return {**report, "rows": [{**asdict(r), "healthy": r.healthy} for r in report["rows"]],
            "system": [asdict(w) for w in report["system"]]}
