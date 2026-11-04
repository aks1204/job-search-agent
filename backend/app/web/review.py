"""Part 6c: the Review screen — every outreach email before it goes, and its buttons.

One card per role. The email is shown once, as it will arrive (only the greeting's first name changes per
person), with who it goes to underneath, day by day. You Approve or Skip each person (Undo until outreach
begins), edit the text or ask for a fresh draft, and then press Begin outreach: only that starts the waves
(owner 2026-11-04). Sending itself still waits for the Sending switch in Settings.
"""

from datetime import timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from markupsafe import Markup, escape
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.db import get_session
from app.models import Company, JobCluster, OutreachCampaign, OutreachDraft
from app.web.safety import check_token

router = APIRouter(include_in_schema=False)

# One-tap reasons for skipping a person (kept with the email; nothing else reads them)
SKIP_REASONS = {"wrong_person": "Not the right person", "too_senior": "Too senior to email cold",
                "in_touch": "Already in touch with them", "address": "The address looks wrong"}
CONFIDENCE = {"proven": ("ch-green", "address proven"), "verified": ("ch-green", "address verified"),
              "low": ("ch-amber", "address not proven"), "unchecked": ("ch-amber", "address not checked"),
              "invalid": ("ch-red", "address doesn't exist")}
DAYS = {0: "Day 1", 1: "Day 2", 2: "Day 3"}
REPLIES = {"interested": "replied: interested", "question": "replied with a question", "rejection": "replied: no",
           "ooo": "out of office", "redirect": "said: not the right person", "auto_other": "automatic reply",
           "unsure": "replied (see Gmail)", "forwarded": "forwarded it"}
STATUS = {"pending_review": ("ch-red", "to review"), "scheduled": ("ch-green", "outreach on"),   # (web/labels.py)
          "running": ("ch-green", "outreach on"), "paused": ("ch-amber", "outreach on hold")}


def preview(body: str, greeting_hint: str = "Hi first name,") -> Markup:
    """The shared text as it will read, escaped, with the per-person greeting marked."""
    marked = f'<span class="greet" title="Each person gets their own first name here">{escape(greeting_hint)}</span>'
    return Markup(str(escape(body)).replace("{greeting}", marked))


def draft_cost(session: Session) -> float:
    return float(session.scalar(select(func.avg(OutreachDraft.cost_usd)).where(OutreachDraft.cost_usd > 0)) or 0.02)


def _person(e) -> dict:
    conf = CONFIDENCE.get(e.confidence, ("ch-gray", e.confidence))
    if e.vouched and e.confidence not in ("proven", "verified", "invalid"):
        conf = ("ch-green", "a lookup service vouches for it")
    state = None
    if e.status == "sent":
        state = "sent " + e.sent_at.strftime("%d %b") if e.sent_at else "sent"
    elif e.status == "bounced" or e.bounced_at:
        state = "bounced"
    elif e.status == "cancelled" and e.decision != "skipped":
        state = e.status_reason or "cancelled"
    elif e.status == "failed" and (e.status_reason or "").startswith("unsure"):
        state = e.status_reason                # stopped mid-send: maybe sent, never re-sent
    elif e.status == "failed":
        state = "couldn't send" + (f": {e.status_reason}" if e.status_reason else "")
    return {"id": e.id, "name": e.to_name, "email": e.to_email, "wave": e.wave, "decision": e.decision,
            "status": e.status, "conf_cls": conf[0], "conf": conf[1], "go_ahead": _needs(e),
            "invalid": e.confidence == "invalid", "allow_low": e.allow_low,
            "skip_reason": (e.status_reason or "").removeprefix("skipped by you").lstrip(": ") if e.decision == "skipped" else "",
            "before": e.previously_emailed_at, "state": state,
            "reply": REPLIES.get(e.reply_category or "", e.reply_category) if (e.replied_at or e.reply_category) else None}


def _needs(e) -> bool:
    from app.modules.m3_outreach.campaigns import needs_go_ahead

    return needs_go_ahead(e)


