from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.celery_app import celery_app
from app.db import get_session
from app.modules.m1_companies.health import scout_health
from app.modules.m1_companies.scouts import SCOUTS

router = APIRouter()


@router.get("/scouts")
def list_scouts(session: Session = Depends(get_session)) -> list[dict]:
    return scout_health(session)


@router.post("/scouts/{name}/run", status_code=202)
def run_scout_now(name: str) -> dict:
    if name not in SCOUTS:
        raise HTTPException(404, f"unknown scout; known: {', '.join(SCOUTS)}")
    task = celery_app.send_task("m1.run_scout", args=[name])
    return {"queued": name, "task_id": task.id}
