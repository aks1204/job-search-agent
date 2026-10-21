"""Module 5b: career direction (spec §5.2), with the owner's rule: nothing is added to or removed from what
the agent looks for without your yes.

- The agent works out its VIEW from your resume + profile (one AI read, re-done when the profile changes):
  where you are, where you're heading, every family it sees as step-up / sideways (+ when) / excluded, the
  job titles worth searching, your sideways appetite and track — each with a one-line reason.
- Your actual lists (role families, target roles, appetite, track) stay in your confirmed profile.
- Differences between its view and your lists become SUGGESTIONS for one yes/no each (summary mail /
  dashboard; `direction yes|no <id>` until then). Yes changes your lists; no is remembered, and asked again
  only if a later resume makes a strong case. Reasons and summaries refresh automatically.
"""

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config_schemas import OwnerStub
from app.core import audit
from app.core.notify import notify
from app.models.direction import CareerDirection, DirectionSuggestion
from app.models.profile import OwnerProfile
from app.modules.m2_jobs.profile_text import resume_text
from app.modules.m2_jobs.reader import profile_brief
from app.modules.m5_profile import store

log = logging.getLogger(__name__)

REVIEW_FILE = Path("/review/direction.yaml")
LISTS = ("progression", "lateral", "excluded")
EDITABLE = ("role_families", "target_roles", "lateral_appetite", "track", "open_to_management_track")


# --- the agent's view -------------------------------------------------------------------------------

class FamilyView(BaseModel):
    family: str = Field(description="A role family. If it means the same as one in the candidate's current lists, "
                                    "use exactly that name.")
    verdict: Literal["progression", "lateral", "excluded"] = Field(
        description="progression = a step up for them; lateral = sideways; excluded = not a fit.")
    condition: str = Field(description="For lateral: when it's worth it (e.g. 'only at a step-up brand'); else ''.")
    why: str = Field(description="One line, from the resume.")
    strength: Literal["strong", "moderate"] = Field(description="How clearly the resume supports this.")


class TitleView(BaseModel):
    title: str = Field(description="A job title to search job sites for. For a current target role, use its exact text.")
    action: Literal["keep", "add", "drop"] = Field(description="keep a current one / add a new one / drop a current one.")
    why: str
    strength: Literal["strong", "moderate"]


class DirectionView(BaseModel):
    position: str = Field(description="Where they are: level, time in role, recent moves. One or two lines.")
    trajectory: str = Field(description="Where their career is heading. One or two lines.")
    families: list[FamilyView] = Field(description="Every family in their current lists, plus any new one the resume "
                                                   "clearly supports.")
    target_roles: list[TitleView] = Field(description="Every current target role (keep or drop), plus new ones to add.")
    lateral_appetite: Literal["low", "medium", "high"]
    appetite_why: str
    track: Literal["individual_contributor", "people_manager"]
    open_to_management: bool
    track_why: str


SYSTEM = """You read a job seeker's resume and profile and describe their career direction: where they are,
where they're heading, which role families are a step up / sideways / not a fit, which job titles are worth
searching for, how open they should be to sideways moves, and whether they're on an individual-contributor or
people-manager track. Ground every point in the resume. Their current choices are listed: cover each one (say
honestly if one no longer fits) and add new ones only when the resume clearly supports them.
Answer in the requested JSON format."""


def _current(owner: OwnerStub) -> dict:
    fam = owner.role_families
    return {"progression": list(fam.progression), "lateral": list(fam.lateral), "excluded": list(fam.excluded),
            "target_roles": list(owner.target_roles), "lateral_appetite": owner.lateral_appetite,
            "track": owner.track, "open_to_management_track": owner.open_to_management_track}


