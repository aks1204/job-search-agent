"""Module 3 Part 3c: connect Gmail (Google sign-in) and the review flow (Approve / Edit / Skip per email)."""

from html import escape

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_session
from app.models import Company, OutreachCampaign
from app.modules.m3_outreach import campaigns as C
from app.modules.m3_outreach import gmail
from app.modules.m3_outreach.template import load_template

router = APIRouter()


@router.get("/gmail/connect")
def gmail_connect() -> RedirectResponse:
    try:
        return RedirectResponse(gmail.auth_url())
    except gmail.GmailError as e:
        raise HTTPException(400, str(e)) from e


@router.get("/gmail/callback", response_class=HTMLResponse)
def gmail_callback(code: str | None = None, state: str | None = None, error: str | None = None,
                   session: Session = Depends(get_session)) -> str:
    if error or not code:
        return f"<h3>Gmail was not connected</h3><p>{escape(error or 'no code returned')}. You can close this tab and try again.</p>"
    try:
        acct = gmail.finish_connect(session, code, state or "")
    except Exception as e:
        return f"<h3>Gmail was not connected</h3><p>{escape(str(e)[:300])}</p>"
    return (f"<h3>Gmail connected: {escape(acct.email)}</h3><p>The agent can now send the emails you approve from this "
            "account, and read only the threads it started. You can close this tab.</p>")


@router.get("/gmail/status")
def gmail_status(session: Session = Depends(get_session)) -> dict:
    acct = gmail.account(session)
    return {"configured": gmail.configured(), "connected": bool(acct and acct.status == "ok"),
            "email": acct.email if acct else None, "status": acct.status if acct else None}


def _campaign_out(session: Session, c: OutreachCampaign) -> dict:
    t = load_template()
    out = {"id": c.id, "cluster_id": c.cluster_id, "company": session.get(Company, c.company_id).name,
           "status": c.status, "status_reason": c.status_reason, "edited": c.edited, "subject": c.subject,
           "body": c.body, "attach_resume": c.attach_resume, "armed_at": c.armed_at, "emails": []}
    for e in C.emails(session, c):
        subject, body = C.render_email(t, c, e)
        out["emails"].append({"id": e.id, "position": e.position, "wave": e.wave + 1, "to": e.to_email,
                              "to_name": e.to_name, "confidence": e.confidence, "vouched": e.vouched,
                              "needs_go_ahead": C.needs_go_ahead(e), "decision": e.decision, "status": e.status,
                              "status_reason": e.status_reason, "sent_at": e.sent_at, "subject": subject, "body": body})
    return out


@router.get("/campaigns")
def list_campaigns(status: str | None = Query(None), session: Session = Depends(get_session)) -> list[dict]:
    q = select(OutreachCampaign).order_by(OutreachCampaign.id.desc())
    if status:
        q = q.where(OutreachCampaign.status == status)
    return [_campaign_out(session, c) for c in session.scalars(q.limit(100)).all()]


@router.get("/campaigns/{campaign_id}")
def get_campaign(campaign_id: int, session: Session = Depends(get_session)) -> dict:
    c = session.get(OutreachCampaign, campaign_id)
    if c is None:
        raise HTTPException(404, "no such campaign")
    return _campaign_out(session, c)


class DecisionIn(BaseModel):
    action: str             # approve | skip
    allow_low: bool = False


@router.post("/campaigns/{campaign_id}/emails/{email_id}")
def decide_email(campaign_id: int, email_id: int, body: DecisionIn, session: Session = Depends(get_session)) -> dict:
    c = session.get(OutreachCampaign, campaign_id)
    e = next((x for x in C.emails(session, c) if x.id == email_id), None) if c else None
    if e is None:
        raise HTTPException(404, "no such email in this campaign")
    try:
        C.decide(session, c, e, body.action, allow_low=body.allow_low)
    except C.ReviewError as err:
        raise HTTPException(422, str(err)) from err
    return _campaign_out(session, c)


class EditIn(BaseModel):
    subject: str
    body: str


@router.put("/campaigns/{campaign_id}/text")
def edit_campaign(campaign_id: int, body: EditIn, session: Session = Depends(get_session)) -> dict:
    c = session.get(OutreachCampaign, campaign_id)
    if c is None:
        raise HTTPException(404, "no such campaign")
    try:
        C.edit(session, c, body.subject, body.body)
    except C.ReviewError as err:
        raise HTTPException(422, str(err)) from err
    return _campaign_out(session, c)
