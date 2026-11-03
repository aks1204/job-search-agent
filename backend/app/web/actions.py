"""Part 6b: every button on the screens. Each is a small form (works without JavaScript): it checks the page's
secret code, does one thing through the same code the terminal uses, then goes back to the page with a short
line saying what happened — and an Undo where a change can be taken back.

Big changes (Sending on, a new website or name, a permanent reject, stopping outreach, forgetting a memory)
first show what will happen and wait for Confirm. Slow jobs run in the background (see runs.py).
"""

import base64
import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from starlette.concurrency import run_in_threadpool
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_session
from app.models import Company, JobScore, Notification
from app.web.safety import check_token, safe_next

router = APIRouter(prefix="/do", include_in_schema=False)
FLASH_COOKIE = "ja_flash"
OUTREACH_ACTIONS = ("hold", "release", "stop")
MEMORY_ACTIONS = ("pin", "unpin", "forget", "restore", "keep")
CONTACT_KINDS = ("recruiting", "hiring_manager", "founder")


class Confirm(Exception):
    """Raised by an action that needs your Confirm first: what will happen, line by line."""

    def __init__(self, title: str, lines: list[str], button: str = "Confirm"):
        self.title, self.lines, self.button = title, lines, button


class NotFound(ValueError):
    """The thing a button is about is gone (e.g. a company that was merged meanwhile)."""


class Refused(ValueError):
    """Not done — and `after` records something that must stay even so (e.g. Gmail asking you to sign in again)."""

    def __init__(self, message: str, after=None):
        super().__init__(message)
        self.after = after


def _flash_value(text: str, ok: bool = True, undo: str | None = None, card: int | None = None) -> str:
    raw = json.dumps({"text": text[:400], "ok": ok, "undo": undo, "card": card}).encode()
    return base64.urlsafe_b64encode(raw).decode()


def read_flash(request: Request) -> dict | None:
    value = request.cookies.get(FLASH_COOKIE)
    if not value:
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(value.encode()))
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    undo = data.get("undo")
    card = data.get("card")
    return {"text": str(data.get("text") or ""), "ok": bool(data.get("ok", True)),
            "undo": undo if isinstance(undo, str) and undo.startswith("/do/") else None,
            "card": card if isinstance(card, int) and not isinstance(card, bool) else None}


def _back(form, text: str, *, ok: bool = True, undo: str | None = None, card: int | None = None,
          fallback: str = "/jobs") -> RedirectResponse:
    """Back to the page. With `card`, to that role's card, which stays shown (marked) for this one view."""
    url = safe_next(str(form.get("next") or ""), fallback).split("#")[0]
    r = RedirectResponse(f"{url}#c{card}" if card else url, status_code=303)
    r.set_cookie(FLASH_COOKIE, _flash_value(text, ok, undo, card), httponly=True, samesite="lax", max_age=120,
                 path="/")
    return r


async def _run(request: Request, session: Session, do, *, fallback: str = "/jobs"):
    """One button press: the code check, then the rest off the web server's main loop (database, Redis and
    Gmail calls block), so one slow button never stalls the other screens."""
    form = await request.form()
    try:
        check_token(request, str(form.get("token") or ""))
    except HTTPException:
        return _back(form, "Not done: this page was old. It's fresh now — press the button again.", ok=False,
                     fallback=fallback)
    return await run_in_threadpool(_press, request, session, do, form, fallback)


