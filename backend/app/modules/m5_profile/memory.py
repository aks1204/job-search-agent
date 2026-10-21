"""Module 5c: memory across the agent (spec §5.3), on top of the one memory table (app/core/memory.py).

Writes
  answers      — your yes/no to direction questions, "which role?" answers, re-confirmed pay / notice
  precedents   — your decisions with reasons: company keep/reject and brand calls (Module 1, already),
                 and now your feedback on roles (act / skip / 👍 / 👎, with your reason)
  taste_signal — a pattern in your feedback, only once it repeats (threshold-gated); a strong one becomes a
                 suggestion to edit your direction — never a change by itself
Reads
  asked at most once: before a question, memory is searched for the same question already answered
  job grading sees your most similar past decisions and your patterns as examples
Ageing (daily)
  money / timing answers → "needs reconfirm" after their date; patterns that stop repeating fade;
  anything unused for a year → archived (not deleted) unless pinned
After a resume update
  memories that clearly clash with the new profile are shown to you: keep, or forget (archive). The agent
  never changes or deletes a memory itself.
"""

import hashlib
import json
import logging
from collections import Counter
from datetime import datetime, timedelta, timezone

from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core import audit
from app.core.memory import remember, search
from app.core.notify import notify
from app.models.memory import MemoryEntry

log = logging.getLogger(__name__)

PRECEDENT_MIN = 0.6         # how similar a past decision must be to be shown as an example
TASTE_MIN_EVENTS = 4        # a pattern needs this many matching reactions …
TASTE_WINDOW_DAYS = 180     # … within this window, and fades when it stops repeating
UNUSED_ARCHIVE_DAYS = 365


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _shareable(e: MemoryEntry) -> bool:
    """Your pay and notice period never go to the AI service (the pay rules run on your machine)."""
    return (e.context or {}).get("module") not in ("m5.profile", "m2.pay")


# --- answers: asked at most once -------------------------------------------------------------------

def _same_question(session: Session, question: str, statuses=("active", "needs_reconfirm")) -> MemoryEntry | None:
    """Answers are keyed by the EXACT question text — questions that differ by one word ('sales' vs 'marketing'
    analytics) sound alike to embeddings but are different questions."""
    return session.scalar(select(MemoryEntry).where(
        MemoryEntry.kind == "answer", MemoryEntry.status.in_(statuses),
        MemoryEntry.content["question"].astext == question).order_by(MemoryEntry.id.desc()))


def record_answer(session: Session, question: str, answer: str, *, module: str, expires_days: int | None = None,
                  extra: dict | None = None) -> MemoryEntry:
    from app.core.embeddings import embed_one

    meaning = f"Q: {question} A: {answer}"
    expires_at = _now() + timedelta(days=expires_days) if expires_days else None
    content = {"question": question, "answer": answer, **(extra or {})}
    entry = _same_question(session, question)
    if entry is not None:            # the same question answered again (e.g. re-confirmed pay): update it
        entry.meaning, entry.embedding, entry.content = meaning, embed_one(meaning), content
        entry.expires_at, entry.evergreen, entry.status = expires_at, expires_days is None, "active"
        entry.context = {**(entry.context or {}), "module": module}
        return entry
    return remember(session, "answer", meaning, content, context={"module": module},
                    provenance={"source": "owner_answer"}, evergreen=expires_days is None, expires_at=expires_at,
                    dedupe=False)


def already_answered(session: Session, question: str) -> MemoryEntry | None:
    """The asked-at-most-once check: this exact question answered before and still current."""
    entry = _same_question(session, question, statuses=("active",))
    if entry is not None:
        entry.usage_count += 1
        entry.last_used_at = _now()
    return entry


# --- precedents from your feedback on roles -----------------------------------------------------------

