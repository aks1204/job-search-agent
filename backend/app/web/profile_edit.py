"""Part 6b-2: editing your profile in the app (owner 2026-11-03: "forms in the app").

The Profile screen's Edit opens your profile as a form, in sections. Save checks it against the same rules as
review/profile.yaml; if something's wrong nothing is saved and the problem shows next to the field, with what
you typed kept. A change the AI must re-read or re-grade jobs for asks first, with the rough cost. A new
résumé reading opens here too, its changes marked, and Save confirms it.
"""

import copy
import re
from datetime import datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.config_loader import get_config
from app.db import get_session
from app.web.safety import check_token

router = APIRouter(include_in_schema=False)

APPETITES = {"low": "Low: never sideways", "medium": "Medium: only at a step-up brand",
             "high": "High: anywhere that isn't a step down"}
TRACKS = {"individual_contributor": "Individual contributor", "people_manager": "People manager"}
_MONEY = re.compile(r"[,\s_]")


# --- profile ⇄ the form's values -------------------------------------------------------------------------

def _lines(items) -> str:
    return "\n".join(items or [])


def _split(text: str, commas: bool = False) -> list[str]:
    parts = re.split(r"[\n,;]" if commas else r"\n", text or "")
    seen, out = set(), []
    for p in (x.strip(" •-\t\r") for x in parts):
        if p and p.lower() not in seen:
            seen.add(p.lower())
            out.append(p)
    return out


def blank_job() -> dict:
    return {"company": "", "title": "", "start": "", "end": "", "remove": False}


def to_values(data: dict) -> dict:
    """Your profile as the form shows it."""
    pay = data.get("current_compensation") or {}
    wa = data.get("work_authorization") or {}
    fam = data.get("role_families") or {}
    skills = data.get("skills") or {}
    return {
        "name": data.get("name") or "", "location": data.get("location") or "", "mobile": data.get("mobile") or "",
        "linkedin_url": data.get("linkedin_url") or "", "career_field": data.get("career_field") or "",
        "pay_amount": f"{pay['annual_ctc']:,}" if pay.get("annual_ctc") is not None else "",
        "pay_currency": (pay.get("currency") or "INR").upper(), "pay_variable": bool(pay.get("includes_variable", True)),
        "pay_floor": f"{data['pay_floor_annual']:,}" if data.get("pay_floor_annual") is not None else "",
        "notice_days": str(data["notice_period_days"]) if data.get("notice_period_days") is not None else "",
        "jobs": [{"company": j.get("company") or "", "title": j.get("title") or "", "start": j.get("start") or "",
                  "end": j.get("end") or "", "remove": False} for j in data.get("employment_history") or []]
                + [blank_job()],
        "level_ladder": _lines(data.get("level_ladder")), "current_level": data.get("current_level") or "",
        "cities": ", ".join(data.get("preferred_locations_india") or []),
        "remote_ok": bool(data.get("remote_ok", True)), "hybrid_ok": bool(data.get("hybrid_ok", True)),
        "open_to_relocation": bool(wa.get("open_to_relocation")), "citizenship": wa.get("citizenship") or "India",
        "visas": ", ".join(wa.get("visas_held") or []),
        "progression": _lines(fam.get("progression")), "lateral": _lines(fam.get("lateral")),
        "excluded": _lines(fam.get("excluded")), "target_roles": _lines(data.get("target_roles")),
        "lateral_appetite": data.get("lateral_appetite") or "medium",
        "track": data.get("track") or "individual_contributor",
        "open_to_management_track": bool(data.get("open_to_management_track")),
        "must_have": _lines(skills.get("must_have")), "nice_to_have": _lines(skills.get("nice_to_have")),
    }


TEXT_FIELDS = ("name", "location", "mobile", "linkedin_url", "career_field", "pay_amount", "pay_currency",
               "pay_floor", "notice_days", "level_ladder", "current_level", "cities", "citizenship", "visas",
               "progression", "lateral", "excluded", "target_roles", "lateral_appetite", "track", "must_have",
               "nice_to_have")