def derive_view(session: Session, llm, owner: OwnerStub) -> DirectionView:
    cur = _current(owner)
    user = "\n".join([
        "## The candidate", profile_brief(owner),
        "", "## Their current choices",
        f"Step-up families: {', '.join(cur['progression']) or '-'}",
        f"Sideways families: {', '.join(cur['lateral']) or '-'}",
        f"Excluded families: {', '.join(cur['excluded']) or '-'}",
        f"Target roles (job titles searched): {', '.join(cur['target_roles']) or '-'}",
        f"Sideways appetite: {cur['lateral_appetite']}; track: {cur['track']}; open to managing: {cur['open_to_management_track']}",
        "", "--- resume ---", resume_text()[:15000] or "(resume not available)",
    ])
    return llm.structured("judge", system=SYSTEM, user=user, schema=DirectionView, purpose="m5.direction",
                          session=session, subject=("profile", "direction")).parsed


def _norm(s: str) -> str:
    return " ".join((s or "").lower().split())


def _where(cur: dict, family: str) -> str | None:
    n = _norm(family)
    for lst in LISTS:
        if n in {_norm(x) for x in cur[lst]}:
            return lst
    return None


def suggestions_from(view: DirectionView, cur: dict) -> list[dict]:
    """What would change your lists → one suggestion each. Pure: only reads."""
    out = []
    for f in view.families:
        now = _where(cur, f.family)
        if now is None and f.verdict in ("progression", "lateral"):
            out.append({"kind": "add", "target": f.verdict, "item": f.family, "why": f.why, "strength": f.strength})
        elif now in ("progression", "lateral") and f.verdict == "excluded":
            out.append({"kind": "remove", "target": now, "item": f.family, "why": f.why, "strength": f.strength})
        elif now == "excluded" and f.verdict in ("progression", "lateral"):
            out.append({"kind": "include", "target": f.verdict, "item": f.family, "why": f.why, "strength": f.strength})
    titles = {_norm(t) for t in cur["target_roles"]}
    for t in view.target_roles:
        if t.action == "add" and _norm(t.title) not in titles:
            out.append({"kind": "add", "target": "target_roles", "item": t.title, "why": t.why, "strength": t.strength})
        elif t.action == "drop" and _norm(t.title) in titles:
            out.append({"kind": "remove", "target": "target_roles", "item": t.title, "why": t.why, "strength": t.strength})
    if view.lateral_appetite != cur["lateral_appetite"]:
        out.append({"kind": "appetite", "target": "lateral_appetite", "item": view.lateral_appetite,
                    "why": view.appetite_why, "strength": "moderate"})
    if view.track != cur["track"] or view.open_to_management != cur["open_to_management_track"]:
        item = f"{view.track}{' (open to managing)' if view.open_to_management else ''}"
        out.append({"kind": "track", "target": "track", "item": item, "why": view.track_why, "strength": "moderate"})
    return out


def _key(s) -> tuple:
    return (s["kind"] if isinstance(s, dict) else s.kind, s["target"] if isinstance(s, dict) else s.target,
            _norm(s["item"] if isinstance(s, dict) else s.item))


def refresh(session: Session, llm) -> dict:
    """Re-work the view from the confirmed profile; turn differences into new yes/no questions."""
    row = store.load_confirmed(session)
    owner = store.as_owner(row)
    if owner is None:
        return {"skipped": "no confirmed profile yet (profile read → profile confirm)"}
    view = derive_view(session, llm, owner)
    for old in session.scalars(select(CareerDirection).where(CareerDirection.status == "current")).all():
        old.status = "superseded"
    direction = CareerDirection(status="current", view=view.model_dump(), profile_id=row.id, resume_hash=row.resume_hash)
    session.add(direction)
    session.flush()
    proposed = suggestions_from(view, _current(owner))
    keys = {_key(p) for p in proposed}
    open_now = session.scalars(select(DirectionSuggestion).where(DirectionSuggestion.status == "open")).all()
    for s in open_now:
        if _key(s) not in keys and s.strength != "taste":  # pattern-based ones come from you, not this view
            s.status = "stale"           # no longer suggested
    open_keys = {_key(s) for s in open_now if s.status == "open"}
    declined = session.scalars(select(DirectionSuggestion).where(DirectionSuggestion.status == "declined")).all()
    new = []
    for p in proposed:
        if _key(p) in open_keys:
            continue
        said_no = [d for d in declined if _key(d) == _key(p)]
        # You said no: ask again only with a new resume AND a strong case.
        if said_no and (any(d.resume_hash == row.resume_hash for d in said_no) or p["strength"] != "strong"):
            continue
        s = DirectionSuggestion(direction_id=direction.id, status="open", resume_hash=row.resume_hash, **p)
        session.add(s)
        new.append(s)
        open_keys.add(_key(p))  # the AI listing the same thing twice asks once
    session.flush()
    if new:
        lines = [f"[{s.id}] {describe(s)}" for s in new]
        notify(session, "needs_you", "needs_you", f"Your career direction: {len(new)} suggestion(s) to answer",
               detail=" · ".join(lines) + " — answer with: direction yes <id> / direction no <id>",
               subject=("direction", str(direction.id)))
    audit.record(session, "m5", "direction_refreshed", f"Re-worked your career direction ({len(new)} new suggestion(s))",
                 subject_type="profile", subject_id=str(row.id))
    return {"direction": direction.id, "new_suggestions": len(new)}


