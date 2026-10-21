"""Scoring funnel (spec §2.3) and routing bands (§2.4).

Stage 1 — free: cosine similarity between the candidate (resume + direction profile) and each role that
passed the rules. Stage 2 — paid: the top roles by similarity get one AI grade (judge model) with six
dimension scores, reasons, flags, skill gaps (`missing`, used by Module 3 drafts) and a one-liner.
Final score = weighted mean of the dimensions − penalties, then caps. Roles not graded (yet) are banded
by similarity alone and never above Maybe; leads (job-site/HN/WaaS) never above Maybe either.
Re-grades happen only when the posting's content or the resume/profile/weights change.
"""

import hashlib
import json
import logging
from collections import Counter
from datetime import date

from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config_loader import get_config
from app.config_schemas import DIMENSIONS
from app.core.embeddings import embed
from app.core.llm import LLM
from app.db import session_scope
from app.models.assessment import JobAssessment
from app.models.company import Company
from app.models.job import JobPosting
from app.models.llm_call import LLMCall
from app.models.score import JobScore
from app.modules.m1_companies.queue import start_of_today_utc
from app.modules.m2_jobs.brand import brand_level
from app.modules.m2_jobs.profile_text import (
    _normalise, cosine, job_text, profile_summary, profile_vector, resume_text, similarity_version,
)
from app.modules.m2_jobs.reader import candidate_brief

log = logging.getLogger(__name__)

# Unresolved questions keep a role out of the fast lane (spec §2.4); pay uncertainty alone does not.
FAST_LANE_BLOCKERS = ("geo: confirm", "level: unclear", "hours: confirm")


class Dimensions(BaseModel):
    skills_fit: int = Field(description="0-100: how well the candidate's skills cover what the role needs.")
    experience_fit: int = Field(description="0-100: years and kind of experience vs what's asked.")
    domain_fit: int = Field(description="0-100: industry/domain match (e.g. pharma commercial analytics).")
    career_growth: int = Field(description="0-100: how much this role advances the candidate's direction.")
    brand_value: int = Field(description="0-100: the employer's brand value FOR THIS candidate's career field.")
    location_fit: int = Field(description="0-100: fit with the candidate's location preferences / remote.")


class Grade(BaseModel):
    dimensions: Dimensions
    reasons: list[str] = Field(description="2-4 short, specific reasons for the fit, best first.")
    missing: list[str] = Field(description="Skills or experience the role asks for that the resume lacks.")
    flags: list[str] = Field(description="Anything the candidate should double-check; empty if none.")
    one_liner: str = Field(description="Why this role, in at most 18 words.")


GRADE_SYSTEM = """You grade how well one job fits one candidate, for their personal job search.
Score each dimension 0-100 honestly: 50 = an ordinary fit, 80+ = a strong fit, 95+ = rare.
Use only what the resume and profile say about the candidate; never assume skills that aren't there.
Answer in the requested JSON format."""


def _hash(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:32]


def grade_version() -> str:
    """What the AI grade itself depends on: resume text and profile. A change re-grades lazily.
    (Weights, bands and penalties only re-combine stored dimension scores — free, every round.)"""
    owner = get_config().owner
    return _hash([resume_text()[:20000], profile_summary(owner) if owner else "",
                  candidate_brief(owner) if owner else ""])


def grades_today(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(LLMCall).where(
        LLMCall.purpose == "m2.grade_job", LLMCall.created_at >= start_of_today_utc())) or 0


def band_for(score: int) -> str:
    bands = get_config().job_rules.scoring.bands
    if score >= bands.fast_lane:
        return "fast_lane"
    if score >= bands.inbox:
        return "inbox"
    if score >= bands.maybe:
        return "maybe"
    return "archive"


def similarity_proxy(similarity: float) -> int:
    """Ungraded roles: map similarity onto 0..ungraded_max (never above Maybe)."""
    ceiling = get_config().job_rules.scoring.ungraded_max_score
    low, high = 0.55, 0.85  # bge-small: unrelated ≈ 0.5, close match ≈ 0.85+
    frac = min(max((similarity - low) / (high - low), 0.0), 1.0)
    return round(frac * ceiling)


def tenure_years() -> float | None:
    owner = get_config().owner
    if not owner:
        return None
    y, m = map(int, owner.current_role.started.split("-"))
    today = date.today()
    return (today.year - y) + (today.month - m) / 12