CHECKBOXES = ("pay_variable", "remote_ok", "hybrid_ok", "open_to_relocation", "open_to_management_track")


def from_form(form) -> dict:
    """What you typed, as the form shows it again (nothing lost when something needs fixing)."""
    values = {k: str(form.get(k) or "").strip() for k in TEXT_FIELDS}
    values.update({k: form.get(k) == "on" for k in CHECKBOXES})
    try:
        count = min(int(form.get("job_count") or 0), 60)
    except ValueError:
        count = 0
    values["jobs"] = [{"company": str(form.get(f"job-{i}-company") or "").strip(),
                       "title": str(form.get(f"job-{i}-title") or "").strip(),
                       "start": str(form.get(f"job-{i}-start") or "").strip(),
                       "end": str(form.get(f"job-{i}-end") or "").strip(),
                       "remove": form.get(f"job-{i}-remove") == "on"} for i in range(count)]
    return values


def _month(text: str) -> str | None:
    """'2021-03', '2021-3', '03/2021' or 'Mar 2021' → '2021-03'; None if it isn't a month."""
    t = text.strip()
    for fmt in ("%Y-%m", "%m/%Y", "%b %Y", "%B %Y", "%Y/%m"):
        try:
            d = datetime.strptime(t, fmt)
            return f"{d.year:04d}-{d.month:02d}"
        except ValueError:
            continue
    return None


def _money(text: str, field: str, errors: dict) -> int | None:
    t = _MONEY.sub("", text or "")
    if not t:
        return None
    if not t.isdigit():
        errors[field] = "Write a whole number, e.g. 24,00,000 or 2400000."
        return None
    return int(t)