def _press(request: Request, session: Session, do, form, fallback: str):
    """The action, its confirm step, the save, the line saying what happened. A refused or confirm-first press
    leaves no trace (a savepoint). An action may return (text, undo URL, what to do once it's saved)."""
    from app.web.pages import _page

    step = session.begin_nested()
    try:
        out = do(form)
        step.commit()
    except Confirm as c:
        step.rollback()
        if form.get("confirmed") == "1":            # (an action asks only once; a second ask is a bug)
            raise HTTPException(500, "confirm loop") from None
        fields = [(k, str(v)) for k, v in form.multi_items() if k not in ("token", "confirmed")]
        return _page(request, session, "confirm.html", "", c=c, action=request.url.path, fields=fields,
                     back=safe_next(str(form.get("next") or ""), fallback))
    except ValueError as e:                         # your input or the state of things: told, nothing changed
        step.rollback()
        if getattr(e, "after", None):
            e.after()
            session.commit()
        return _back(form, f"Not done: {e}", ok=False, fallback=fallback)
    out = out if isinstance(out, tuple) else (out,)
    text, undo, then, card = (*out, None, None, None)[:4]
    session.commit()
    if then is not None:                            # outside the database (Redis): only once the save stuck
        then()
    return _back(form, text, undo=undo, card=card, fallback=fallback)


def _confirmed(form) -> bool:
    return form.get("confirmed") == "1"


def _text(form, key: str, default: str = "") -> str:
    return str(form.get(key) or default).strip()


def _company(session: Session, company_id: int) -> Company:
    c = session.get(Company, company_id)
    if c is None:
        raise NotFound("no such company")
    return c


# --- Jobs: your reaction to a role -----------------------------------------------------------------

@router.post("/feedback/{cluster_id}")
async def feedback(cluster_id: int, request: Request, session: Session = Depends(get_session)):
    """Pursue or Skip (the Jobs screen's two buttons; the API still takes 👍 / 👎 / rescue). The card stays on
    the page, marked, with Undo — until you reload."""
    from app.modules.m2_jobs.scoring import FEEDBACK_ACTIONS, record_feedback
    from app.modules.m4_apply.tracker import pursue_now
    from app.modules.m5_profile.reasons import CHIPS, explain

    def do(form):
        action = _text(form, "action")
        if action not in FEEDBACK_ACTIONS:
            raise ValueError("unknown button")
        row = session.scalar(select(JobScore).where(JobScore.cluster_id == cluster_id))
        if row is None:
            raise NotFound("this role isn't scored")
        chips = [c for c in form.getlist("chips") if c in CHIPS]
        fb = record_feedback(session, row, action, _text(form, "reason") or None, chips=chips)
        session.flush()
        if action == "act":
            pursue_now(session, cluster_id)
            text = "Pursuing: it's on your Pursuing tab with its apply link; contacts and a draft follow on Review."
        elif action in ("skip", "thumbs_down"):
            text = f"Skipped · reason: {explain(fb.chips, fb.inferred)}"
        else:
            text = {"thumbs_up": "Liked", "maybe_rescue": "Moved to the Inbox"}[action]
        return text, f"/do/feedback-undo/{fb.id}", None, cluster_id

    return await _run(request, session, do)


@router.post("/feedback-undo/{feedback_id}")
async def feedback_undo(feedback_id: int, request: Request, session: Session = Depends(get_session)):
    from app.models import JobFeedback
    from app.modules.m2_jobs.scoring import undo_feedback
    from app.modules.m4_apply.tracker import after_undo

    def do(form):
        fb = session.get(JobFeedback, feedback_id)
        if fb is None:
            raise NotFound("nothing to undo (it was already undone)")
        action, cluster_id = fb.action, fb.cluster_id
        row = undo_feedback(session, feedback_id)
        after_undo(session, cluster_id, action)
        return f"Undone: back in {row.band.replace('_', ' ')}.", None, None, cluster_id

    return await _run(request, session, do)


# --- Role page --------------------------------------------------------------------------------------

@router.post("/applied/{cluster_id}")
async def applied(cluster_id: int, request: Request, session: Session = Depends(get_session)):
    from app.modules.m4_apply.tracker import mark_applied

    def do(form):
        mark_applied(session, cluster_id, _text(form, "note") or None)
        return "Marked applied. Emails from the hiring system about it will be tracked from here."

    return await _run(request, session, do)