def final_score(score_row: JobScore, assessment: JobAssessment) -> tuple[int, dict]:
    """Weighted mean of graded dimensions − penalties, then caps. Pure; explains itself in `breakdown`."""
    rules = get_config().job_rules.scoring
    breakdown: dict = {"penalties": [], "caps": []}
    if score_row.graded and score_row.grade.get("dimensions"):
        dims = score_row.grade["dimensions"]
        total_w = sum(rules.weights.get(d, 0) for d in DIMENSIONS)
        base = sum(dims.get(d, 0) * rules.weights.get(d, 0) for d in DIMENSIONS) / total_w
        breakdown["base"] = round(base, 1)
        breakdown["source"] = "ai_grade"
    else:
        base = similarity_proxy(score_row.similarity)
        breakdown["base"] = base
        breakdown["source"] = "similarity"

    score = base
    for p in assessment.penalties or []:
        score += p["points"]
        breakdown["penalties"].append(p)
    tenure = tenure_years()
    if any(f.startswith("lateral move") for f in assessment.flags or []) and tenure is not None \
            and tenure >= rules.lateral_tenure_years:
        score -= rules.lateral_penalty
        breakdown["penalties"].append({"name": f"lateral after {tenure:.1f} years in role",
                                       "points": -rules.lateral_penalty})

    if not score_row.graded and score > rules.ungraded_max_score:
        score = rules.ungraded_max_score
        breakdown["caps"].append("not AI-graded yet")
    if assessment.lead_only and score > rules.ungraded_max_score:
        score = rules.ungraded_max_score
        breakdown["caps"].append("lead only (no confirmed posting)")
    blockers = [f for f in assessment.flags or [] if f in FAST_LANE_BLOCKERS]
    if blockers and score >= get_config().job_rules.scoring.bands.fast_lane:
        score = get_config().job_rules.scoring.bands.fast_lane - 1
        breakdown["caps"].append("unresolved: " + ", ".join(blockers))
    return max(0, min(100, round(score))), breakdown


def _rebanding(row: JobScore, assessment: JobAssessment) -> None:
    row.score, row.breakdown = final_score(row, assessment)
    band = band_for(row.score)
    # The owner's own action wins over the score: skipped/thumbs-down roles leave the shortlist,
    # a Maybe-rescue lifts a role into the Inbox.
    if row.owner_action in ("skip", "thumbs_down"):
        band = "archive"
        row.breakdown = {**row.breakdown, "caps": [*row.breakdown.get("caps", []), f"you chose: {row.owner_action}"]}
    elif row.owner_action == "maybe_rescue" and band in ("maybe", "archive"):
        band = "inbox"
        row.breakdown = {**row.breakdown, "caps": [*row.breakdown.get("caps", []), "rescued by you"]}
    row.band = band


FEEDBACK_ACTIONS = ("act", "skip", "thumbs_up", "thumbs_down", "maybe_rescue")


def record_feedback(session: Session, row: JobScore, action: str, reason: str | None) -> None:
    """Store the owner's reaction (the signal for future weight tuning) and apply it to this role now."""
    from app.core import audit
    from app.models.score import JobFeedback

    if action not in FEEDBACK_ACTIONS:
        raise ValueError(f"action must be one of {FEEDBACK_ACTIONS}")
    session.add(JobFeedback(cluster_id=row.cluster_id, action=action, reason=reason,
                            score_at_time=row.score, band_at_time=row.band))
    row.owner_action = action
    assessment = session.get(JobAssessment, row.assessment_id)
    _rebanding(row, assessment)
    try:  # Part 5c: your decision, with its reason, is remembered and shown to future grades as an example
        from app.modules.m5_profile.memory import record_role_feedback

        job = session.get(JobPosting, assessment.job_id)
        read = assessment.read or {}
        with session.begin_nested():  # a memory problem rolls back only this, never your feedback
            record_role_feedback(session, company=session.get(Company, job.company_id).name, title=job.title,
                                 family=read.get("role_family"), level=read.get("level"), action=action,
                                 reason=reason, cluster_id=row.cluster_id)
    except Exception as e:  # memory is a help, never a reason to lose your feedback
        log.warning("couldn't remember feedback: %r", e)
    audit.record(session, "m2", "job_feedback", f"You marked a role '{action}'" + (f": {reason}" if reason else ""),
                 subject_type="job_cluster", subject_id=str(row.cluster_id),
                 detail={"action": action, "reason": reason, "score": row.score})


def update_similarities(session: Session, limit: int = 400) -> Counter:
    """Stage 1 for passed assessments without a current similarity. Free (local embeddings)."""
    counts: Counter = Counter()
    version = similarity_version()
    rows = session.execute(
        select(JobAssessment, JobPosting, JobScore)
        .join(JobPosting, JobPosting.id == JobAssessment.job_id)
        .outerjoin(JobScore, JobScore.cluster_id == JobAssessment.cluster_id)
        .where(JobAssessment.outcome == "passed", JobPosting.status == "open")
        .where((JobScore.id.is_(None)) | (JobScore.similarity_version != version)
               | (JobScore.assessment_id != JobAssessment.id))
        .limit(limit)
    ).all()
    if not rows:
        return counts
    me = profile_vector()
    vectors = embed([job_text(job, a.read) for a, job, _ in rows])
    for (a, job, row), vec in zip(rows, vectors):
        sim = cosine(me, _normalise(vec))
        if row is None:
            row = JobScore(cluster_id=a.cluster_id, assessment_id=a.id, similarity=sim,
                           similarity_version=version, score=0, band="archive")
            session.add(row)
            counts["new"] += 1
        else:
            row.assessment_id, row.similarity, row.similarity_version = a.id, sim, version
            counts["updated"] += 1
        _rebanding(row, a)
    # A role that stopped passing (closed, re-assessed as rejected) leaves the bands.
    stale = session.scalars(
        select(JobScore).join(JobAssessment, JobAssessment.id == JobScore.assessment_id)
        .join(JobPosting, JobPosting.id == JobAssessment.job_id)
        .where((JobAssessment.outcome != "passed") | (JobPosting.status != "open"), JobScore.band != "archive")
    ).all()
    for row in stale:
        row.band = "archive"
        counts["archived"] += 1
    return counts


