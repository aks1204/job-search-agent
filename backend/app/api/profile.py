"""Module 5a: your profile — the Profile screen's data, the draft read from your resume, and confirming it."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db import get_session
from app.modules.m5_profile import profile as P
from app.modules.m5_profile import store

router = APIRouter()


def _out(row) -> dict | None:
    if row is None:
        return None
    return {"id": row.id, "status": row.status, "data": row.data, "notes": row.notes,
            "confirmed_fields": row.confirmed_fields, "created_at": row.created_at, "confirmed_at": row.confirmed_at,
            "stale": P.stale_fields(row) if row.status == "confirmed" else []}


@router.get("/profile")
def get_profile(session: Session = Depends(get_session)) -> dict:
    return {"confirmed": _out(store.load_confirmed(session)), "draft": _out(store.latest_draft(session))}


@router.post("/profile/read")
def read_profile(session: Session = Depends(get_session)) -> dict:
    from app.core.llm import LLM

    if not LLM.available():
        raise HTTPException(400, "needs ANTHROPIC_API_KEY")
    try:
        return _out(P.make_draft(session, LLM()))
    except ValueError as e:
        raise HTTPException(422, str(e)) from e


class ConfirmIn(BaseModel):
    data: dict


@router.post("/profile/confirm")
def confirm_profile(body: ConfirmIn, session: Session = Depends(get_session)) -> dict:
    try:
        return _out(P.confirm(session, body.data))
    except Exception as e:  # validation problems → shown on the Profile screen
        raise HTTPException(422, str(e)) from e
