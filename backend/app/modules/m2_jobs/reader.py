"""Kind-2 AI read (spec §2.1): one cheap-model pass over a role's description.

Title = hint; description = truth. The read classifies the role's real family and rung
(on the owner's own ladder), checks the India/visa fine print and 6-day weeks, and
collects pay and the flags shown in the digest. Rules are applied afterwards, in code.
"""

from functools import lru_cache
from typing import Literal

from pydantic import BaseModel, Field, create_model

from app.config_schemas import OwnerStub

YesNo = Literal["yes", "no", "unclear"]

GEOGRAPHY_HELP = (
    "yes = an India location, remote-from-anywhere/remote-India, or explicit visa sponsorship for this role; "
    "no = needs local work authorization the candidate lacks (e.g. 'must be authorized to work in the US'), "
    "or onsite abroad with no sponsorship; unclear otherwise."
)
LEVEL_HELP = (
    "The rung on the candidate's ladder this role sits at, judged from years asked for, scope and "
    "reporting line - not the title."
)
SERVICES_HELP = (
    "True if the work is delivering projects for external clients (consulting delivery, outsourcing, agency work)."
)
PAY_HELP = (
    "If pay is not stated: your estimate of the annual base pay range for this role and location, "
    "low end; -1 if you can't estimate."
)


class Flags(BaseModel):
    travel: Literal["none", "some", "heavy", "unclear"]
    relocation_required: YesNo
    on_call: YesNo
    shift: Literal["standard", "us_overlap_flexible", "rigid_night_shift", "unclear"] = Field(
        description="rigid_night_shift = fixed graveyard hours; us_overlap_flexible = some overlap with US hours "
                    "but flexible; standard = normal local hours."
    )


@lru_cache(maxsize=8)
def read_model(ladder: tuple[str, ...]) -> type[BaseModel]:
    """The structured-output schema, built around the owner's own level ladder."""
    level_type = Literal[tuple([*ladder, "unclear"])] if ladder else Literal["unclear"]
    fields = {
        "doable_from_india": (YesNo, Field(description=GEOGRAPHY_HELP)),
        "geography_basis": (Literal["india_location", "remote_anywhere", "remote_india", "visa_sponsorship",
                                    "needs_local_work_authorization", "onsite_abroad_no_sponsorship", "unclear"], ...),
        "geography_evidence": (str, Field(description="The phrase the geography answer rests on, or 'none'.")),
        "six_day_week": (YesNo, ...),
        "role_family": (str, Field(description="Short label for what the job really is, from its duties, e.g. "
                                               "'data analytics', 'product analytics', 'strategy', 'data engineering'.")),
        "family_match": (Literal["progression", "lateral", "excluded", "other"],
                         Field(description="Compare role_family with the candidate's families.")),
        "level": (level_type, Field(description=LEVEL_HELP)),
        "years_experience_min": (int, Field(description="Minimum years of experience asked for; -1 if not stated.")),
        "people_management": (Literal["manages_people", "individual_contributor", "unclear"],
                              Field(description="manages_people only if the role has direct reports.")),
        "employment_type": (Literal["full_time", "part_time", "contract", "internship", "temporary", "unclear"], ...),
        "services_flavored": (bool, Field(description=SERVICES_HELP)),
        "stated_pay": (str, Field(description="Pay exactly as written in the posting, or 'none'.")),
        "pay_estimate_currency": (str, Field(description="ISO currency of the role's location, e.g. INR, USD.")),
        "pay_estimate_min": (int, Field(description=PAY_HELP)),
        "pay_estimate_max": (int, ...),
        "pay_estimate_confidence": (Literal["high", "medium", "low"], ...),
        "flags": (Flags, ...),
        "one_liner": (str, Field(description="What the job is, at most 15 words.")),
    }
    return create_model("JobRead", **fields)


SYSTEM = """You read one job posting for a job seeker based in India and fill in a structured assessment.
The title is only a hint: judge family, level and requirements from the description itself.
Be literal about geography fine print ("must be authorized to work in...", "visa sponsorship available").
If the description is missing, judge from the title and location and mark uncertain fields "unclear".

Role families: analytics and insights work applied to ANY business domain (marketing, sales, finance,
operations, customer, commercial, pharma, product) counts as the candidate's core "business analytics"
progression family — unless the work is mainly one of the candidate's EXCLUDED families, which always win
(e.g. a role that is mostly risk modelling or ML model building is excluded if those are excluded).
Answer in the requested JSON format."""


def candidate_brief(owner: OwnerStub) -> str:
    fam = owner.role_families
    progression = sorted({*owner.target_roles, *fam.progression})
    lateral = [x for x in fam.lateral if x not in progression]
    return "\n".join([
        f"Career field: {owner.career_field}",
        f"Level ladder (low to high): {' -> '.join(owner.level_ladder) or 'not given'}",
        f"Current level: {owner.current_level or 'not given'} ({owner.total_experience_years or '?'} years' experience, "
        f"{owner.track.replace('_', ' ')})",
        f"Progression families: {', '.join(progression) or '-'}",
        f"Lateral families: {', '.join(lateral) or '-'}",
        f"Excluded families: {', '.join(fam.excluded) or '-'}",
        f"Work authorization: {owner.work_authorization.citizenship} citizen; visas held: "
        f"{', '.join(owner.work_authorization.visas_held) or 'none'}",
    ])


def job_brief(company: str, job, description_limit: int = 12_000) -> str:
    body = (job.description or "").strip()
    return "\n".join([
        f"Company: {company}",
        f"Title: {job.title}",
        f"Location: {job.location or 'not stated'}",
        f"Employment type (from the listing): {job.employment_type}",
        "",
        "--- description ---",
        body[:description_limit] if body else "(no description available)",
    ])