def question_of(kind: str, target: str, item: str) -> str:
    """The question itself (without the reason) — the same words every time, so memory can tell when it
    was already answered."""
    return describe(DirectionSuggestion(kind=kind, target=target, item=item, why="")).removesuffix(" ()")


def describe(s: DirectionSuggestion) -> str:
    where = {"progression": "step-up families", "lateral": "sideways families", "excluded": "excluded families",
             "target_roles": "target roles"}.get(s.target, s.target)
    if s.kind == "add":
        return f"Add '{s.item}' to your {where}? ({s.why})"
    if s.kind == "remove" and s.target == "target_roles":
        return f"Remove '{s.item}' from your {where}? ({s.why})"
    if s.kind == "remove":
        return f"Move '{s.item}' from your {where} to your excluded families? ({s.why})"
    if s.kind == "include":
        return f"Start including '{s.item}' (you'd excluded it) as {where}? ({s.why})"
    if s.kind == "appetite":
        return f"Change your openness to sideways moves to '{s.item}'? ({s.why})"
    return f"Change your track to {s.item}? ({s.why})"


# --- your answers ------------------------------------------------------------------------------------

class DirectionError(ValueError):
    pass


def _save_lists(session: Session, row: OwnerProfile, data: dict, why: str) -> None:
    OwnerStub.model_validate(data)  # never save something the app can't use
    row.data = data
    session.flush()
    store.invalidate()
    audit.record(session, "m5", "direction_changed", why, subject_type="profile", subject_id=str(row.id))


def answer(session: Session, suggestion_id: int, yes: bool) -> DirectionSuggestion:
    s = session.get(DirectionSuggestion, suggestion_id)
    if s is None or s.status != "open":
        raise DirectionError(f"no open suggestion {suggestion_id} (see: direction)")
    if not yes:
        s.status, s.answered_at = "declined", datetime.now(timezone.utc)
        audit.record(session, "m5", "direction_declined", f"You said no: {describe(s)}", subject_type="profile")
        _remember(session, s, "no")
        return s
    row = store.load_confirmed(session)
    if row is None:
        raise DirectionError("no confirmed profile")  # checked before anything is marked
    data = dict(row.data)
    fam = {k: list(v) for k, v in (data.get("role_families") or {}).items()}
    for lst in LISTS:
        fam.setdefault(lst, [])
    item = s.item.strip()

    def drop(name: str, lists=LISTS):
        for lst in lists:
            fam[lst] = [x for x in fam[lst] if _norm(x) != _norm(name)]

    if s.target == "target_roles":
        roles = [r for r in data.get("target_roles") or [] if _norm(r) != _norm(item)]
        data["target_roles"] = roles + [item] if s.kind == "add" else roles
    elif s.kind == "appetite":
        data["lateral_appetite"] = item
    elif s.kind == "track":
        data["track"] = item.split(" ")[0]
        data["open_to_management_track"] = "open to managing" in item
    elif s.kind in ("add", "include"):
        drop(item)
        fam[s.target].append(item)
    elif s.kind == "remove":
        drop(item)
        fam["excluded"].append(item)
    data["role_families"] = fam
    _save_lists(session, row, data, f"You said yes: {describe(s)}")
    s.status, s.answered_at = "accepted", datetime.now(timezone.utc)  # only once the change is saved
    _remember(session, s, "yes")
    return s