@router.post("/apply-skip/{cluster_id}")
async def apply_skip(cluster_id: int, request: Request, session: Session = Depends(get_session)):
    from app.modules.m4_apply.tracker import skip

    def do(form):
        skip(session, cluster_id, _text(form, "reason") or "no reason given")
        return "Taken off your apply list."

    return await _run(request, session, do)


@router.post("/designate/{cluster_id}")
async def designate(cluster_id: int, request: Request, session: Session = Depends(get_session)):
    from app.modules.m3_outreach.pipeline import designate as set_channel

    def do(form):
        email = _text(form, "email") or None
        ch = set_channel(session, cluster_id, email, _text(form, "reason") or "your choice")
        return (f"Only {ch.designated_email} will be used for this role." if ch.manual
                else "Back to automatic: the agent picks the people on the next round.")

    return await _run(request, session, do)


@router.post("/add-contact/{company_id}")
async def add_contact(company_id: int, request: Request, session: Session = Depends(get_session)):
    from app.modules.m3_outreach.pipeline import add_contact as add

    def do(form):
        kind = _text(form, "kind", "recruiting")
        if kind not in CONTACT_KINDS:
            raise ValueError("unknown kind of contact")
        c = add(session, _company(session, company_id), _text(form, "name"), _text(form, "email") or None,
                _text(form, "title") or None, kind)
        return f"Added {c.full_name}; the next round of contact finding can use them."

    return await _run(request, session, do)


# --- Companies --------------------------------------------------------------------------------------

@router.post("/verdict/{company_id}")
async def verdict(company_id: int, request: Request, session: Session = Depends(get_session)):
    from app.modules.m1_companies.override import override

    def do(form):
        c = _company(session, company_id)
        decision = _text(form, "decision")
        if decision not in ("keep", "reject"):
            raise ValueError("keep or reject")
        permanent = form.get("permanent") == "1" and decision == "reject"
        reason = _text(form, "reason")
        if permanent and not _confirmed(form):
            raise Confirm(f"Reject {c.name} for good?",
                          [f"{c.name} ({c.domain or 'no website'}) is taken off your watchlist and never judged again.",
                           f"Your reason: {reason or 'none given'} — kept as an example for future judgments.",
                           "You can still keep it later from its page."], "Reject for good")
        v = override(session, c, "pass" if decision == "keep" else "fail", reason, permanent=permanent)
        return f"{c.name}: {v.summary}"

    return await _run(request, session, do, fallback="/companies")


@router.post("/brand/{company_id}")
async def brand(company_id: int, request: Request, session: Session = Depends(get_session)):
    from app.modules.m2_jobs.brand import set_brand

    def do(form):
        c = _company(session, company_id)
        level = _text(form, "level")
        if level not in ("up", "lateral", "down"):
            raise ValueError("up, lateral or down")
        n = set_brand(session, c, level, _text(form, "reason"))
        label = {"up": "a step up", "lateral": "sideways", "down": "a step down"}[level]
        return f"{c.name}: recorded as {label} from your current employer. {n} role(s) re-checked on the next round."

    return await _run(request, session, do, fallback="/companies")


def _correction(session: Session, company_id: int, form, *, new_domain=None, new_name=None):
    from app.modules.m1_companies import fix_website as F

    c = _company(session, company_id)
    p = F.plan(session, c, new_domain=new_domain, new_name=new_name)
    if not _confirmed(form):
        raise Confirm(f"Correct {c.name}?", p.describe(), "Yes, start it over")
    c = F.apply(session, p)
    return (f"Done: {c.name} ({c.domain or 'no website'}) starts over — it's judged again on the next round, "
            "then its roles, contacts and drafts follow.")


@router.post("/fix-website/{company_id}")
async def fix_website(company_id: int, request: Request, session: Session = Depends(get_session)):
    return await _run(request, session, lambda form: _correction(session, company_id, form,
                                                                 new_domain=_text(form, "domain")),
                      fallback="/companies")