def record_role_feedback(session: Session, *, company: str, title: str, family: str | None, level: str | None,
                         action: str, reason: str | None, cluster_id: int, chips: list[str] | None = None,
                         inferred: list[dict] | None = None) -> MemoryEntry:
    from app.modules.m5_profile.reasons import explain

    what = {"act": "pursue it", "skip": "skip it", "thumbs_up": "liked it", "thumbs_down": "disliked it",
            "maybe_rescue": "rescue it from Maybe"}.get(action, action)
    why = f": {reason}" if reason else (f" ({explain(chips or [], inferred or [], private=True)})"
                                       if action in ("skip", "thumbs_down") else ".")
    meaning = f"{title} at {company} ({family or 'role'}, {level or 'level unclear'}). You chose to {what}{why}"
    entry = remember(session, "precedent", meaning, {"action": action, "reason": reason, "title": title,
                                                     "company": company, "family": family,
                                                     "chips": chips or [], "inferred": inferred or []},
                     context={"module": "m2", "cluster_id": cluster_id}, provenance={"source": "owner_feedback"},
                     evergreen=True, dedupe=False)
    # Your newer decision on THIS role replaces the older one (kept as history); other roles are untouched.
    for old in session.scalars(select(MemoryEntry).where(
            MemoryEntry.kind == "precedent", MemoryEntry.status == "active", MemoryEntry.id != entry.id,
            MemoryEntry.context["module"].astext == "m2",
            MemoryEntry.context["cluster_id"].astext == str(cluster_id))).all():
        old.status, old.superseded_by_id = "superseded", entry.id
    return entry


def examples_for_job(session: Session, text: str, k: int = 3) -> list[str]:
    """Your most similar past decisions on roles, plus your active patterns — for the job grade."""
    hits = search(session, "precedent", text, k=k * 2, module="m2", mark_used=False)
    used = [e for e, sim in hits if sim >= PRECEDENT_MIN][:k]
    for e in used:  # only what is actually shown counts as "used" (for ageing)
        e.usage_count += 1
        e.last_used_at = _now()
    out = [e.meaning for e in used]
    out += [e.meaning for e in session.scalars(select(MemoryEntry).where(
        MemoryEntry.kind == "taste_signal", MemoryEntry.status == "active").limit(8)).all()
            if _shareable(e)][:5]  # pay patterns stay on your machine
    return out


# --- patterns (taste signals) ----------------------------------------------------------------------

