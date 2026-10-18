"""Draft engine (spec §3.2): ONE draft per role, identical for every recipient but the greeting name.

Inputs: the posting, the resume + profile, the company's fact card, the Module 2 grade, the scenario.
The owner's template (config/email_template.md) fixes the wording around the AI-written parts.

Truthfulness verifier (hard requirement): a second, cheaper pass lists every factual claim about the
candidate and checks it against the resume/profile. Unsupported claims get one rewrite; whatever is
still unsupported is cut before the draft reaches the owner (and the draft is flagged if a cut can't
be made cleanly). Owner decision: drafts never mention gaps — only why the candidate is a strong fit.
"""

import hashlib
import json
import logging
import re
from collections import Counter

from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config_loader import get_config
from app.core.llm import LLM
from app.db import session_scope
from app.models.assessment import JobAssessment
from app.models.company import Company
from app.models.contact import RoleChannel
from app.models.draft import OutreachDraft
from app.models.job import JobPosting
from app.models.llm_call import LLMCall
from app.models.score import JobScore
from app.modules.m1_companies.facts import fact_card, value
from app.modules.m1_companies.queue import start_of_today_utc
from app.modules.m2_jobs.profile_text import resume_path, resume_text
from app.modules.m2_jobs.reader import candidate_brief
from app.modules.m3_outreach.template import Template, fill, fit_subject, load_template

log = logging.getLogger(__name__)

DRAFTABLE = ("designated", "bench", "mailbox")


# --- what the AI returns -------------------------------------------------------------------------

class Point(BaseModel):
    heading: str = Field(description="Short name of the area, e.g. 'Field Force Effectiveness & GTM Analytics'.")
    text: str = Field(description="1-2 lines, first person, on exactly what the candidate did in that area.")


class DraftOut(BaseModel):
    role_name: str = Field(description="The job title as it should read in the email: no req codes, locations or levels in brackets.")
    role_short: str = Field(description="The job title shortened for the subject line (core title only).")
    intro: str = Field(description="The intro paragraph, following the template's intro instructions.")
    points: list[Point]


class Claim(BaseModel):
    part: str = Field(description="'intro' or 'point N' (1-based).")
    sentence: str = Field(description="The exact sentence containing the claim, copied verbatim from the draft.")
    claim: str = Field(description="The factual claim about the candidate, in a few words.")
    supported: bool = Field(description="True only if the resume or profile states or clearly implies it.")
    evidence: str = Field(description="A short quote from the resume/profile that supports it; '' if none.")


class TruthCheck(BaseModel):
    claims: list[Claim]


DRAFT_SYSTEM = """You write one short cold-outreach email for a job seeker, applying to one specific opening.
You write ONLY the parts the template leaves to you; the rest of the email is fixed text you will see.
Follow the template's instructions and rules exactly. Use only facts from the candidate's resume and
profile: never invent numbers, tools, clients, employers, titles or results. Write in the first person,
as the candidate. Answer in the requested JSON format."""

VERIFY_SYSTEM = """You fact-check a job application email against the candidate's own resume and profile.
List EVERY factual claim the email makes about the candidate (experience, years, employers, titles,
skills, tools, projects, numbers, results). Mark a claim supported only if the resume or profile states
it or clearly implies it; general enthusiasm or fit statements are not claims. Answer in the requested
JSON format."""


# --- inputs ----------------------------------------------------------------------------------------

def _years(owner) -> str:
    y = owner.total_experience_years
    if y is None:
        return ""
    return str(int(y)) if float(y).is_integer() else f"{y:g}"


def req_id(job: JobPosting) -> str:
    """A human job ID from the posting (e.g. Workday 'R-112054'), else the board's own id."""
    for source in (job.url or "", job.external_id or ""):
        if m := re.search(r"(?<![A-Za-z0-9])((?:JR|REQ|R)-?\d{4,})(?!\d)", source, re.I):  # "…_R-112054"
            return m.group(1).upper()
    ext = (job.external_id or "").rstrip("/")
    return ext.rsplit("/", 1)[-1][:40]


