"""Module 5a: your profile from your resume (spec §5.1, §5.4).

Flow: read config/resume.pdf (one AI read) → merge with what you already told the agent (owner_stub.yaml, or
your current confirmed profile) → a draft you check in review/profile.yaml → `profile confirm` (mandatory:
the one gate against a misread resume silently changing everything) → every module uses it.

The resume is read only — never edited, never tailored. A changed resume.pdf is noticed by its hash and
produces a new draft for you to confirm; until you do, the last confirmed profile stays in use.
Salary and notice period carry a "last confirmed" date; when they're older than ~6 months you're asked
once (in the summary mail) to re-confirm.
"""

import hashlib
import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config_loader import get_config
from app.config_schemas import OwnerStub
from app.core import audit
from app.core.notify import notify
from app.models.notification import Notification
from app.models.profile import OwnerProfile
from app.modules.m2_jobs.profile_text import resume_path, resume_text
from app.modules.m5_profile import store

log = logging.getLogger(__name__)

REVIEW_FILE = Path("/review/profile.yaml")
TIME_SENSITIVE = ("current_compensation", "notice_period_days")
RECONFIRM_AFTER_DAYS = 182
# What a resume can't tell: asked for if still empty after the merge.
GAP_FIELDS = {
    "current_compensation": "your current pay (salary rule)",
    "notice_period_days": "your notice period, in days",
    "level_ladder": "the level ladder at your company, lowest first",
    "current_level": "your rung on that ladder",
    "role_families": "which role families are a step up / excluded (Part 5b will work these out)",
    "target_roles": "job titles to search job sites for",
    "preferred_locations_india": "cities you'd work in",
    "mobile": "your mobile number, for your email signature",
}


# --- the AI read of the resume ------------------------------------------------------------------

class ResumeJob(BaseModel):
    company: str
    title: str = Field(description="Job title at that company (the latest one if promoted there).")
    start: str = Field(description="YYYY-MM, or '' if not stated.")
    end: str = Field(description="YYYY-MM, or '' if this is the current job.")


class ResumeRead(BaseModel):
    name: str = Field(description="The candidate's full name.")
    location: str = Field(description="City, Country if stated, else ''.")
    mobile: str = Field(description="Phone number as written, else ''.")
    linkedin_url: str = Field(description="LinkedIn profile URL if present, else ''.")
    career_field: str = Field(description="Their career field in a few words, e.g. 'analytics / data science'.")
    jobs: list[ResumeJob] = Field(description="Every job, most recent first.")
    total_experience_years: float = Field(description="Total full-time work experience in years (e.g. 3.5).")
    skills: list[str] = Field(description="Their main skills and tools, most important first, at most 15.")


SYSTEM = """You read one resume and fill in the requested fields literally from it. Never guess dates: leave
them '' if the resume doesn't state month and year. Answer in the requested JSON format."""


