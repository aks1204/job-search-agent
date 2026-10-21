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


@router.get("/direction")
def get_direction(session: Session = Depends(get_session)) -> dict:
    """Your lists with the agent's reasons, its view of where you are / heading, and open suggestions."""
    from app.modules.m5_profile import direction as D

    return D.explained(session)


class AnswerIn(BaseModel):
    yes: bool


@router.post("/direction/suggestions/{suggestion_id}")
def answer_suggestion(suggestion_id: int, body: AnswerIn, session: Session = Depends(get_session)) -> dict:
    """The [Yes] / [No] buttons on a direction suggestion."""
    from app.modules.m5_profile import direction as D

    try:
        s = D.answer(session, suggestion_id, body.yes)
    except D.DirectionError as e:
        raise HTTPException(422, str(e)) from e
    return {"id": s.id, "status": s.status}


def _mem_out(e) -> dict:
    return {"id": e.id, "kind": e.kind, "meaning": e.meaning, "status": e.status, "pinned": e.pinned,
            "clash": (e.content or {}).get("clash"), "created_at": e.created_at, "last_used_at": e.last_used_at,
            "expires_at": e.expires_at}


@router.get("/memory")
def list_memory(kind: str | None = None, archived: bool = False, session: Session = Depends(get_session)) -> list[dict]:
    from app.modules.m5_profile import memory as M

    return [_mem_out(e) for e in M.listing(session, kind=kind, include_archived=archived)]


@router.get("/memory/search")
def search_memory(q: str, session: Session = Depends(get_session)) -> list[dict]:
    from app.modules.m5_profile import memory as M

    return [{**_mem_out(e), "similarity": round(sim, 3)} for e, sim in M.search_text(session, q)]


@router.get("/memory/beliefs")
def memory_beliefs(session: Session = Depends(get_session)) -> dict:
    """'What the agent believes about you' (cached until your memories change)."""
    from app.core.llm import LLM
    from app.modules.m5_profile import memory as M

    if not LLM.available():
        raise HTTPException(400, "needs ANTHROPIC_API_KEY")
    return M.beliefs(session, LLM()).model_dump()


@router.post("/memory/{entry_id}/{action}")
def memory_action(entry_id: int, action: str, session: Session = Depends(get_session)) -> dict:
    """pin | unpin | forget | restore | keep (the Memory screen's buttons)."""
    from app.modules.m5_profile import memory as M

    try:
        return _mem_out(M.act(session, entry_id, action))
    except M.MemoryError as e:
        raise HTTPException(422, str(e)) from e