def sibling_req_ids(session: Session, cluster_id: int) -> list[str]:
    jobs = session.scalars(select(JobPosting).where(JobPosting.cluster_id == cluster_id, JobPosting.status == "open",
                                                    JobPosting.lead_only.is_(False))
                           .order_by(JobPosting.first_seen_at, JobPosting.id)).all()
    return sorted({r for j in jobs if (r := req_id(j))})  # stable order in the email (and in inputs_version)


def scenario_for(session: Session, cluster_id: int, req_ids: list[str]) -> str:
    """fresh | multi_req today. Reopening-after-silence, post-rejection and post-interview need the
    outreach/application history from Parts 3c-3d and Module 4; they plug in here."""
    return "multi_req" if len(req_ids) > 1 else "fresh"


SCENARIO_NOTES = {
    "fresh": "First contact about this opening.",
    "multi_req": "The company has several openings for this same role; the fixed text lists their job IDs.",
    "reopening": "The candidate wrote about this role before and heard nothing; don't mention that.",
    "post_rejection": "The candidate was turned down for a similar role earlier; never open with or mention it.",
    "post_interview": "The candidate interviewed here before; mention it once, briefly and positively.",
}


def _values(owner, company: Company, role_name: str, req_ids: list[str]) -> dict[str, str | None]:
    return {"role": role_name, "company": company.name, "years": _years(owner),
            "req_ids": f" (job IDs: {', '.join(req_ids)})" if len(req_ids) > 1 else "",
            "full_name": owner.name, "mobile": owner.mobile, "linkedin_url": owner.linkedin_url}


def inputs_version(t: Template, job: JobPosting, scenario: str, req_ids: list[str]) -> str:
    owner = get_config().owner
    blob = json.dumps([job.content_hash, hashlib.sha256(resume_text().encode()).hexdigest(), candidate_brief(owner),
                       owner.name, owner.mobile, owner.linkedin_url, owner.total_experience_years,
                       t.version, scenario, req_ids], default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:32]


def _system(t: Template) -> str:
    """Stable across every job (template + candidate + resume) → cached by the API."""
    owner = get_config().owner
    sections = "\n\n".join(f"### {name}\n{text}" for name, text in t.sections.items())
    return (DRAFT_SYSTEM + "\n\n## The template's instructions\n" + sections
            + f"\n\nWrite between {t.settings.points_min} and {t.settings.points_max} points."
            + "\n\n## The candidate\n" + candidate_brief(owner)
            + f"\nTotal experience: {_years(owner) or 'not given'} years"
            + "\n\n--- resume ---\n" + (resume_text()[:12000] or "(resume not available)"))


def _user(session: Session, t: Template, score: JobScore, a: JobAssessment, job: JobPosting, company: Company,
          scenario: str, req_ids: list[str]) -> str:
    card = fact_card(session, company.id)
    what = value(card, "what_they_build")
    grade = score.grade or {}
    fixed = fill(t.body, _values(get_config().owner, company, "<role>", req_ids))
    return "\n".join([
        f"Company: {company.name}" + (f" — {what}" if what and what != "unknown" else ""),
        f"Job title as posted: {job.title}", f"Location: {job.location or 'not stated'}",
        f"Scenario: {SCENARIO_NOTES[scenario]}",
        "Why this role fits the candidate (from an earlier assessment): " + "; ".join(grade.get("reasons") or [])
        if grade.get("reasons") else "",
        "", "--- the email's fixed text (you write {{intro}} and {{points}}; don't repeat what it says) ---", fixed,
        "", "--- job description ---", (job.description or "(no description)")[:9000],
    ])


# --- truth check -----------------------------------------------------------------------------------

def _as_text(d: DraftOut) -> str:
    return "\n".join(["intro: " + d.intro, *[f"point {i}: {p.heading}: {p.text}" for i, p in enumerate(d.points, 1)]])