def update_taste(session: Session, now: datetime | None = None) -> dict:
    """Patterns in your reactions over the last TASTE_WINDOW_DAYS — not just by role family but by the reason
    (yours, or the agent's 'probably'): location, pay, company, level, hours. A repeated pattern becomes a
    signal; a strong one becomes the matching yes/no suggestion (exclude a family / drop a city / set a pay
    floor). Skips explained by something else (pay, location…) don't count against the role family."""
    from app.models.assessment import JobAssessment
    from app.models.job import JobPosting
    from app.models.score import JobFeedback, JobScore
    from app.modules.m5_profile.reasons import _city, effective

    now = now or _now()
    since = now - timedelta(days=TASTE_WINDOW_DAYS)
    resets = _reset_dates(session)
    rows = session.execute(
        select(JobFeedback.action, JobFeedback.chips, JobFeedback.inferred, JobFeedback.created_at,
               JobAssessment.read, JobAssessment.comp, JobPosting.location)
        .join(JobScore, JobScore.cluster_id == JobFeedback.cluster_id)
        .join(JobAssessment, JobAssessment.id == JobScore.assessment_id)
        .join(JobPosting, JobPosting.id == JobAssessment.job_id)
        .where(JobFeedback.created_at >= since)
    ).all()
    neg, pos = Counter(), Counter()     # keys: ("family", x) | ("location", city) | ("company"|"level"|"hours", "")
    skipped_pay: list[int] = []
    pursued_pay: list[int] = []
    for action, chips, inferred, created, read, comp, location in rows:
        family = ((read or {}).get("role_family") or "").strip().lower()
        disliked = action in ("skip", "thumbs_down")
        liked = action in ("act", "thumbs_up", "maybe_rescue")
        reasons = effective(chips or [], inferred or []) if disliked else []
        mid = (comp or {}).get("mid_inr")
        if liked and mid:
            pursued_pay.append(mid)
        if disliked and family:
            reset = max((ts for name, ts in resets.items()
                         if _words(name) and (_words(name) <= _words(family) or _words(family) <= _words(name))),
                        default=None)
            old = reset is not None and created < reset
            if not old and (not reasons or "not_my_field" in reasons):   # only skips about the role itself
                neg[("family", family)] += 1
        elif liked and family:
            pos[("family", family)] += 1
        if disliked:
            if "location" in reasons and _city(location):
                neg[("location", _city(location))] += 1
            if "pay" in reasons and mid:
                skipped_pay.append(mid)
            for chip, dim in (("company", "company"), ("too_junior", "junior"), ("too_senior", "senior"),
                              ("hours_travel", "hours")):
                if chip in reasons:
                    neg[(dim, "")] += 1
        elif liked and _city(location):
            pos[("location", _city(location))] += 1

    seen, out = set(), {"signals": 0, "suggested": 0, "faded": 0}

    def signal(key: str, meaning: str, content: dict, module: str = "m2") -> MemoryEntry | None:
        # One signal per pattern (by its exact key, never by similar wording); one you forgot stays forgotten.
        cond = MemoryEntry.content["key"].astext == key
        if content.get("family"):  # patterns saved before keys existed were keyed by family only
            cond = cond | (MemoryEntry.content["key"].astext.is_(None)
                           & (MemoryEntry.content["family"].astext == content["family"])
                           & MemoryEntry.content["kind"].astext.is_distinct_from("reset"))
        latest = session.scalar(select(MemoryEntry).where(MemoryEntry.kind == "taste_signal", cond)
                                .order_by(MemoryEntry.id.desc()))
        if latest is not None and (latest.content or {}).get("forgotten_by_owner"):
            return None
        content = {**content, "key": key}
        if latest is not None and latest.status in ("active", "archived") and (latest.content or {}).get("kind") != "reset":
            from app.core.embeddings import embed_one

            latest.meaning, latest.embedding, latest.content, latest.status = meaning, embed_one(meaning), content, "active"
            entry = latest
        else:
            entry = remember(session, "taste_signal", meaning, content, context={"module": module},
                             provenance={"source": "feedback_pattern"}, dedupe=False)
        entry.last_used_at = now
        seen.add(entry.id)
        out["signals"] += 1
        return entry

    def strong(n: int, p: int) -> bool:
        return n >= TASTE_MIN_EVENTS and n >= 3 * max(p, 1)

    for (dim, value), n in list(neg.items()) + [(k, 0) for k in pos if k not in neg]:
        p = pos[(dim, value)]
        if dim == "family":
            if strong(n, p):
                meaning = f"You usually pass on {value} roles ({n} skipped vs {p} pursued in the last 6 months)."
                if signal(f"family:{value}", meaning, {"dim": "family", "family": value, "kind": "dislike",
                                                       "skipped": n, "pursued": p}) \
                        and _suggest_exclusion(session, value, meaning):
                    out["suggested"] += 1
            elif strong(p, n):
                signal(f"family:{value}", f"You usually go for {value} roles ({p} pursued vs {n} skipped in the last 6 months).",
                       {"dim": "family", "family": value, "kind": "like", "skipped": n, "pursued": p})
        elif dim == "location" and strong(n, p):
            meaning = f"You usually skip roles in {value.title()} ({n} skipped vs {p} pursued in the last 6 months)."
            if signal(f"location:{value}", meaning, {"dim": "location", "city": value, "kind": "dislike",
                                                     "skipped": n, "pursued": p}) \
                    and _suggest_other(session, "location", "locations", value.title(), meaning):
                out["suggested"] += 1
        elif dim in ("company", "junior", "senior", "hours") and strong(n, 0):
            text = {"company": "the company (brand or services-heavy firms)", "junior": "being below your level",
                    "senior": "being well above your level / years", "hours": "hours, shifts or travel"}[dim]
            signal(f"{dim}:", f"You often skip roles because of {text} ({n} in the last 6 months).",
                   {"dim": dim, "kind": "dislike", "skipped": n})
    if len(skipped_pay) >= TASTE_MIN_EVENTS:
        floor = -(-max(skipped_pay) // 100_000) * 100_000          # round up to a whole lakh
        if not any(x < floor for x in pursued_pay):                 # you never went for anything below it
            meaning = f"You usually skip roles paying below ~{floor / 1e5:.0f} LPA ({len(skipped_pay)} skipped)."
            if signal("pay:floor", meaning, {"dim": "pay", "kind": "dislike", "floor": floor, "skipped": len(skipped_pay)},
                      module="m2.pay") and _suggest_other(session, "pay_floor", "pay_floor", str(floor), meaning):
                out["suggested"] += 1
    for entry in session.scalars(select(MemoryEntry).where(MemoryEntry.kind == "taste_signal",
                                                           MemoryEntry.status == "active")).all():
        if entry.id not in seen and not entry.pinned:
            entry.status = "archived"          # the pattern stopped repeating: it fades
            out["faded"] += 1
    return out


def _suggest_other(session: Session, kind: str, target: str, item: str, why: str) -> bool:
    """A strong location / pay pattern → one yes/no (drop the city from your preferred list / set a pay floor)."""
    from app.models.direction import DirectionSuggestion
    from app.modules.m5_profile import direction as D
    from app.modules.m5_profile import store

    row = store.load_confirmed(session)
    owner = store.as_owner(row)
    current = D.current_view(session)
    if owner is None or current is None:
        return False
    if kind == "location" and item.lower() not in {c.strip().lower() for c in owner.preferred_locations_india}:
        return False                     # not on your list anyway: nothing to change
    if kind == "pay_floor":
        current_pay = owner.current_compensation.annual_ctc if owner.current_compensation else 0
        if int(item) <= current_pay or (owner.pay_floor_annual and owner.pay_floor_annual >= int(item)):
            return False                 # "more than your current pay" or your floor already covers it
    if already_answered(session, D.question_of(kind, target, item)):
        return False
    exists = session.scalar(select(DirectionSuggestion.id).where(
        DirectionSuggestion.status.in_(("open", "declined", "accepted")), DirectionSuggestion.kind == kind,
        func.lower(DirectionSuggestion.item) == item.lower()).limit(1))
    if exists:
        return False
    session.add(DirectionSuggestion(direction_id=current.id, kind=kind, target=target, item=item, why=why,
                                    strength="taste", status="open", resume_hash=row.resume_hash))
    notify(session, "needs_you", "needs_you", "A pattern in your choices",
           detail=f"{why} (see: direction)", subject=("direction", "taste"))
    return True


def _suggest_exclusion(session: Session, family: str, why: str) -> bool:
    """A strong 'you keep skipping X' where X is one of your step-up families → one yes/no to exclude it."""
    from app.models.direction import DirectionSuggestion
    from app.modules.m5_profile import direction as D
    from app.modules.m5_profile import store

    row = store.load_confirmed(session)
    owner = store.as_owner(row)
    current = D.current_view(session)
    if owner is None or current is None:
        return False
    target = D._where(D._current(owner), family)
    if target not in ("progression", "lateral"):
        return False
    if already_answered(session, D.question_of("remove", target, family)):
        return False  # you already said yes or no to this
    exists = session.scalar(select(DirectionSuggestion.id).where(
        DirectionSuggestion.status.in_(("open", "declined", "accepted")), DirectionSuggestion.kind == "remove",
        func.lower(DirectionSuggestion.item) == family.lower()).limit(1))
    if exists:
        return False
    # strength "taste": a suggestion from your own behaviour — a direction refresh never marks it stale.
    session.add(DirectionSuggestion(direction_id=current.id, kind="remove", target=target, item=family, why=why,
                                    strength="taste", status="open", resume_hash=row.resume_hash))
    notify(session, "needs_you", "needs_you", f"A pattern in your choices: {family}",
           detail=f"{why} Move it to your excluded families? (see: direction)", subject=("direction", "taste"))
    return True


# --- ageing -------------------------------------------------------------------------------------------

def age(session: Session, now: datetime | None = None) -> dict:
    now = now or _now()
    out = {"needs_reconfirm": 0, "archived": 0}
    for e in session.scalars(select(MemoryEntry).where(MemoryEntry.status == "active",
                                                       MemoryEntry.expires_at.is_not(None),
                                                       MemoryEntry.expires_at < now)).all():
        e.status = "needs_reconfirm"           # money / timing answers: re-confirmed when next used
        out["needs_reconfirm"] += 1
    cutoff = now - timedelta(days=UNUSED_ARCHIVE_DAYS)
    for e in session.scalars(select(MemoryEntry).where(
            MemoryEntry.status.in_(("active", "needs_reconfirm")), MemoryEntry.pinned.is_(False),
            func.coalesce(MemoryEntry.last_used_at, MemoryEntry.created_at) < cutoff)).all():
        e.status = "archived"
        out["archived"] += 1
    return out


# --- after a resume update: memories that clash with the new you ---------------------------------------

class Clash(BaseModel):
    id: int
    why: str = Field(description="One line: what in the new profile it clashes with.")


class Clashes(BaseModel):
    clashes: list[Clash] = Field(description="Only memories that CLEARLY no longer fit; usually none.")


CLASH_SYSTEM = """You compare a job seeker's updated profile with things the agent remembers about their past
preferences and decisions. List only the memories that clearly no longer fit the new profile (for example
'skipped as too senior' after a promotion to that level). When in doubt, leave it out. Answer in the
requested JSON format."""


def check_clashes(session: Session, llm) -> dict:
    """Once per newly confirmed profile: show clashing memories to you (keep / forget). Never changes them."""
    from app.modules.m2_jobs.reader import profile_brief
    from app.modules.m5_profile import store

    row = store.load_confirmed(session)
    owner = store.as_owner(row)
    if owner is None or (row.notes or {}).get("memory_checked"):
        return {}
    entries = session.scalars(select(MemoryEntry).where(
        MemoryEntry.status == "active", MemoryEntry.kind.in_(("precedent", "answer", "taste_signal")))
        .order_by(MemoryEntry.id.desc()).limit(80)).all()
    entries = [e for e in entries if _shareable(e)]
    result: dict = {"checked": len(entries), "clashes": 0}
    if entries:
        listing = "\n".join(f"[{e.id}] {e.meaning}" for e in entries)
        found = llm.structured("fast", system=CLASH_SYSTEM,
                               user=f"## Updated profile\n{profile_brief(owner)}\n\n## Remembered\n{listing}",
                               schema=Clashes, purpose="m5.memory_clash", session=session,
                               subject=("profile", str(row.id))).parsed
        by_id = {e.id: e for e in entries}
        for c in found.clashes:
            e = by_id.get(c.id)
            if e is None:
                continue
            e.content = {**(e.content or {}), "clash": {"profile_id": row.id, "why": c.why}}
            notify(session, "needs_you", "needs_you", "Your new resume may not fit something the agent remembers",
                   detail=f"“{e.meaning}” — {c.why}. Keep it: memory keep {e.id} · forget it: memory forget {e.id}",
                   subject=("memory", str(e.id)))
            result["clashes"] += 1
    row.notes = {**(row.notes or {}), "memory_checked": True}
    return result


# --- your control ----------------------------------------------------------------------------------------

class MemoryError(ValueError):
    pass


def act(session: Session, entry_id: int, action: str) -> MemoryEntry:
    e = session.get(MemoryEntry, entry_id)
    if e is None:
        raise MemoryError(f"no memory {entry_id}")
    if action == "pin":
        e.pinned = True
    elif action == "unpin":
        e.pinned = False
    elif action == "forget":
        e.status = "archived"
        e.content = {**(e.content or {}), "forgotten_by_owner": True}  # a forgotten pattern stays forgotten
    elif action == "restore":
        e.status = "active"
        e.last_used_at = _now()
        e.content = {k: v for k, v in (e.content or {}).items() if k != "forgotten_by_owner"}
    elif action == "keep":
        e.content = {k: v for k, v in (e.content or {}).items() if k != "clash"}
        e.last_used_at = _now()
    else:
        raise MemoryError("action must be pin, unpin, forget, restore or keep")
    audit.record(session, "m5", f"memory_{action}", f"You chose {action} for a memory: {e.meaning[:120]}",
                 subject_type="memory", subject_id=str(e.id))
    return e


def listing(session: Session, *, kind: str | None = None, include_archived: bool = False) -> list[MemoryEntry]:
    q = select(MemoryEntry).order_by(MemoryEntry.pinned.desc(), MemoryEntry.id.desc())
    if kind:
        q = q.where(MemoryEntry.kind == kind)
    if not include_archived:
        q = q.where(MemoryEntry.status.in_(("active", "needs_reconfirm")))
    return session.scalars(q.limit(300)).all()


class Belief(BaseModel):
    topic: str
    statements: list[str]


class Beliefs(BaseModel):
    topics: list[Belief] = Field(description="3-8 topics, e.g. 'Companies', 'Roles', 'Pay & timing', 'Style'.")


BELIEF_SYSTEM = """Summarise, in plain first-person-about-them sentences ("You prefer…", "You don't want…"), what an
assistant has learned about a job seeker from the memories listed. Group by topic. Don't invent anything that
isn't in the list. Answer in the requested JSON format."""


def beliefs(session: Session, llm) -> Beliefs:
    """'What the agent believes about you' — recomputed only when the memories change (cached)."""
    from app.core.redis_client import get_redis

    entries = listing(session)
    if not entries:
        return Beliefs(topics=[])
    key = "m5:beliefs:" + hashlib.sha256(json.dumps([[e.id, e.meaning, e.status] for e in entries]).encode()).hexdigest()[:24]
    try:
        cached = get_redis().get(key)
        if cached:
            return Beliefs.model_validate_json(cached)
    except Exception:
        cached = None
    text = "\n".join(f"- ({e.kind}) {e.meaning}" for e in [x for x in entries if _shareable(x)][:150])
    if any(not _shareable(x) for x in entries):
        text += "\n- (answer) Their current pay and notice period are on file (kept private)."
    result = llm.structured("fast", system=BELIEF_SYSTEM, user=text, schema=Beliefs, purpose="m5.beliefs",
                            session=session, subject=("profile", "beliefs")).parsed
    try:
        get_redis().set(key, result.model_dump_json(), ex=30 * 86400)
    except Exception:
        pass
    return result


def search_text(session: Session, query: str, k: int = 10) -> list[tuple[MemoryEntry, float]]:
    out = []
    for kind in ("answer", "precedent", "taste_signal"):
        out += search(session, kind, query, k=k)
    return sorted(out, key=lambda x: -x[1])[:k]


# --- after you say yes to a family you used to pass on ---------------------------------------------------

def _words(s: str | None) -> set[str]:
    import re

    return {w for w in re.findall(r"[a-z]+", (s or "").lower()) if len(w) >= 2 and w not in ("and", "the", "of", "roles")}


def _about_family(entry: MemoryEntry, family: str) -> bool:
    """Is this remembered skip about that family? By its recorded family name (word overlap), e.g.
    'data science' ~ 'applied data science', 'machine learning' ~ 'machine learning engineering'."""
    name = (entry.content or {}).get("family") or ""
    if " ".join(family.lower().split()) == " ".join(name.lower().split()):
        return True
    want, have = _words(family), _words(name)
    return bool(want and have) and (want <= have or have <= want)


def old_skips_of(session: Session, family: str) -> list[MemoryEntry]:
    rows = session.scalars(select(MemoryEntry).where(
        MemoryEntry.kind == "precedent", MemoryEntry.status == "active",
        MemoryEntry.context["module"].astext == "m2",
        MemoryEntry.content["action"].astext.in_(("skip", "thumbs_down")))).all()
    return [e for e in rows if _about_family(e, family)]


def after_family_included(session: Session, family: str, direction_id: int, resume_hash: str | None) -> dict:
    """You said yes to including `family`: (1) a 'you usually pass on it' pattern is set aside at once and a
    new one may form only from what you do from now on; (2) if you had skipped such roles, you're asked once
    whether to stop using those skips as examples."""
    from app.models.direction import DirectionSuggestion

    out = {"patterns_set_aside": 0, "asked": False}
    for e in session.scalars(select(MemoryEntry).where(MemoryEntry.kind == "taste_signal")).all():
        if _words(family) and _about_family(e, family) and (e.content or {}).get("kind") == "dislike":
            e.status = "archived"
            e.content = {**(e.content or {}), "reset_at": _now().isoformat(), "set_aside_by": "your direction change"}
            out["patterns_set_aside"] += 1
    # Remember the date of your yes even if no pattern existed yet, so old skips never count again.
    remember(session, "taste_signal", f"Reset: you started including {family} roles.",
             {"family": family, "kind": "reset", "reset_at": _now().isoformat()}, context={"module": "m2"},
             provenance={"source": "direction_change"}, dedupe=False).status = "archived"
    # Each old skip on its own merits: about the field → set aside; about something else → kept; unclear → asked.
    field, other, unclear = sort_skips(old_skips_of(session, family))
    import secrets

    batch = secrets.token_hex(3)
    for e in field:
        e.status = "archived"
        e.content = {**(e.content or {}), "set_aside_by": "your yes to including these roles", "set_aside_batch": batch}
    out.update(set_aside=len(field), kept=len(other))
    if field or other:
        lines = []
        if field:
            lines.append(f"Set aside {len(field)} earlier skip(s) that were about the field ("
                         + "; ".join(_short(e) for e in field[:3]) + ").")
        if other:
            lines.append(f"Kept {len(other)} that were about other things ("
                         + "; ".join(_short(e) for e in other[:3]) + ").")
        notify(session, "update", "happened", f"You now include {family} roles",
               detail=" ".join(lines) + (f" Undo: memory undo {batch}" if field else ""),
               subject=("memory", f"undo:{batch}"))
    already = session.scalar(select(DirectionSuggestion.id).where(
        DirectionSuggestion.kind == "memory", DirectionSuggestion.status == "open",
        func.lower(DirectionSuggestion.item) == family.lower()).limit(1))
    if unclear and not already:
        session.add(DirectionSuggestion(
            direction_id=direction_id, kind="memory", target="memory", item=family, strength="taste", status="open",
            resume_hash=resume_hash,
            why=f"{len(unclear)} earlier skip(s) without a reason I can work out: " + "; ".join(_short(e) for e in unclear[:3])))
        notify(session, "needs_you", "needs_you", f"You now include {family} roles",
               detail=f"I can't tell why you skipped {len(unclear)} earlier role(s) like this. "
                      "Stop using them as examples? (see: direction)", subject=("direction", "memory"))
        out["asked"] = True
    return out


def _reasons_of(e: MemoryEntry) -> list[str]:
    """Why a remembered skip happened: your chips / typed reason first, else the agent's probable reasons."""
    from app.modules.m5_profile.reasons import chips_from_text

    c = e.content or {}
    given = list(c.get("chips") or []) or chips_from_text(c.get("reason"))
    return given or [i.get("chip") for i in c.get("inferred") or []]


def sort_skips(skips: list[MemoryEntry]) -> tuple[list, list, list]:
    field, other, unclear = [], [], []
    for e in skips:
        reasons = [r for r in _reasons_of(e) if r != "other"] or _reasons_of(e)
        if "not_my_field" in reasons:
            field.append(e)
        elif reasons:
            other.append(e)
        else:
            unclear.append(e)
    return field, other, unclear


def _short(e: MemoryEntry) -> str:
    c = e.content or {}
    reasons = ", ".join(r.replace("_", " ") for r in _reasons_of(e)) or "no reason"
    return f"{c.get('title', 'a role')} at {c.get('company', '?')}: {reasons}"


def set_aside_examples(session: Session, family: str) -> int:
    """Your yes to the question: the earlier skips whose reason couldn't be worked out stop being shown to the
    AI as examples (archived, not deleted). Skips with a clear reason were already handled."""
    _, _, unclear = sort_skips(old_skips_of(session, family))
    for e in unclear:
        e.status = "archived"
        e.content = {**(e.content or {}), "set_aside_by": "you, after including these roles"}
    return len(unclear)


def undo_set_aside(session: Session, batch: str) -> int:
    """The [Undo] on 'set aside N earlier skips': they're used as examples again."""
    n = 0
    for e in session.scalars(select(MemoryEntry).where(MemoryEntry.content["set_aside_batch"].astext == batch)).all():
        cluster = (e.context or {}).get("cluster_id")
        newer = session.scalar(select(MemoryEntry.id).where(
            MemoryEntry.kind == "precedent", MemoryEntry.status == "active", MemoryEntry.id != e.id,
            MemoryEntry.context["cluster_id"].astext == str(cluster)).limit(1)) if cluster is not None else None
        if newer:  # you gave newer feedback on that role meanwhile: that one stands
            e.status, e.superseded_by_id = "superseded", newer
            continue
        e.status = "active"
        e.content = {k: v for k, v in (e.content or {}).items() if k not in ("set_aside_by", "set_aside_batch")}
        n += 1
    if n:
        audit.record(session, "m5", "memory_undo", f"You undid setting aside {n} earlier skip(s)", subject_type="memory")
    return n


def _reset_dates(session: Session) -> dict[str, datetime]:
    """Families whose patterns were reset by a direction change: only reactions after that date count."""
    out: dict[str, datetime] = {}
    for e in session.scalars(select(MemoryEntry).where(MemoryEntry.kind == "taste_signal")).all():
        when = (e.content or {}).get("reset_at")
        family = (e.content or {}).get("family")
        if when and family:
            ts = datetime.fromisoformat(when)
            out[family] = max(out.get(family, ts), ts)
    return out