def _remember(session: Session, s: DirectionSuggestion, answer_text: str) -> None:
    try:  # Part 5c: your answer is remembered (asked at most once; shown in "what the agent believes")
        from app.modules.m5_profile.memory import record_answer

        with session.begin_nested():
            record_answer(session, question_of(s.kind, s.target, s.item), answer_text, module="m5.direction",
                          extra={"resume_hash": s.resume_hash})
    except Exception as e:
        log.warning("couldn't remember the answer: %r", e)


# --- your own edits ---------------------------------------------------------------------------------

def export(session: Session, path: Path = REVIEW_FILE) -> Path:
    row = store.load_confirmed(session)
    if row is None:
        raise DirectionError("no confirmed profile yet (profile read → profile confirm)")
    lists = {k: row.data.get(k) for k in EDITABLE}
    header = ("# Your career direction: what the agent looks for. Edit the lists, save, then run:\n"
              "#     docker compose exec api python -m app.cli direction confirm\n"
              "# lateral_appetite: low (never sideways) | medium (only at step-up brands) | high (anywhere not a step down)\n"
              "# track: individual_contributor | people_manager\n\n")
    path.write_text(header + yaml.safe_dump(lists, sort_keys=False, allow_unicode=True, width=100), encoding="utf-8")
    return path


def confirm_edit(session: Session, text: str) -> OwnerProfile:
    try:
        edited = yaml.safe_load(text) or {}
    except yaml.YAMLError as e:
        raise DirectionError(f"the file isn't valid YAML: {e}") from e
    unknown = set(edited) - set(EDITABLE)
    if unknown:
        raise DirectionError(f"only these can be edited here: {', '.join(EDITABLE)} (not {', '.join(sorted(unknown))})")
    row = store.load_confirmed(session)
    if row is None:
        raise DirectionError("no confirmed profile")
    data = {**row.data, **{k: v for k, v in edited.items() if v is not None}}
    try:
        _save_lists(session, row, data, "You edited your career direction")
    except ValidationError as e:
        raise DirectionError("; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors()[:6])) from e
    return row


# --- reading it ----------------------------------------------------------------------------------------

def current_view(session: Session) -> CareerDirection | None:
    return session.scalar(select(CareerDirection).where(CareerDirection.status == "current")
                          .order_by(CareerDirection.id.desc()))


def explained(session: Session) -> dict:
    """Your lists, each with the agent's reason (or 'your choice'), plus open suggestions — for `direction`
    and the Profile screen."""
    owner = store.as_owner(store.load_confirmed(session))
    if owner is None:
        return {}
    cur = _current(owner)
    d = current_view(session)
    reasons = {_norm(f["family"]): f for f in (d.view.get("families", []) if d else [])}
    title_reasons = {_norm(t["title"]): t for t in (d.view.get("target_roles", []) if d else [])}
    out = {"position": d.view.get("position") if d else None, "trajectory": d.view.get("trajectory") if d else None,
           "lateral_appetite": cur["lateral_appetite"], "track": cur["track"],
           "open_to_management_track": cur["open_to_management_track"]}
    for lst in LISTS:
        out[lst] = [{"family": x, "why": (reasons.get(_norm(x)) or {}).get("why") or "your choice",
                     "condition": (reasons.get(_norm(x)) or {}).get("condition", "")} for x in cur[lst]]
    out["target_roles"] = [{"title": t, "why": (title_reasons.get(_norm(t)) or {}).get("why") or "your choice"}
                           for t in cur["target_roles"]]
    out["suggestions"] = [{"id": s.id, "text": describe(s)} for s in session.scalars(
        select(DirectionSuggestion).where(DirectionSuggestion.status == "open").order_by(DirectionSuggestion.id)).all()]
    return out


def needs_refresh(session: Session) -> bool:
    row = store.load_confirmed(session)
    d = current_view(session)
    return row is not None and (d is None or d.profile_id != row.id)
