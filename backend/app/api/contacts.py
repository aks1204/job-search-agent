"""Module 3: role channels, company benches (3a) and drafts (3b). Read-only; nothing here sends email."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_session
from app.models import Bench, Company, RoleChannel
from app.modules.m3_outreach.addresses import best_address
from app.modules.m3_outreach.bench import members, open_slots

router = APIRouter()


def _bench(session: Session, bench: Bench) -> dict:
    people = []
    for m, c in members(session, bench):
        addr = best_address(session, c)
        people.append({
            "rank": m.rank, "slot": m.slot_kind, "name": c.full_name, "title": c.title, "location": c.location,
            "status": "frozen" if m.co_suspects else c.status, "linkedin_url": c.linkedin_url,
            "email": addr.email if addr else None, "confidence": addr.confidence if addr else None,
            "address_source": addr.source if addr else None, "vouched": addr.vouched if addr else False,
        })
    return {"id": bench.id, "key": bench.key, "shape": bench.shape, "open_slots": open_slots(session, bench),
            "filled_at": bench.filled_at, "people": people}


@router.get("/roles/{cluster_id}/channel")
def role_channel(cluster_id: int, session: Session = Depends(get_session)) -> dict:
    ch = session.get(RoleChannel, cluster_id)
    if ch is None:
        raise HTTPException(404, "no channel decided for this role yet")
    out = {"cluster_id": cluster_id, "company": session.get(Company, ch.company_id).name, "mode": ch.mode,
           "manual": ch.manual, "address": ch.designated_email, "also": ch.also_emails or [],
           "context": ch.designated_context, "note": ch.note,
           "next_retry_at": ch.next_retry_at, "bench": None}
    if ch.bench_id and ch.mode != "designated":
        out["bench"] = _bench(session, session.get(Bench, ch.bench_id))
    return out


@router.get("/companies/{company_id}/benches")
def company_benches(company_id: int, session: Session = Depends(get_session)) -> list[dict]:
    return [_bench(session, b) for b in session.scalars(select(Bench).where(Bench.company_id == company_id)).all()]


@router.get("/roles/{cluster_id}/draft")
def role_draft(cluster_id: int, session: Session = Depends(get_session)) -> dict:
    """The role's one draft (Part 3b). `body` still holds {greeting}; `preview` is how the first recipient
    would get it."""
    from app.models import OutreachDraft
    from app.modules.m3_outreach.drafting import render_for
    from app.modules.m3_outreach.template import load_template

    d = session.scalar(select(OutreachDraft).where(OutreachDraft.cluster_id == cluster_id))
    if d is None:
        raise HTTPException(404, "no draft for this role yet")
    _, preview = render_for(d, load_template(), None)
    return {"cluster_id": cluster_id, "version": d.version, "scenario": d.scenario, "status": d.status,
            "subject": d.subject, "body": d.body, "preview": preview, "points": d.points, "req_ids": d.req_ids,
            "attach_resume": d.attach_resume, "truth": d.truth, "cost_usd": d.cost_usd, "updated_at": d.updated_at}
