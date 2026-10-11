"""Pydantic schemas for config/*.yaml. Unknown keys are rejected so typos fail loudly at startup."""

from datetime import date, time
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _year_month(v):
    if isinstance(v, date):
        return f"{v.year:04d}-{v.month:02d}"
    return v


YearMonth = Annotated[str, BeforeValidator(_year_month), Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")]


# --- app.yaml -------------------------------------------------------------

class QuietHours(Strict):
    start: time
    end: time


class AppConfig(Strict):
    timezone: str = "Asia/Kolkata"
    quiet_hours: QuietHours = Field(default_factory=lambda: QuietHours(start=time(22), end=time(8)))
    summary_mail_times: list[time] = Field(default_factory=lambda: [time(8), time(20)])
    heartbeat_interval_seconds: int = Field(60, ge=10, le=3600)

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as e:
            raise ValueError(f"unknown timezone {v!r}") from e
        return v


# --- owner_stub.yaml (temporary until Module 5) ---------------------------

class OwnerRole(Strict):
    title: str
    company: str
    started: YearMonth


class OwnerJob(Strict):
    company: str
    title: str
    start: YearMonth
    end: YearMonth | None = None


class OwnerStub(Strict):
    name: str
    location: str = ""
    career_field: str
    current_role: OwnerRole
    employment_history: list[OwnerJob] = Field(min_length=1)
    target_roles: list[str] = Field(default_factory=list)


# --- seed_companies.yaml --------------------------------------------------

class SeedCompany(Strict):
    name: str
    domain: str
    type: Literal["startup", "established"]
    trusted: bool = False  # exists per spec; unused by default (seeds are judged like everyone else)
    notes: str = ""


class SeedList(Strict):
    companies: list[SeedCompany] = Field(default_factory=list)


# --- scouts.yaml ----------------------------------------------------------

SCOUT_NAMES = ("seeds", "yc", "adzuna", "jooble", "remotive", "hn")


class Schedule(Strict):
    """Cron-style fields, evaluated in app.yaml's timezone."""

    minute: str | int = "0"
    hour: str | int = "6"
    day_of_month: str | int = "*"


class ScoutBase(Strict):
    enabled: bool = True
    schedule: Schedule = Field(default_factory=Schedule)


class SeedsScout(ScoutBase):
    schedule: Schedule = Field(default_factory=lambda: Schedule(hour="5", minute="0"))


class YCScout(ScoutBase):
    schedule: Schedule = Field(default_factory=lambda: Schedule(hour="5", minute="30"))
    min_batch_year: int = 2022
    require_hiring: bool = True
    regions_any: list[str] = Field(default_factory=lambda: ["India", "Remote"])


class AdzunaScout(ScoutBase):
    schedule: Schedule = Field(default_factory=lambda: Schedule(hour="6", minute="0"))
    country: str = "in"
    max_days_old: int = 21
    results_per_page: int = Field(50, le=50)
    pages_per_role: int = Field(2, ge=1, le=10)


class JoobleScout(ScoutBase):
    schedule: Schedule = Field(default_factory=lambda: Schedule(hour="6", minute="15"))
    location: str = "India"


class RemotiveScout(ScoutBase):
    schedule: Schedule = Field(default_factory=lambda: Schedule(hour="6", minute="30"))
    limit: int = 100
    locations_any: list[str] = Field(
        default_factory=lambda: ["worldwide", "anywhere", "india", "asia", "apac"]
    )


class HNScout(ScoutBase):
    # Thread is posted on the 1st weekday of the month; 2nd = main scan, 16th = mid-month rescan.
    schedule: Schedule = Field(default_factory=lambda: Schedule(day_of_month="2,16", hour="7", minute="0"))
    max_comments_per_run: int = 1500


class Resolver(Strict):
    """Finds the website (= identity) of companies seen only by name."""

    interval_minutes: int = 60
    batch_size: int = 25
    max_attempts: int = 4
    brave_monthly_budget: int = 600


class ScoutsConfig(Strict):
    seeds: SeedsScout = Field(default_factory=SeedsScout)
    yc: YCScout = Field(default_factory=YCScout)
    adzuna: AdzunaScout = Field(default_factory=AdzunaScout)
    jooble: JoobleScout = Field(default_factory=JoobleScout)
    remotive: RemotiveScout = Field(default_factory=RemotiveScout)
    hn: HNScout = Field(default_factory=HNScout)
    resolver: Resolver = Field(default_factory=Resolver)
    company_name_skiplist: list[str] = Field(
        default_factory=lambda: ["confidential", "company name withheld", "undisclosed", "stealth", "hiring company"]
    )


# --- company_rules.yaml ----------------------------------------------------

PassOrReject = Literal["pass", "reject"]


class EvaluationRules(Strict):
    max_new_per_day: int = Field(50, ge=0)
    batch_size: int = Field(10, ge=1, le=100)
    interval_minutes: int = Field(30, ge=5)
    source_priority: list[str] = Field(default_factory=lambda: list(SCOUT_NAMES))
    search_monthly_budget: int = 800


class SharedRules(Strict):
    avoid_business_models: list[str] = Field(default_factory=lambda: ["it_outsourcing", "staffing_recruiting"])
    geography_unknown: PassOrReject = "pass"


class StartupRules(Strict):
    funding_window_months: int = 24
    viability_unknown: PassOrReject = "pass"
    category_blocklist: dict[str, list[str]] = Field(default_factory=lambda: {
        "ecommerce": ["e-commerce", "ecommerce", "d2c"],
        "food_delivery": ["food delivery"],
        "quick_commerce": ["quick commerce", "q-commerce"],
        "generic_marketplace": [],
    })
    novelty_borderline: PassOrReject = "reject"
    novelty_guidance: str = "Pass genuinely novel problems; reject undifferentiated clones."


class EstablishedRules(Strict):
    keep: list[Literal["up", "lateral", "down"]] = Field(default_factory=lambda: ["up", "lateral"])
    unknown: PassOrReject = "reject"
    guidance: str = "Judge brand relative to the owner's career field."


class CompanyRules(Strict):
    evaluation: EvaluationRules = Field(default_factory=EvaluationRules)
    shared: SharedRules = Field(default_factory=SharedRules)
    startup: StartupRules = Field(default_factory=StartupRules)
    established: EstablishedRules = Field(default_factory=EstablishedRules)


# --- llm.yaml ---------------------------------------------------------------

class LLMTier(Strict):
    effort: Literal["low", "medium", "high", "xhigh", "max"]
    max_tokens: int = Field(ge=256)


class LLMConfig(Strict):
    fast: LLMTier = Field(default_factory=lambda: LLMTier(effort="low", max_tokens=4000))
    judge: LLMTier = Field(default_factory=lambda: LLMTier(effort="medium", max_tokens=8000))
    server_side_fallback: bool = True
    prices: dict[str, list[float]] = Field(default_factory=dict)