@router.post("/rename/{company_id}")
async def rename(company_id: int, request: Request, session: Session = Depends(get_session)):
    return await _run(request, session, lambda form: _correction(session, company_id, form,
                                                                 new_name=_text(form, "name")),
                      fallback="/companies")


@router.post("/name-ok/{company_id}")
async def name_ok(company_id: int, request: Request, session: Session = Depends(get_session)):
    from app.modules.m1_companies.fix_website import confirm_name

    def do(form):
        c = _company(session, company_id)
        confirm_name(session, c)
        return f"{c.name} ↔ {c.domain}: marked correct — never flagged again (unless its website changes)."

    return await _run(request, session, do, fallback="/companies")


@router.post("/outreach/{company_id}")
async def outreach(company_id: int, request: Request, session: Session = Depends(get_session)):
    from app.modules.m3_outreach import campaigns as C

    def do(form):
        c = _company(session, company_id)
        action = _text(form, "action")
        if action not in OUTREACH_ACTIONS:
            raise ValueError("hold, release or stop")
        if action == "hold":
            return f"{c.name}: {C.stop_company(session, c.id, 'held by you', pause=True)} unsent email(s) held."
        if action == "release":
            return f"{c.name}: {C.resume_company(session, c.id)} campaign(s) released."
        if not _confirmed(form):
            raise Confirm(f"Stop all outreach to {c.name}?",
                          ["Every unsent email to this company is cancelled — approved ones too.",
                           "Already-sent emails stay sent. To pause instead (and resume later), use Hold."],
                          "Stop outreach")
        return f"{c.name}: {C.stop_company(session, c.id, 'stopped by you', outcome='stopped')} unsent email(s) cancelled."

    return await _run(request, session, do, fallback="/companies")


@router.post("/employer")
async def employer(request: Request, session: Session = Depends(get_session)):
    from app.modules.m1_companies import employers as E

    def do(form):
        name, domain = _text(form, "name"), _text(form, "domain").lower()
        if not domain:
            raise ValueError("give the website, or none to match by name only")
        e, released = E.fix(session, name, None if domain == "none" else domain)
        if e is None:
            raise NotFound(f"“{name}” isn't an employer in your profile")
        blocked = E.apply(session)
        extra = f" {blocked} compan{'y' if blocked == 1 else 'ies'} newly taken off your watchlist." if blocked else ""
        extra += f" {released} wrongly matched put back for a fresh look." if released else ""
        return f"{e.name}: website set to {e.domain or 'none (matched by exact name only)'}.{extra}"

    return await _run(request, session, do, fallback="/companies")


# --- Background runs ("Run now"; Judge again / Set up feeds / Write the draft now / Read my résumé) ------

@router.post("/run/{job}")
async def run(job: str, request: Request, session: Session = Depends(get_session)):
    from app.web import runs

    def do(form):
        arg = _text(form, "arg") or None
        if arg is not None and not arg.isdigit():
            raise ValueError("bad target")
        try:
            return runs.start(job, arg)
        except ValueError:
            raise
        except Exception as e:                       # Redis / the worker queue not reachable
            raise ValueError(f"couldn't start it — is the worker running? ({type(e).__name__})") from e

    return await _run(request, session, do, fallback="/maintenance")


# --- Profile ----------------------------------------------------------------------------------------