def _verify(session: Session, llm, d: DraftOut, cluster_id: int) -> TruthCheck:
    owner = get_config().owner
    system = (VERIFY_SYSTEM + "\n\n## Candidate profile\n" + candidate_brief(owner)
              + f"\nTotal experience: {_years(owner) or 'not given'} years"
              + f"\nCurrent role: {owner.current_role.title} at {owner.current_role.company} since {owner.current_role.started}"
              + "\n\n--- resume ---\n" + (resume_text()[:12000] or "(resume not available)"))
    return llm.structured("fast", system=system, user=_as_text(d), schema=TruthCheck, purpose="m3.verify_draft",
                          session=session, subject=("job_cluster", str(cluster_id))).parsed


def strike(d: DraftOut, unsupported: list[Claim]) -> tuple[DraftOut, list[str], bool]:
    """Cut each unsupported sentence; drop points left empty. Returns (draft, cut sentences, clean?)."""
    intro, points, cut, clean = d.intro, [p.model_copy() for p in d.points], [], True
    for c in unsupported:
        s = c.sentence.strip()
        if not s:
            clean = False
            continue
        if s in intro:
            intro = " ".join(intro.replace(s, " ").split())
            cut.append(s)
            continue
        hit = False
        for p in points:
            if s in p.text:
                p.text = " ".join(p.text.replace(s, " ").split())
                hit = True
            elif s in f"{p.heading}: {p.text}" or (p.heading and s.startswith(p.heading)):
                p.text, hit = "", True
        if hit:
            cut.append(s)
        else:
            clean = False
    points = [p for p in points if len(p.text.strip(" .;:-")) > 3]
    return d.model_copy(update={"intro": intro, "points": points}), cut, clean


def write_draft(session: Session, llm, score: JobScore, a: JobAssessment, job: JobPosting, company: Company,
                t: Template | None = None) -> OutreachDraft:
    t = t or load_template()
    owner = get_config().owner
    rules = get_config().outreach.drafts
    req_ids = sibling_req_ids(session, score.cluster_id)
    scenario = scenario_for(session, score.cluster_id, req_ids)
    system = _system(t)
    user = _user(session, t, score, a, job, company, scenario, req_ids)
    subject_ref = ("job_cluster", str(score.cluster_id))
    cost = 0.0

    r = llm.structured("judge", system=system, user=user, schema=DraftOut, purpose="m3.draft",
                       session=session, subject=subject_ref)
    draft, cost = r.parsed, cost + r.cost_usd
    rounds = []
    for _ in range(rules.max_rewrites + 1):
        check = _verify(session, llm, draft, score.cluster_id)
        bad = [c for c in check.claims if not c.supported]
        rounds.append({"claims": len(check.claims), "unsupported": [c.model_dump() for c in bad]})
        if not bad or len(rounds) > rules.max_rewrites:
            break
        fix = "\n".join(f"- {c.part}: \"{c.sentence}\" (claim: {c.claim})" for c in bad)
        r = llm.structured("judge", system=system, purpose="m3.draft", session=session, subject=subject_ref,
                           schema=DraftOut, user=user + "\n\n--- your previous draft ---\n" + _as_text(draft)
                           + "\n\n--- these claims are NOT supported by the resume/profile ---\n" + fix
                           + "\nRewrite the draft: remove or correct exactly these claims; keep everything else.")
        draft, cost = r.parsed, cost + r.cost_usd

    cut, clean = [], True
    if bad:
        draft, cut, clean = strike(draft, bad)
    status = "ready" if clean and len(draft.points) >= t.settings.points_min else "flagged"

    values = _values(owner, company, draft.role_name, req_ids)
    points_text = "\n".join(f"{i}. {p.heading}: {p.text}" for i, p in enumerate(draft.points, 1))
    body = fill(t.body, values).replace("{{intro}}", draft.intro).replace("{{points}}", points_text)
    subject = fit_subject(t, values, draft.role_short)

    row = session.scalar(select(OutreachDraft).where(OutreachDraft.cluster_id == score.cluster_id))
    if row is None:
        row = OutreachDraft(cluster_id=score.cluster_id, version=0)
        session.add(row)
    row.version = (row.version or 0) + 1
    row.scenario, row.role_name, row.subject, row.body = scenario, draft.role_name, subject, body
    row.intro, row.points, row.req_ids = draft.intro, [p.model_dump() for p in draft.points], req_ids
    row.attach_resume, row.status = t.settings.attach_resume, status
    row.truth = {"rounds": rounds, "cut": cut, "clean": clean}
    row.inputs_version = inputs_version(t, job, scenario, req_ids)
    row.cost_usd = round(cost, 6)
    session.flush()
    return row