def file_hash(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def read_resume(session: Session, llm) -> ResumeRead:
    text = resume_text()
    if not text:
        raise ValueError("config/resume.pdf is missing or has no readable text")
    return llm.structured("judge", system=SYSTEM, user=text[:20000], schema=ResumeRead, purpose="m5.read_resume",
                          session=session, subject=("profile", "resume")).parsed


# --- merging with what you already said -------------------------------------------------------------

def _ym(s: str) -> str | None:
    s = (s or "").strip()
    try:
        datetime.strptime(s, "%Y-%m")
        return s
    except ValueError:
        return None


def merge(base: dict, read: ResumeRead) -> tuple[dict, dict]:
    """(profile data, notes). Facts come from the resume; your preferences and answers stay as they were.
    Notes say what came from where, what changed, and what's still missing."""
    data = {k: v for k, v in (base or {}).items()}
    notes: dict = {"from_resume": [], "changed": [], "kept": [], "gaps": [], "unclear": []}

    def put(field, value):
        old = data.get(field)
        if value in (None, "", []):
            return
        if old not in (None, "", []) and old != value:
            notes["changed"].append({"field": field, "was": old, "now": value})
        data[field] = value
        notes["from_resume"].append(field)

    put("name", read.name.strip())
    put("location", read.location.strip())
    if read.mobile.strip() and not data.get("mobile"):
        put("mobile", read.mobile.strip())
    if read.linkedin_url.strip() and not data.get("linkedin_url"):
        put("linkedin_url", read.linkedin_url.strip())
    if not data.get("career_field"):
        put("career_field", read.career_field.strip())

    jobs = []
    for j in read.jobs:
        start = _ym(j.start)
        if not start:
            notes["unclear"].append(f"no start month for {j.title} at {j.company}: please add it")
            continue
        jobs.append({"company": j.company.strip(), "title": j.title.strip(), "start": start, "end": _ym(j.end)})
    if jobs:
        put("employment_history", jobs)
        current = next((j for j in jobs if j["end"] is None), None)
        if current:
            put("current_role", {"title": current["title"], "company": current["company"], "started": current["start"]})
        else:
            notes["unclear"].append("no current job found on the resume: please check current_role")
    years = store.experience_years(data.get("employment_history"))
    if years is not None:  # from the job dates, never the AI's own estimate
        put("total_experience_years", years)
    elif read.total_experience_years:
        put("total_experience_years", round(float(read.total_experience_years) * 2) / 2)
        notes["unclear"].append("total_experience_years is the resume reader's estimate: job dates were missing")
    skills = data.get("skills") or {}
    if not (skills.get("must_have") or skills.get("nice_to_have")) and read.skills:
        put("skills", {"must_have": read.skills[:8], "nice_to_have": read.skills[8:15]})
    notes["kept"] = sorted(k for k in data if k not in notes["from_resume"])
    for field, why in GAP_FIELDS.items():
        if data.get(field) in (None, "", [], {}) or (field == "role_families" and not any((data.get(field) or {}).values())):
            notes["gaps"].append(f"{field}: {why}")
    return data, notes


def _base(session: Session) -> dict:
    """What the agent already knows: your confirmed profile, else owner_stub.yaml."""
    row = store.load_confirmed(session)
    if row is not None:
        return dict(row.data)
    stub = get_config().owner_file
    return stub.model_dump(mode="json") if stub else {}


def make_draft(session: Session, llm) -> OwnerProfile:
    read = read_resume(session, llm)
    data, notes = merge(_base(session), read)
    for old in session.scalars(select(OwnerProfile).where(OwnerProfile.status == "draft")).all():
        old.status = "superseded"  # one draft at a time
    row = OwnerProfile(status="draft", data=data, resume_read=read.model_dump(), notes=notes,
                       resume_hash=file_hash(resume_path()), confirmed_fields={})
    session.add(row)
    session.flush()
    audit.record(session, "m5", "profile_read", "Read your resume into a draft profile — waiting for you to confirm",
                 subject_type="profile", subject_id=str(row.id), detail={"gaps": notes["gaps"]})
    return row


# --- the review file ----------------------------------------------------------------------------------

def _comment_block(notes: dict, draft_id: int | None = None) -> str:
    lines = [f"# draft: {draft_id}" if draft_id else "# draft: none",
             "# Your profile, read from config/resume.pdf. Check it, correct anything, save, then run:",
             "#     docker compose exec api python -m app.cli profile confirm",
             "# Nothing uses it until you confirm. Dates are \"YYYY-MM\" and must stay in quotes.", "#"]
    if notes.get("gaps"):
        lines.append("# PLEASE FILL IN (a resume can't say these):")
        lines += [f"#   - {g}" for g in notes["gaps"]]
    if notes.get("unclear"):
        lines.append("# PLEASE CHECK:")
        lines += [f"#   - {u}" for u in notes["unclear"]]
    if notes.get("changed"):
        lines.append("# Changed compared with what the agent had before:")
        lines += [f"#   - {c['field']}: was {c['was']!r}" for c in notes["changed"]]
    if notes.get("from_resume"):
        lines.append("# Read from your resume: " + ", ".join(sorted(set(notes["from_resume"]))))
    lines.append("")
    return "\n".join(lines)


def export(row: OwnerProfile, path: Path = REVIEW_FILE) -> Path:
    path.write_text(_comment_block(row.notes or {}, getattr(row, "id", None)) + yaml.safe_dump(row.data, sort_keys=False, allow_unicode=True,
                                                                     width=100), encoding="utf-8")
    return path


class ProfileError(ValueError):
    pass


def parse_review(text: str) -> dict:
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as e:
        raise ProfileError(f"the file isn't valid YAML: {e}") from e
    try:
        OwnerStub.model_validate(data)
    except ValidationError as e:
        problems = "; ".join(f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors()[:8])
        raise ProfileError(f"please fix: {problems}") from e
    return data


# --- confirming ---------------------------------------------------------------------------------------

def confirm(session: Session, data: dict, *, row: OwnerProfile | None = None) -> OwnerProfile:
    """Your check of the draft (or your edit of the confirmed profile) becomes the profile everything uses."""
    owner = OwnerStub.model_validate(data)  # raises on anything invalid
    data = owner.model_dump(mode="json")
    current = store.load_confirmed(session)
    row = row or latest_draft_or_new(session)
    today = date.today().isoformat()
    fields = dict(current.confirmed_fields) if current else {}
    for f in TIME_SENSITIVE:
        if data.get(f) not in (None, "", {}):
            fields[f] = today  # you just looked at them
    if current is not None and current.id != row.id:
        current.status = "superseded"
    row.status, row.data, row.confirmed_at, row.confirmed_fields = "confirmed", data, datetime.now(timezone.utc), fields
    if row.resume_hash is None:
        row.resume_hash = (current.resume_hash if current else None) or file_hash(resume_path())
    for n in session.scalars(select(Notification).where(Notification.subject_type == "profile",
                                                        Notification.read_at.is_(None))).all():
        n.read_at = datetime.now(timezone.utc)
    _remember_time_sensitive(session, data)
    session.flush()
    store.invalidate()
    audit.record(session, "m5", "profile_confirmed", "You confirmed your profile; every module now uses it",
                 subject_type="profile", subject_id=str(row.id))
    return row


def latest_draft_or_new(session: Session) -> OwnerProfile:
    draft = store.latest_draft(session)
    if draft is not None:
        return draft
    row = OwnerProfile(status="draft", data={}, notes={}, confirmed_fields={}, resume_hash=file_hash(resume_path()))
    session.add(row)
    session.flush()
    return row


def edit_current(session: Session) -> OwnerProfile:
    """A draft that starts from your confirmed profile (to change an answer, or re-confirm pay/notice)."""
    current = store.load_confirmed(session)
    if current is None:
        raise ProfileError("no confirmed profile yet: run `profile read` first")
    for old in session.scalars(select(OwnerProfile).where(OwnerProfile.status == "draft")).all():
        old.status = "superseded"
    row = OwnerProfile(status="draft", data=dict(current.data), notes={"gaps": [], "from_resume": []},
                       resume_hash=current.resume_hash, confirmed_fields={})
    session.add(row)
    session.flush()
    return row


# --- keeping it fresh --------------------------------------------------------------------------------

def stale_fields(row: OwnerProfile | None, today: date | None = None) -> list[str]:
    if row is None:
        return []
    today = today or date.today()
    out = []
    for f in TIME_SENSITIVE:
        when = (row.confirmed_fields or {}).get(f)
        if row.data.get(f) in (None, "", {}):
            continue
        if when is None or today - date.fromisoformat(when) > timedelta(days=RECONFIRM_AFTER_DAYS):
            out.append(f)
    return out


def _asked_recently(session: Session, key: str, days: int = 30) -> bool:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    return session.scalar(select(Notification.id).where(Notification.subject_type == "profile",
                                                        Notification.subject_id == key,
                                                        Notification.created_at >= since).limit(1)) is not None


def check(session: Session, llm=None) -> dict:
    """Daily: a new resume.pdf → a draft to confirm; stale pay/notice → one reminder; no profile → onboarding."""
    out: dict = {}
    current = store.load_confirmed(session)
    draft = store.latest_draft(session)
    h = file_hash(resume_path())
    waiting = draft is not None and draft.resume_hash == h
    if h and llm is not None and not waiting and (current is None or current.resume_hash != h):
        try:
            row = make_draft(session, llm)
        except ValueError as e:  # e.g. a scanned PDF with no readable text: say so once, don't fail daily
            if not _asked_recently(session, "unreadable"):
                notify(session, "needs_you", "needs_you", "Your resume couldn't be read",
                       detail=f"{e}. Save it as a text PDF (not a scan) in config/resume.pdf.",
                       subject=("profile", "unreadable"))
            return {"unreadable": str(e)}
        out["draft"] = row.id
        what = "Your resume changed" if current else "Your profile is ready to check"
        notify(session, "needs_you", "needs_you", f"{what}: please confirm it",
               detail="Run: profile export — check review/profile.yaml — then: profile confirm. "
                      "Until then the agent keeps using " + ("your last confirmed profile." if current else "owner_stub.yaml."),
               subject=("profile", "draft"))
    for f in stale_fields(current):
        if not _asked_recently(session, f):
            label = {"current_compensation": "your current pay", "notice_period_days": "your notice period"}[f]
            notify(session, "needs_you", "needs_you", f"Is {label} still right?",
                   detail="It was last confirmed over 6 months ago. To update or re-confirm: profile edit, then "
                          "profile confirm.", subject=("profile", f))
            out.setdefault("reminders", []).append(f)
    return out


def draft_id_in(text: str) -> int | None:
    """The draft a review file was written from (its first line), so a stale file can't be confirmed."""
    first = text.split("\n", 1)[0].strip()
    if first.startswith("# draft:"):
        value = first.split(":", 1)[1].strip()
        return int(value) if value.isdigit() else None
    return None


def _remember_time_sensitive(session: Session, data: dict) -> None:
    """Part 5c: your pay and notice period are remembered as answers that go stale after ~6 months."""
    nested = None
    try:
        from app.modules.m5_profile.memory import record_answer

        nested = session.begin_nested()  # a memory problem rolls back only this, never your confirm
        pay = data.get("current_compensation") or {}
        if pay.get("annual_ctc"):
            record_answer(session, "What is your current pay?", f"{pay['annual_ctc']} {pay.get('currency', 'INR')} a year",
                          module="m5.profile", expires_days=RECONFIRM_AFTER_DAYS)
        if data.get("notice_period_days") is not None:
            record_answer(session, "What is your notice period?", f"{data['notice_period_days']} days",
                          module="m5.profile", expires_days=RECONFIRM_AFTER_DAYS)
        nested.commit()
    except Exception as e:
        if nested is not None and nested.is_active:
            nested.rollback()
        log.warning("couldn't remember pay / notice: %r", e)