def to_data(values: dict, base: dict) -> tuple[dict, dict]:
    """(profile, problems by field). Starts from what the agent has, so anything the form doesn't show stays."""
    from app.config_schemas import OwnerStub
    from app.modules.m5_profile.store import experience_years

    errors: dict = {}
    data = copy.deepcopy(base or {})
    for key in ("name", "location", "career_field"):
        data[key] = values[key]
    if not values["name"]:
        errors["name"] = "Your name is needed (it signs your emails)."
    if not values["career_field"]:
        errors["career_field"] = "Say your field in a few words, e.g. business analytics."
    data["mobile"] = values["mobile"] or None
    data["linkedin_url"] = values["linkedin_url"] or None
    if values["linkedin_url"] and not values["linkedin_url"].startswith(("https://", "http://")):
        errors["linkedin_url"] = "Paste the whole link, starting with https://"

    amount = _money(values["pay_amount"], "pay_amount", errors)
    if amount is None:
        data["current_compensation"] = None
    else:
        old = (base or {}).get("current_compensation") or {}
        data["current_compensation"] = {**old, "annual_ctc": amount, "currency": (values["pay_currency"] or "INR").upper(),
                                        "includes_variable": values["pay_variable"]}
    data["pay_floor_annual"] = _money(values["pay_floor"], "pay_floor", errors)
    if values["notice_days"]:
        if values["notice_days"].isdigit() and int(values["notice_days"]) <= 365:
            data["notice_period_days"] = int(values["notice_days"])
        else:
            errors["notice_days"] = "Days, as a number, e.g. 60."
    else:
        data["notice_period_days"] = None

    history, current = [], []
    for i, j in enumerate(values["jobs"]):
        if j["remove"] or not any((j["company"], j["title"], j["start"], j["end"])):
            continue
        start, end = _month(j["start"]), _month(j["end"]) if j["end"] else None
        if not (j["company"] and j["title"]):
            errors[f"job-{i}"] = "Company and title, please."
        elif not start:
            errors[f"job-{i}"] = "Start month as 2021-03."
        elif j["end"] and not end:
            errors[f"job-{i}"] = "End month as 2023-08, or leave it empty for your current job."
        elif end and end < start:
            errors[f"job-{i}"] = "The end date is before the start date."
        else:
            history.append({"company": j["company"], "title": j["title"], "start": start, "end": end})
            if end is None:
                current.append(i)
    if not history and not any(k.startswith("job-") for k in errors):
        errors["jobs"] = "Add at least your current job."
    elif history and not current and not any(k.startswith("job-") for k in errors):
        errors["jobs"] = "Leave the end date empty on your current job."
    elif len(current) > 1:
        errors[f"job-{current[1]}"] = "Only your current job can have no end date."
    data["employment_history"] = history
    now = next((h for h in history if h["end"] is None), None)
    if now:
        data["current_role"] = {"title": now["title"], "company": now["company"], "started": now["start"]}
    data["total_experience_years"] = experience_years(history) if history else data.get("total_experience_years")

    ladder = _split(values["level_ladder"])
    data["level_ladder"] = ladder
    data["current_level"] = values["current_level"] or None
    if ladder and data["current_level"] and data["current_level"] not in ladder:
        errors["current_level"] = "Pick your level from the ladder above."

    data["preferred_locations_india"] = _split(values["cities"], commas=True)
    data["remote_ok"], data["hybrid_ok"] = values["remote_ok"], values["hybrid_ok"]
    data["work_authorization"] = {**((base or {}).get("work_authorization") or {}),
                                  "citizenship": values["citizenship"] or "India",
                                  "visas_held": _split(values["visas"], commas=True),
                                  "open_to_relocation": values["open_to_relocation"]}
    data["role_families"] = {k: _split(values[k]) for k in ("progression", "lateral", "excluded")}
    both = {x.lower() for x in data["role_families"]["progression"]} & {x.lower() for x in data["role_families"]["excluded"]}
    if both:
        errors["excluded"] = f"Can't be both a step up and excluded: {', '.join(sorted(both))}."
    data["target_roles"] = _split(values["target_roles"])
    data["lateral_appetite"] = values["lateral_appetite"] if values["lateral_appetite"] in APPETITES else "medium"
    data["track"] = values["track"] if values["track"] in TRACKS else "individual_contributor"
    data["open_to_management_track"] = values["open_to_management_track"]
    data["skills"] = {"must_have": _split(values["must_have"]), "nice_to_have": _split(values["nice_to_have"])}

    if not errors:
        try:
            data = OwnerStub.model_validate(data).model_dump(mode="json")
        except ValidationError as e:   # the file's own rules; anything the form's checks missed
            errors["form"] = "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors()[:5])
    return data, errors


# --- what a change costs ---------------------------------------------------------------------------------

def cost_of(session: Session, old: dict, new: dict) -> dict | None:
    """A change to what the AI is told about you: how many open roles get read / graded again and outreach
    drafts written again, roughly what it costs, and which of your changes did it (None: free to save)."""
    from app.models import JobAssessment, JobPosting, JobScore, OutreachDraft
    from app.modules.m5_profile.profile import LABELS, ai_changes, changed_fields

    read, grade, redraft = ai_changes(old, new)
    if not (grade or redraft):
        return None
    free = {"location", "current_compensation", "pay_floor_annual", "notice_period_days",
            "preferred_locations_india", "remote_ok", "hybrid_ok", "lateral_appetite"}
    fields = [LABELS[k] for k in changed_fields(old, new) if k not in free]
    open_ = (select(func.count()).select_from(JobAssessment)
             .join(JobPosting, JobPosting.id == JobAssessment.job_id).where(JobPosting.status == "open"))
    reads = (session.scalar(open_) or 0) if read else 0
    grades = (session.scalar(select(func.count()).select_from(JobScore).where(
        JobScore.graded.is_(True), JobScore.band != "archive")) or 0) if grade else 0
    drafts = (session.scalar(select(func.count()).select_from(OutreachDraft)) or 0) if redraft else 0
    per_read = float(session.scalar(select(func.avg(JobAssessment.cost_usd)).where(JobAssessment.cost_usd > 0)) or 0.002)
    per_grade = float(session.scalar(select(func.avg(JobScore.cost_usd)).where(JobScore.cost_usd > 0)) or 0.01)
    per_draft = float(session.scalar(select(func.avg(OutreachDraft.cost_usd)).where(OutreachDraft.cost_usd > 0)) or 0.02)
    return {"fields": fields or ["work history"], "reads": reads, "grades": grades, "drafts": drafts,
            "usd": round(reads * per_read + grades * per_grade + drafts * per_draft, 2)}