@router.post("/profile-confirm")
async def profile_confirm(request: Request, session: Session = Depends(get_session)):
    from pydantic import ValidationError

    from app.modules.m5_profile import profile as P
    from app.modules.m5_profile import store

    def do(form):
        try:
            row = P.confirm_review_file(session)
        except ValidationError as e:
            raise ValueError("; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors()[:5])) from e
        store.invalidate()
        return f"Confirmed. Every part of the agent now uses this profile (version {row.id})."

    return await _run(request, session, do, fallback="/profile")


@router.post("/direction/{suggestion_id}")
async def direction(suggestion_id: int, request: Request, session: Session = Depends(get_session)):
    from app.modules.m5_profile import direction as D
    from app.modules.m5_profile import store

    def do(form):
        answer = _text(form, "answer")
        if answer not in ("yes", "no"):
            raise ValueError("yes or no")
        s = D.answer(session, suggestion_id, answer == "yes")
        store.invalidate()
        return ("Done: " if answer == "yes" else "Noted, nothing changed: ") + D.describe(s, private=True)

    return await _run(request, session, do, fallback="/profile")


# --- Memory -----------------------------------------------------------------------------------------

@router.post("/memory/{entry_id}")
async def memory(entry_id: int, request: Request, session: Session = Depends(get_session)):
    from app.modules.m5_profile import memory as M

    def do(form):
        action = _text(form, "action") or request.query_params.get("undo", "")
        if action not in MEMORY_ACTIONS:
            raise ValueError("unknown button")
        e = session.get(M.MemoryEntry, entry_id)
        if e is None:
            raise NotFound("no such memory")
        if action == "forget" and not _confirmed(form):
            raise Confirm("Forget this memory?", [M.shown_meaning(e),
                                                  "The agent stops using it. You can restore it from Memory."],
                          "Forget")
        try:
            M.act(session, entry_id, action)
        except M.MemoryError as err:
            raise ValueError(str(err)) from err
        done = {"pin": "Pinned", "unpin": "Unpinned", "forget": "Forgotten", "restore": "Restored",
                "keep": "Kept"}[action]
        undo = {"pin": "unpin", "unpin": "pin", "forget": "restore"}.get(action)
        return f"{done}: {M.shown_meaning(e)[:120]}", (f"/do/memory/{entry_id}?undo={undo}" if undo else None)

    return await _run(request, session, do, fallback="/memory")


@router.post("/memory-undo/{batch}")
async def memory_undo(batch: str, request: Request, session: Session = Depends(get_session)):
    from app.modules.m5_profile.memory import undo_set_aside

    def do(form):
        n = undo_set_aside(session, batch)
        for p in session.scalars(select(Notification).where(Notification.subject_type == "memory",
                                                            Notification.subject_id == f"undo:{batch}")).all():
            p.read_at = p.read_at or _now()
        return f"Undone: {n} earlier skip(s) are used as examples again."

    return await _run(request, session, do, fallback="/pings")


# --- Settings ---------------------------------------------------------------------------------------

@router.post("/sending")
async def sending(request: Request, session: Session = Depends(get_session)):
    from app.core import audit
    from app.modules.m3_outreach import sender as S

    def do(form):
        on = _text(form, "on") == "1"
        if on and not _confirmed(form):
            due = len(S.next_candidates(session, _now()))
            raise Confirm("Turn sending on?", [
                f"Emails you approved start going out: {due} are due now.",
                "Daily limits, spacing and quiet hours still apply; nothing you haven't approved is ever sent.",
                "You can turn it off again here at any time."], "Turn sending on")
        audit.record(session, "m3", "sending_switch", f"You turned sending {'on' if on else 'off'}")
        text = "Sending is on." if on else "Sending is off: nothing goes out until you turn it on."
        return text, None, lambda: S.set_paused(not on)      # the switch flips once the record is saved

    return await _run(request, session, do, fallback="/settings")


@router.post("/breaker-reset")
async def breaker_reset(request: Request, session: Session = Depends(get_session)):
    from app.core import audit
    from app.modules.m3_outreach import sender as S

    def do(form):
        b = S.breaker(session)
        b.state, b.reason, b.reset_at = "ok", None, _now()
        audit.record(session, "m3", "breaker_reset", "You reset the bounce breaker")
        return "Bounce breaker reset."

    return await _run(request, session, do, fallback="/settings")


@router.post("/gmail-test")
async def gmail_test(request: Request, session: Session = Depends(get_session)):
    from app.modules.m3_outreach import gmail

    def do(form):
        acct = gmail.account(session)
        if acct is None or acct.status != "ok":
            raise ValueError("Gmail isn't connected")
        msg = gmail.build_message(sender=acct.email, sender_name=None, to=acct.email, subject="Job Agent test",
                                  body="Sending works. This went from you to you.", attachment=None)
        try:
            gmail.send(session, msg)
        except Exception as e:                       # Google said no, or no internet: told, not a crash
            status, account_id = acct.status, acct.id   # e.g. "reconnect": your sign-in was revoked — that stays

            def keep_status():
                session.get(type(acct), account_id).status = status
            raise Refused(f"Gmail didn't take it ({type(e).__name__}: {str(e)[:120]})", keep_status) from e
        return f"Test email sent to yourself ({acct.email}); check your inbox."

    return await _run(request, session, do, fallback="/settings")


# --- Pings ------------------------------------------------------------------------------------------

def _now():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)