def card(session: Session, c: OutreachCampaign) -> dict:
    from app.modules.m3_outreach import campaigns as C

    es = C.emails(session, c)
    cluster = session.get(JobCluster, c.cluster_id)
    company = session.get(Company, c.company_id)
    draft = session.get(OutreachDraft, c.draft_id)
    people = [_person(e) for e in es]
    days: dict[int, list] = {}
    for p in people:
        days.setdefault(p["wave"], []).append(p)
    truth = (draft.truth or {}) if draft else {}
    return {"id": c.id, "cluster_id": c.cluster_id, "title": cluster.title if cluster else f"Role {c.cluster_id}",
            "company": company.name if company else "?", "company_id": c.company_id, "status": c.status,
            "status_chip": STATUS.get(c.status, ("ch-gray", c.status.replace("_", " "))),
            "status_reason": c.status_reason, "edited": c.edited, "subject": c.subject, "body": preview(c.body),
            "attach_resume": c.attach_resume, "armed_at": c.armed_at, "people": people,
            "days": sorted(days.items()), "tally": C.tally(es),
            "approvable": [p for p in people if p["decision"] == "pending" and not p["go_ahead"] and not p["invalid"]],
            "before": [p for p in people if p["before"] and p["decision"] == "pending"],
            "flagged": bool(draft and draft.status == "flagged"), "cut": truth.get("cut") or [],
            "scenario": draft.scenario if draft else None,
            "day_dates": {w: c.armed_at + timedelta(days=w) for w in days} if c.armed_at else {},
            "late": [p for p in people if p["decision"] == "pending"] if c.status in ("scheduled", "running") else []}


def page_context(session: Session) -> dict:
    from app.modules.m3_outreach import sender as S
    from app.modules.m3_outreach.campaigns import ACTIVE
    from app.modules.m3_outreach.drafting import attachment

    rows = session.scalars(select(OutreachCampaign).where(OutreachCampaign.status.in_(ACTIVE))
                           .order_by(OutreachCampaign.id)).all()
    cards = [card(session, c) for c in rows]
    try:
        sending_on = not S.paused()
    except Exception:                       # Redis down: say it's off (it can't send without Redis anyway)
        sending_on = False
    att = attachment()
    return {"waiting": [x for x in cards if x["status"] == "pending_review"],
            "under_way": [x for x in cards if x["status"] != "pending_review"],
            "attachment": att[0] if att else None, "sending_on": sending_on, "skip_reasons": SKIP_REASONS,
            "days": DAYS, "draft_cents": max(1, round(draft_cost(session) * 100))}


# --- Edit draft: the email's text as a form ----------------------------------------------------------

def _render_edit(request: Request, session: Session, c: OutreachCampaign, subject: str, body: str,
                 error: str | None = None) -> HTMLResponse:
    from app.web.pages import _page

    return _page(request, session, "review_edit.html", "review", c=card(session, c), subject=subject, body=body,
                 error=error)


def _campaign(session: Session, campaign_id: int) -> OutreachCampaign | None:
    return session.get(OutreachCampaign, campaign_id)


@router.get("/review/{campaign_id}/edit", response_class=HTMLResponse)
def edit(campaign_id: int, request: Request, session: Session = Depends(get_session)):
    c = _campaign(session, campaign_id)
    if c is None or c.status != "pending_review":
        return RedirectResponse("/review", status_code=303)
    return _render_edit(request, session, c, c.subject, c.body)


@router.post("/review/{campaign_id}/edit", response_class=HTMLResponse)
async def save(campaign_id: int, request: Request, session: Session = Depends(get_session)):
    form = await request.form()
    return await run_in_threadpool(_save, campaign_id, request, session, form)


def _save(campaign_id: int, request: Request, session: Session, form):
    from app.modules.m3_outreach import campaigns as C
    from app.web.actions import FLASH_COOKIE, _flash_value

    c = _campaign(session, campaign_id)
    if c is None or c.status != "pending_review":
        return RedirectResponse("/review", status_code=303)
    subject = str(form.get("subject") or "")
    body = str(form.get("body") or "").replace("\r\n", "\n")
    try:
        check_token(request, str(form.get("token") or ""))
    except Exception:
        return _render_edit(request, session, c, subject, body, "This page was old. Nothing was saved — press Save again.")
    before = (c.subject, c.body)
    step = session.begin_nested()
    try:
        C.edit(session, c, subject, body)
        step.commit()
    except C.ReviewError as e:
        step.rollback()
        return _render_edit(request, session, c, subject, body, f"Not saved: {e}.")
    session.commit()
    text = ("Saved your edit. Approvals were reset, so approve each person again for the new text."
            if (c.subject, c.body) != before else "No change: the text is as it was.")
    r = RedirectResponse(f"/review#c{c.id}", status_code=303)
    r.set_cookie(FLASH_COOKIE, _flash_value(text), httponly=True, samesite="lax", max_age=120, path="/")
    return r
