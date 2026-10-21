"""Why you passed on a role (Part 5c): the one-tap reasons you give, and the agent's "probably" reasons
worked out from the role's own facts when you don't. A reason you give always replaces a guess.

The guesses come only from facts the agent already holds (your level vs the role's, pay vs yours or your
floor, the company's brand / services flavour, location vs your preferred cities, hours / travel, years asked
for, a sideways family). Nothing is invented: when no fact explains a skip, it's "unclear".
"""

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

CHIPS = {
    "too_junior": "Too junior", "too_senior": "Too senior", "not_my_field": "Not my field",
    "location": "Location", "company": "Company", "pay": "Pay", "hours_travel": "Hours / travel", "other": "Other",
}
_TEXT_TO_CHIP = [
    (r"junior|below my level|too low a level|entry", "too_junior"),
    (r"senior|too high|above my level|too many years", "too_senior"),
    (r"field|domain|not interested|not my area|not relevant|wrong role|not for me", "not_my_field"),
    (r"location|city|relocat|commute|office|onsite|hyderabad|mumbai|pune|chennai|delhi|noida|gurgaon|bangalore|bengaluru", "location"),
    (r"company|brand|employer|services|consult|culture|startup", "company"),
    (r"pay|salary|ctc|lpa|money|compensation|package", "pay"),
    (r"travel|shift|hours|night|weekend|on.?call", "hours_travel"),
]


def chips_from_text(reason: str | None) -> list[str]:
    """Your typed reason → the matching chips (several can apply); empty when nothing matches."""
    text = (reason or "").lower()
    out = [chip for pattern, chip in _TEXT_TO_CHIP if re.search(pattern, text)]
    return list(dict.fromkeys(out)) or (["other"] if text.strip() else [])


def _city(location: str | None) -> str:
    return (location or "").split(",")[0].strip().lower()


def infer(session: Session, cluster_id: int, owner) -> list[dict]:
    """The role's facts that would explain passing on it: [{chip, detail}]. Free (no AI)."""
    from app.models.assessment import JobAssessment
    from app.models.company import Company
    from app.models.job import JobPosting
    from app.models.score import JobScore
    from app.modules.m2_jobs.brand import brand_level

    score = session.scalar(select(JobScore).where(JobScore.cluster_id == cluster_id))
    if score is None or owner is None:
        return []
    a = session.get(JobAssessment, score.assessment_id)
    job = session.get(JobPosting, a.job_id) if a else None
    if a is None or job is None:
        return []
    read, comp, level, flags = a.read or {}, a.comp or {}, a.level or {}, [f.lower() for f in a.flags or []]
    out: list[dict] = []
    gap = level.get("gap")
    if gap is not None and gap < 0:
        out.append({"chip": "too_junior", "detail": f"{level.get('job')} is below your level"})
    elif gap is not None and gap >= 2:
        out.append({"chip": "too_senior", "detail": f"{level.get('job')} is well above your level"})
    years = read.get("years_experience_min")
    if isinstance(years, (int, float)) and years > 0 and owner.total_experience_years \
            and years >= owner.total_experience_years + 3:
        out.append({"chip": "too_senior", "detail": f"asks for {years:g}+ years"})
    mid = comp.get("mid_inr")
    if comp.get("verdict") in ("below", "marginal") or comp.get("below_floor"):
        out.append({"chip": "pay", "detail": f"pay about {mid / 1e5:.0f} LPA" if mid else "pay below yours"})
    company = session.get(Company, job.company_id)
    brand = brand_level(session, company) if company else "unknown"
    if brand == "down":
        out.append({"chip": "company", "detail": "a step down for you"})
    elif read.get("services_flavored") and brand != "up":
        out.append({"chip": "company", "detail": "services / consulting-heavy"})
    city = _city(job.location)
    preferred = {c.strip().lower() for c in owner.preferred_locations_india}
    if city and preferred and city not in preferred and not job.is_remote:
        out.append({"chip": "location", "detail": city.title()})
    if any(k in f for f in flags for k in ("shift", "travel", "on-call", "on call")):
        out.append({"chip": "hours_travel", "detail": "shift / travel / on-call"})
    if read.get("family_match") == "lateral":
        out.append({"chip": "not_my_field", "detail": f"{read.get('role_family')} is sideways for you"})
    return out


def explain(chips: list[str], inferred: list[dict], *, private: bool = False) -> str:
    """private=True: for text that may reach the AI service — pay amounts become just "pay"."""
    if chips:
        return "you said: " + ", ".join(CHIPS.get(c, c).lower() for c in chips)
    if inferred:
        return "probably: " + "; ".join("pay" if private and i["chip"] == "pay" else i["detail"] for i in inferred)
    return "probably: unclear — level, pay, location and company all fit you"


def effective(chips: list[str], inferred: list[dict]) -> list[str]:
    """The reasons that count: yours if you gave any, else the agent's guesses."""
    return list(chips) if chips else [i["chip"] for i in inferred]