@router.post("/ping-open/{notification_id}")
async def ping_open(notification_id: int, request: Request, session: Session = Depends(get_session)):
    """Open: you go where you act on it (worked out here, never from the form). News counts as read; a ping that
    needs you stays until it's answered (or you press Mark read)."""
    from app.web.pages import ping_target

    form = await request.form()
    try:
        check_token(request, str(form.get("token") or ""))
    except HTTPException:
        return RedirectResponse("/pings", status_code=303)

    def go():
        n = session.get(Notification, notification_id)
        if n is None:
            return "/pings"
        if n.kind != "needs_you":
            n.read_at = n.read_at or _now()
        target = ping_target(session, n) or "/pings"
        session.commit()
        return target

    return RedirectResponse(await run_in_threadpool(go), status_code=303)


@router.post("/ping-read/{notification_id}")
async def ping_read(notification_id: int, request: Request, session: Session = Depends(get_session)):
    def do(form):
        n = session.get(Notification, notification_id)
        if n is None:
            raise NotFound("no such ping")
        n.read_at = n.read_at or _now()
        return "Marked read."

    return await _run(request, session, do, fallback="/pings")


@router.post("/ping-unread/{notification_id}")
async def ping_unread(notification_id: int, request: Request, session: Session = Depends(get_session)):
    def do(form):
        n = session.get(Notification, notification_id)
        if n is None:
            raise NotFound("no such ping")
        n.read_at = None
        return "Marked unread."

    return await _run(request, session, do, fallback="/pings")


@router.post("/pings-read")
async def pings_read(request: Request, session: Session = Depends(get_session)):
    def do(form):
        rows = session.scalars(select(Notification).where(Notification.read_at.is_(None),
                                                          Notification.kind != "needs_you")).all()
        for n in rows:
            n.read_at = _now()
        return f"{len(rows)} marked read (the ones that need you stay until answered)."

    return await _run(request, session, do, fallback="/pings")


@router.post("/assign/{no}")
async def assign(no: int, request: Request, session: Session = Depends(get_session)):
    from app.modules.m4_apply.inbox import AssignError
    from app.modules.m4_apply.inbox import assign as answer

    def do(form):
        choice = _text(form, "choice") or ("other" if _text(form, "other") else "")   # typed a title: that's the choice
        try:
            if choice == "ignore":
                answer(session, no, ignore=True)
            elif choice == "other":
                answer(session, no, other_title=_text(form, "other") or None)
            elif choice.isdigit():
                answer(session, no, cluster_id=int(choice))
            else:
                raise ValueError("pick a role, another job, or not mine")
        except AssignError as e:
            raise ValueError(str(e)) from e
        for n in session.scalars(select(Notification).where(Notification.subject_type == "inbox",
                                                            Notification.subject_id == str(no))).all():
            n.read_at = n.read_at or _now()
        return "Ignored." if choice == "ignore" else "Done: the email's news is recorded on that role."

    return await _run(request, session, do, fallback="/pings")