def due_for_grading(session: Session, limit: int) -> list[JobScore]:
    version = grade_version()
    return session.scalars(
        select(JobScore).join(JobAssessment, JobAssessment.id == JobScore.assessment_id)
        .join(JobPosting, JobPosting.id == JobAssessment.job_id)
        .where(JobAssessment.outcome == "passed", JobPosting.status == "open", JobPosting.lead_only.is_(False),
               (JobScore.graded.is_(False)) | (JobScore.grade_version != version)
               | (JobScore.graded_content_hash != JobAssessment.content_hash))
        .order_by(JobScore.similarity.desc()).limit(limit)
    ).all()


def grade_one(session: Session, llm, row: JobScore) -> JobScore:
    cfg = get_config()
    owner = cfg.owner
    assessment = session.get(JobAssessment, row.assessment_id)
    job = session.get(JobPosting, assessment.job_id)
    company = session.get(Company, job.company_id)
    resume = resume_text()[:12000] or "(resume not available)"
    system = (GRADE_SYSTEM + "\n\nThe candidate:\n" + candidate_brief(owner)
              + f"\nMust-have skills: {', '.join(owner.skills.must_have) or '-'}"
              + f"\nNice-to-have skills: {', '.join(owner.skills.nice_to_have) or '-'}"
              + f"\nPreferred locations: {', '.join(owner.preferred_locations_india) or '-'}; "
                f"remote ok: {owner.remote_ok}; hybrid ok: {owner.hybrid_ok}"
              + "\n\n--- resume ---\n" + resume)  # stable across jobs → cached by the API
    read = assessment.read or {}
    user = "\n".join([
        f"Company: {company.name} (brand vs the candidate's career: {brand_level(session, company)})",
        f"Title: {job.title}", f"Location: {job.location or 'not stated'}",
        f"AI read: family {read.get('role_family')}, level {read.get('level')}, "
        f"min years {read.get('years_experience_min')}, {read.get('one_liner')}",
        f"Rule flags: {', '.join(assessment.flags or []) or 'none'}",
        "", "--- description ---", (job.description or "(no description)")[:10000],
    ])
    try:  # Part 5c: the candidate's own past decisions on similar roles, as examples (never as rules)
        from app.modules.m5_profile.memory import examples_for_job

        with session.begin_nested():
            past = examples_for_job(session, f"{job.title} at {company.name}. {read.get('one_liner') or ''}")
    except Exception as e:
        log.info("no memory examples: %r", e)
        past = []
    if past:
        user += ("\n\n--- the candidate's past decisions on similar roles (examples of their taste; the resume and "
                 "the description still decide) ---\n" + "\n".join(f"- {p}" for p in past))
    result = llm.structured("judge", system=system, user=user, schema=Grade, purpose="m2.grade_job",
                            session=session, subject=("job", str(job.id)))
    row.grade = result.parsed.model_dump(mode="json")
    row.graded, row.grade_version, row.graded_content_hash = True, grade_version(), assessment.content_hash
    row.cost_usd = round(result.cost_usd, 6)
    _rebanding(row, assessment)
    return row


def run_scoring(*, grade_limit: int | None = None, force: bool = False) -> dict:
    cfg = get_config().job_rules.scoring
    counts: Counter = Counter()
    with session_scope() as s:
        counts.update({f"similarity_{k}": v for k, v in update_similarities(s).items()})
        reband_all(s)  # free: weights/bands/penalty/brand edits take effect without new AI calls
    if not LLM.available() or get_config().owner is None:
        return dict(counts)
    with session_scope() as s:
        remaining = cfg.grades_per_day - grades_today(s)
        n = grade_limit or cfg.grades_per_tick
        if not force:
            n = min(n, max(remaining, 0))
        ids = [r.id for r in due_for_grading(s, n)] if n > 0 else []
    llm = LLM()
    cost, errors = 0.0, 0
    for score_id in ids:
        try:
            with session_scope() as s:
                row = grade_one(s, llm, s.get(JobScore, score_id))
                counts[f"graded_{row.band}"] += 1
                cost += row.cost_usd
        except Exception as e:
            errors += 1
            counts["grade_errors"] += 1
            log.warning("grading failed for score %s: %r", score_id, e)
            if errors >= 3:
                break
    result: dict = dict(counts)
    if ids:
        result["grade_cost_usd"] = round(cost, 4)
    return result


def reband_all(session: Session) -> int:
    """Free: recompute scores/bands from stored grades (after weights/bands/penalty edits + restart)."""
    rows = session.execute(select(JobScore, JobAssessment).join(
        JobAssessment, JobAssessment.id == JobScore.assessment_id)).all()
    for row, a in rows:
        _rebanding(row, a)
    return len(rows)
