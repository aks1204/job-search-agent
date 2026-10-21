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
    return (e.context or {}).get("module") != "m5.profile"


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
                         action: str, reason: str | None, cluster_id: int) -> MemoryEntry:
    what = {"act": "pursue it", "skip": "skip it", "thumbs_up": "liked it", "thumbs_down": "disliked it",
            "maybe_rescue": "rescue it from Maybe"}.get(action, action)
    meaning = (f"{title} at {company} ({family or 'role'}, {level or 'level unclear'}). "
               f"You chose to {what}" + (f": {reason}" if reason else "."))
    entry = remember(session, "precedent", meaning, {"action": action, "reason": reason, "title": title,
                                                     "company": company, "family": family},
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
        MemoryEntry.kind == "taste_signal", MemoryEntry.status == "active").limit(5)).all()]
    return out


# --- patterns (taste signals) ----------------------------------------------------------------------

def update_taste(session: Session, now: datetime | None = None) -> dict:
    """Count your reactions by role family over the last TASTE_WINDOW_DAYS: a repeated pattern becomes a
    taste signal; a strong 'you keep skipping X' on a step-up family becomes a direction suggestion."""
    from app.models.assessment import JobAssessment
    from app.models.score import JobFeedback, JobScore

    now = now or _now()
    since = now - timedelta(days=TASTE_WINDOW_DAYS)
    rows = session.execute(
        select(JobFeedback.action, JobAssessment.read)
        .join(JobScore, JobScore.cluster_id == JobFeedback.cluster_id)
        .join(JobAssessment, JobAssessment.id == JobScore.assessment_id)
        .where(JobFeedback.created_at >= since)
    ).all()
    neg, pos = Counter(), Counter()
    for action, read in rows:
        family = ((read or {}).get("role_family") or "").strip().lower()
        if not family:
            continue
        if action in ("skip", "thumbs_down"):
            neg[family] += 1
        elif action in ("act", "thumbs_up", "maybe_rescue"):
            pos[family] += 1
    seen, out = set(), {"signals": 0, "suggested": 0, "faded": 0}
    for family in set(neg) | set(pos):
        n, p = neg[family], pos[family]
        if n >= TASTE_MIN_EVENTS and n >= 3 * max(p, 1):
            meaning = f"You usually pass on {family} roles ({n} skipped vs {p} pursued in the last 6 months)."
            kind = "dislike"
        elif p >= TASTE_MIN_EVENTS and p >= 3 * max(n, 1):
            meaning = f"You usually go for {family} roles ({p} pursued vs {n} skipped in the last 6 months)."
            kind = "like"
        else:
            continue
        # One signal per family (by its exact family, never by similar wording); one you forgot stays forgotten.
        latest = session.scalar(select(MemoryEntry).where(
            MemoryEntry.kind == "taste_signal", MemoryEntry.content["family"].astext == family)
            .order_by(MemoryEntry.id.desc()))
        if latest is not None and (latest.content or {}).get("forgotten_by_owner"):
            continue
        content = {"family": family, "kind": kind, "skipped": n, "pursued": p}
        if latest is not None and latest.status in ("active", "archived"):
            from app.core.embeddings import embed_one

            latest.meaning, latest.embedding, latest.content, latest.status = meaning, embed_one(meaning), content, "active"
            entry = latest
        else:
            entry = remember(session, "taste_signal", meaning, content, context={"module": "m2"},
                             provenance={"source": "feedback_pattern"}, dedupe=False)
        entry.last_used_at = now
        seen.add(entry.id)
        out["signals"] += 1
        if kind == "dislike" and _suggest_exclusion(session, family, meaning):
            out["suggested"] += 1
    for entry in session.scalars(select(MemoryEntry).where(MemoryEntry.kind == "taste_signal",
                                                           MemoryEntry.status == "active")).all():
        if entry.id not in seen and not entry.pinned:
            entry.status = "archived"          # the pattern stopped repeating: it fades
            out["faded"] += 1
    return out


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