# --- per recipient ---------------------------------------------------------------------------------

def greeting_name(full_name: str | None) -> str | None:
    if not full_name:
        return None
    first = full_name.split()[0].strip(",.")
    return first if first.isalpha() or "-" in first else None


def render_for(draft: OutreachDraft, t: Template, full_name: str | None) -> tuple[str, str]:
    """(subject, body) exactly as one recipient would get it."""
    first = greeting_name(full_name)
    greeting = fill(t.settings.greeting, {"first_name": first}) if first else t.settings.greeting_without_name
    return draft.subject, draft.body.replace("{greeting}", greeting)


def attachment() -> tuple[str, int] | None:
    path = resume_path()
    return (path.name, path.stat().st_size) if path.is_file() else None


# --- the round -------------------------------------------------------------------------------------

def drafts_today(session: Session) -> int:
    """New/rewritten drafts today (first drafting call per draft; rewrites inside one draft don't count)."""
    return session.scalar(select(func.count(func.distinct(LLMCall.subject_id))).where(
        LLMCall.purpose == "m3.draft", LLMCall.created_at >= start_of_today_utc())) or 0


def due_drafts(session: Session, t: Template, cluster_ids: list[int] | None = None, force: bool = False):
    from app.modules.m3_outreach.pipeline import pursued_roles

    out = []
    for score, a, job, company in pursued_roles(session, cluster_ids):
        channel = session.get(RoleChannel, score.cluster_id)
        if channel is None or channel.mode not in DRAFTABLE:
            continue
        draft = session.scalar(select(OutreachDraft).where(OutreachDraft.cluster_id == score.cluster_id))
        if force or draft is None:
            out.append(score.cluster_id)
            continue
        req_ids = sibling_req_ids(session, score.cluster_id)
        if draft.inputs_version != inputs_version(t, job, scenario_for(session, score.cluster_id, req_ids), req_ids):
            out.append(score.cluster_id)
    return out


def run_drafts(*, cluster_ids: list[int] | None = None, force: bool = False) -> dict:
    rules = get_config().outreach.drafts
    counts: Counter = Counter()
    if not LLM.available() or get_config().owner is None:
        return {"skipped": "needs ANTHROPIC_API_KEY and config/owner_stub.yaml"}
    t = load_template()
    with session_scope() as s:
        todo = due_drafts(s, t, cluster_ids, force)
        left = max(rules.drafts_per_day - drafts_today(s), 0)
    n = len(todo) if cluster_ids else min(len(todo), rules.drafts_per_tick, left)
    llm, cost = LLM(), 0.0
    for cluster_id in todo[:n]:
        try:
            with session_scope() as s:
                from app.modules.m3_outreach.pipeline import pursued_roles

                row = next(iter(pursued_roles(s, [cluster_id])), None)
                if row is None:
                    continue
                draft = write_draft(s, llm, *row, t=t)
                counts[f"drafted_{draft.status}"] += 1
                cost += draft.cost_usd
        except Exception as e:
            counts["errors"] += 1
            log.warning("drafting failed for cluster %s: %r", cluster_id, e)
    if todo[n:]:
        counts["waiting_for_tomorrow"] = len(todo) - n
    result = dict(counts)
    if n:
        result["cost_usd"] = round(cost, 4)
    return result