# --- the screen ------------------------------------------------------------------------------------------

def _start(session: Session) -> tuple[dict, dict, set]:
    """(what the form starts from, what the agent has now, fields your new résumé changed)."""
    from app.modules.m5_profile import store

    from app.modules.m5_profile.profile import resume_draft

    current = store.load_confirmed(session)
    draft = resume_draft(session)
    now = dict(current.data) if current else (get_config().owner.model_dump(mode="json") if get_config().owner else {})
    if draft is not None and draft.data:
        notes = draft.notes or {}
        marked = {c["field"] for c in notes.get("changed") or []} | (set(notes.get("from_resume") or []) - set(now))
        return dict(draft.data), now, marked
    return now, now, set()


def _render(request: Request, session: Session, values: dict, *, errors: dict | None = None,
            cost: dict | None = None, marked: set | None = None, draft: bool = False) -> HTMLResponse:
    from app.web.pages import _page

    return _page(request, session, "profile_edit.html", "profile", v=values, errors=errors or {}, cost=cost,
                 marked=marked or set(), draft=draft, appetites=APPETITES, tracks=TRACKS)


@router.get("/profile/edit", response_class=HTMLResponse)
def edit(request: Request, session: Session = Depends(get_session)):
    from app.modules.m5_profile import profile as P

    start, _now, marked = _start(session)
    return _render(request, session, to_values(start), marked=marked, draft=P.resume_draft(session) is not None)


@router.post("/profile/edit", response_class=HTMLResponse)
async def save(request: Request, session: Session = Depends(get_session)):
    form = await request.form()
    return await run_in_threadpool(_save, request, session, form)


def _save(request: Request, session: Session, form):
    from app.modules.m5_profile import profile as P
    from app.modules.m5_profile import store
    from app.web.actions import FLASH_COOKIE, _flash_value

    start, now, marked = _start(session)
    draft = P.resume_draft(session) is not None
    values = from_form(form)
    try:
        check_token(request, str(form.get("token") or ""))
    except Exception:
        return _render(request, session, values, marked=marked, draft=draft,
                       errors={"form": "This page was old. Nothing was saved — press Save again."})
    if form.get("add_row"):
        values["jobs"].append(blank_job())
        return _render(request, session, values, marked=marked, draft=draft)
    data, errors = to_data(values, start)
    if errors:
        return _render(request, session, values, errors=errors, marked=marked, draft=draft)
    cost = cost_of(session, now, data)
    if cost and form.get("confirmed") != "1":
        return _render(request, session, values, cost=cost, marked=marked, draft=draft)
    step = session.begin_nested()
    try:
        _row, changed = P.save_edit(session, data)
        step.commit()
    except (ValueError, ValidationError) as e:
        step.rollback()
        return _render(request, session, values, marked=marked, draft=draft, errors={"form": f"Not saved: {e}"})
    session.commit()
    store.invalidate()
    what = ", ".join(P.LABELS[k] for k in changed) or "nothing changed; pay and notice period re-confirmed"
    r = RedirectResponse("/profile", status_code=303)
    r.set_cookie(FLASH_COOKIE, _flash_value(f"Saved: {what}. Every part of the agent uses it now."),
                 httponly=True, samesite="lax", max_age=120, path="/")
    return r
