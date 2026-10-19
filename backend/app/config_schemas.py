"""Pydantic schemas for config/*.yaml. Unknown keys are rejected so typos fail loudly at startup."""

from datetime import date, time
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator, model_validator


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


class Compensation(Strict):
    annual_ctc: int = Field(ge=0)
    currency: str = "INR"
    includes_variable: bool = True
    last_confirmed: YearMonth | None = None


class RoleFamilies(Strict):
    progression: list[str] = Field(default_factory=list)
    lateral: list[str] = Field(default_factory=list)
    excluded: list[str] = Field(default_factory=list)


class Skills(Strict):
    must_have: list[str] = Field(default_factory=list)
    nice_to_have: list[str] = Field(default_factory=list)


class WorkAuthorization(Strict):
    citizenship: str = "India"
    visas_held: list[str] = Field(default_factory=list)
    open_to_relocation: bool = False
    relocation_countries: list[str] = Field(default_factory=list)


class OwnerStub(Strict):
    name: str
    location: str = ""
    # Signature details for outreach emails (Module 3); private like the rest of this file.
    mobile: str | None = None
    linkedin_url: str | None = None
    career_field: str
    current_role: OwnerRole
    employment_history: list[OwnerJob] = Field(min_length=1)
    target_roles: list[str] = Field(default_factory=list)
    # Module 2 inputs (temporary home until Module 5's resume flow).
    current_compensation: Compensation | None = None
    level_ladder: list[str] = Field(default_factory=list)
    current_level: str | None = None
    total_experience_years: float | None = None
    track: Literal["individual_contributor", "people_manager"] = "individual_contributor"
    open_to_management_track: bool = False
    role_families: RoleFamilies = Field(default_factory=RoleFamilies)
    skills: Skills = Field(default_factory=Skills)
    work_authorization: WorkAuthorization = Field(default_factory=WorkAuthorization)
    preferred_locations_india: list[str] = Field(default_factory=list)
    remote_ok: bool = True
    hybrid_ok: bool = True
    notice_period_days: int | None = None

    @model_validator(mode="after")
    def _level_on_ladder(self):
        if self.current_level and self.level_ladder and self.current_level not in self.level_ladder:
            raise ValueError(f"current_level {self.current_level!r} is not one of level_ladder {self.level_ladder}")
        return self

    def placeholder_fields(self) -> list[str]:
        """Fields still holding owner_stub.example.yaml's sample values."""
        samples = {"your name", "current company", "previous company"}
        found = []
        if self.name.strip().lower() in samples:
            found.append("name")
        if self.current_role.company.strip().lower() in samples:
            found.append("current_role.company")
        for i, job in enumerate(self.employment_history):
            if job.company.strip().lower() in samples:
                found.append(f"employment_history[{i}].company")
        return found


# --- seed_companies.yaml --------------------------------------------------

class SeedCompany(Strict):
    name: str
    domain: str
    type: Literal["startup", "established"]
    trusted: bool = False  # exists per spec; unused by default (seeds are judged like everyone else)
    notes: str = ""


class SeedList(Strict):
    companies: list[SeedCompany] = Field(default_factory=list)


class SeedPack(Strict):
    """A curated, optional company list shipped in config/seed_packs/ (an open-source contribution surface)."""

    name: str
    description: str = ""
    companies: list[SeedCompany] = Field(default_factory=list)


# --- scouts.yaml ----------------------------------------------------------

SCOUT_NAMES = ("seeds", "packs", "peers", "yc", "waas", "adzuna", "jooble", "remotive", "hn")


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


class PacksScout(ScoutBase):
    schedule: Schedule = Field(default_factory=lambda: Schedule(hour="5", minute="10"))
    enabled_packs: list[str] = Field(default_factory=lambda: ["gcc_india", "india_large", "india_unicorns"])


class PeersScout(ScoutBase):
    """'Companies like the ones you keep' (owner-approved spec deviation, 2026-10-11)."""

    schedule: Schedule = Field(default_factory=lambda: Schedule(hour="7", minute="30"))
    companies_per_run: int = Field(10, ge=1, le=50)
    suggestions_per_company: int = Field(5, ge=1, le=10)


class YCScout(ScoutBase):
    schedule: Schedule = Field(default_factory=lambda: Schedule(hour="5", minute="30"))
    min_batch_year: int = 2022
    require_hiring: bool = True
    regions_any: list[str] = Field(default_factory=lambda: ["India", "Remote"])


class WaaSScout(ScoutBase):
    """YC Work at a Startup, public category pages only (owner-approved deviation, 2026-10-12)."""

    schedule: Schedule = Field(default_factory=lambda: Schedule(hour="6", minute="45"))
    categories: list[str] = Field(default_factory=lambda: ["product-manager", "operations", "finance", "science"])


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
    packs: PacksScout = Field(default_factory=PacksScout)
    peers: PeersScout = Field(default_factory=PeersScout)
    yc: YCScout = Field(default_factory=YCScout)
    waas: WaaSScout = Field(default_factory=WaaSScout)
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
    # Proven scale-ups (owner decision 2026-10-11) are judged like established companies:
    # on brand/growth vs. the owner's career, not on novelty or the category blocklist.
    scaleups_as_established: bool = True
    scaleup_sizes: list[str] = Field(default_factory=lambda: ["1001-10000", "10000+"])


class EstablishedRules(Strict):
    keep: list[Literal["up", "lateral", "down"]] = Field(default_factory=lambda: ["up", "lateral"])
    unknown: PassOrReject = "reject"
    guidance: str = "Judge brand relative to the owner's career field."


class CompanyRules(Strict):
    evaluation: EvaluationRules = Field(default_factory=EvaluationRules)
    shared: SharedRules = Field(default_factory=SharedRules)
    startup: StartupRules = Field(default_factory=StartupRules)
    established: EstablishedRules = Field(default_factory=EstablishedRules)


# --- watch.yaml ---------------------------------------------------------------

class WatchConfig(Strict):
    activation_batch: int = 10
    sync_batch: int = 30
    tick_minutes: int = Field(15, ge=5)
    active_sync_hours: int = 6
    dormant_sync_days: int = 7
    dormant_after_quiet_days: int = 30
    reactivate_after_failures: int = 5
    reverify_days: int = 90
    stale_aggregator_jobs: int = 2
    demote_after_silent_days: int = 90
    archive_after_silent_days: int = 180
    refresh_startup_days: int = 30
    refresh_established_days: int = 90
    refresh_rejected_days: int = 180
    refresh_per_run: int = 40
    workday_pages_per_role: int = Field(2, ge=1, le=10)


# --- adapters/*.yaml ----------------------------------------------------------

class AdapterRequest(Strict):
    method: Literal["GET", "POST"] = "GET"
    url: str
    params: dict[str, str | int] = Field(default_factory=dict)
    json_body: dict | None = None


class AdapterPagination(Strict):
    param: str  # query/body field carrying the offset
    size: int = 50
    max_pages: int = Field(5, ge=1, le=20)


class Adapter(Strict):
    """A JSON careers endpoint for one large company. Field values are dot-paths into each item."""

    name: str
    domain: str
    request: AdapterRequest
    items: str  # dot-path to the list of jobs in the response, e.g. "data.jobs"
    fields: dict[str, str]  # external_id, title, location, url, posted_at, description, department
    url_template: str | None = None  # e.g. "https://careers.example.com/job/{external_id}"
    pagination: AdapterPagination | None = None
    maintainer: str = ""


# --- job_rules.yaml (Module 2) -------------------------------------------------

class Kind1Rules(Strict):
    """Hard filters applied before any AI cost."""

    max_age_days: int = 21
    allowed_employment_types: list[str] = Field(default_factory=lambda: ["full_time"])
    # Obvious non-matches, matched on word stems in the title (owner's excluded families are added on top).
    noise_titles: list[str] = Field(default_factory=list)
    drop_titles_two_rungs_up: bool = True
    senior_title_words: list[str] = Field(
        default_factory=lambda: ["director", "vice president", "vp", "svp", "evp", "head of", "chief"])
    keep_title_phrases: list[str] = Field(
        default_factory=lambda: ["associate director", "assistant director", "deputy director", "chief of staff"])


class DescriptionFetch(Strict):
    per_run: int = Field(80, ge=1, le=1000)


class Kind2Rules(Strict):
    max_reads_per_day: int = Field(150, ge=0)
    reads_per_tick: int = Field(20, ge=1, le=200)
    services_penalty: int = 10
    prestige_brands: list[str] = Field(default_factory=lambda: [
        "mckinsey.com", "bcg.com", "bain.com", "goldmansachs.com", "morganstanley.com", "jpmorganchase.com",
        "google.com", "microsoft.com", "amazon.com", "apple.com", "meta.com",
    ])


class PayRules(Strict):
    marginal_band: float = Field(0.15, ge=0, le=1)


DIMENSIONS = ("skills_fit", "experience_fit", "domain_fit", "career_growth", "brand_value", "location_fit")


class Bands(Strict):
    fast_lane: int = 85
    inbox: int = 65
    maybe: int = 40


class ScoringRules(Strict):
    grades_per_day: int = Field(20, ge=0)
    grades_per_tick: int = Field(5, ge=1, le=100)
    weights: dict[str, float] = Field(default_factory=lambda: {d: 1.0 for d in DIMENSIONS})
    bands: Bands = Field(default_factory=Bands)
    lateral_tenure_years: float = 2
    lateral_penalty: int = 5
    ungraded_max_score: int = Field(64, ge=0, le=100)

    @field_validator("weights")
    @classmethod
    def _known_dimensions(cls, v: dict[str, float]) -> dict[str, float]:
        unknown = set(v) - set(DIMENSIONS)
        if unknown:
            raise ValueError(f"unknown scoring dimensions {sorted(unknown)}; use {list(DIMENSIONS)}")
        if sum(v.values()) <= 0:
            raise ValueError("weights must add up to more than 0")
        return v


class JobRules(Strict):
    kind1: Kind1Rules = Field(default_factory=Kind1Rules)
    descriptions: DescriptionFetch = Field(default_factory=DescriptionFetch)
    kind2: Kind2Rules = Field(default_factory=Kind2Rules)
    pay: PayRules = Field(default_factory=PayRules)
    scoring: ScoringRules = Field(default_factory=ScoringRules)


# --- outreach.yaml (Module 3) --------------------------------------------------

SLOT_KINDS = ("founder", "recruiting", "hiring_manager")


class BenchShape(Strict):
    """How many people of each kind one bench holds (spec §3.1 caps)."""
    founder: int = Field(0, ge=0, le=10)
    recruiting: int = Field(0, ge=0, le=10)
    hiring_manager: int = Field(0, ge=0, le=10)

    @property
    def cap(self) -> int:
        return self.founder + self.recruiting + self.hiring_manager


class Benches(Strict):
    startup: BenchShape = Field(default_factory=lambda: BenchShape(founder=2, recruiting=3))
    mid_size: BenchShape = Field(default_factory=lambda: BenchShape(recruiting=3, hiring_manager=2))
    mnc: BenchShape = Field(default_factory=lambda: BenchShape(recruiting=7))


class CompanyClasses(Strict):
    """Company size buckets (fact `employee_estimate`) → which bench shape applies."""
    startup_sizes: list[str] = Field(default_factory=lambda: ["1-10", "11-50", "51-200"])
    mid_sizes: list[str] = Field(default_factory=lambda: ["201-1000"])
    # Unknown size: startups use the startup shape, established companies the MNC shape.


class EnrichmentBudget(Strict):
    """One pooled credit budget (spec §0); the ladder spends providers in this order."""
    hunter_finder_per_month: int = Field(20, ge=0)
    hunter_domain_search_per_month: int = Field(5, ge=0)
    apollo_per_month: int = Field(50, ge=0)
    enrich_per_bench: int = Field(2, ge=0, le=10)  # rest of the bench is built from the learned pattern


class VerificationBudget(Strict):
    smtp_handshake: bool = True
    smtp_timeout_seconds: float = Field(8, gt=0, le=60)
    smtp_blocked_retry_hours: int = Field(24, ge=1)  # port 25 blocked (most home ISPs) → don't retry for a day
    myemailverifier_per_day: int = Field(100, ge=0)
    hunter_verifier_per_month: int = Field(40, ge=0)
    max_candidates_per_person: int = Field(4, ge=1, le=12)  # construction: how many formats to test


class ContactRules(Strict):
    # Which roles get a contact search: these bands automatically, plus roles you marked `act`.
    auto_bands: list[Literal["fast_lane", "inbox", "maybe"]] = Field(default_factory=lambda: ["fast_lane"])
    searches_per_month: int = Field(80, ge=0)       # Brave searches for people (spec ~50-80/mo)
    searches_per_tick: int = Field(4, ge=0)
    results_per_search: int = Field(20, ge=1, le=20)
    benches: Benches = Field(default_factory=Benches)
    classes: CompanyClasses = Field(default_factory=CompanyClasses)
    # MNC benches are per function + geography; the role family is mapped onto one of these functions.
    functions: dict[str, list[str]] = Field(default_factory=lambda: {
        "analytics": ["analytics", "insight", "data", "business intelligence"],
        "strategy": ["strategy", "bizops", "business operations", "chief of staff", "consulting"],
        "product": ["product"],
    })
    default_function: str = "analytics"
    retry_no_contact_days: int = Field(14, ge=1)
    reverify_after_days: int = Field(180, ge=1)     # on-use re-check of person + mailbox
    sweep_hot_days: int = Field(30, ge=1)
    sweep_warm_days: int = Field(90, ge=1)
    sweep_searches_per_week: int = Field(10, ge=0)
    enrichment: EnrichmentBudget = Field(default_factory=EnrichmentBudget)
    verification: VerificationBudget = Field(default_factory=VerificationBudget)
    # Address formats tried, best first, when no real address at the domain shows its pattern.
    common_formats: list[str] = Field(default_factory=lambda: [
        "{first}.{last}", "{first}", "{f}{last}", "{first}{last}", "{first}_{last}", "{f}.{last}", "{last}.{first}",
    ])
    startup_formats: list[str] = Field(default_factory=lambda: ["{first}", "{first}.{last}", "{f}{last}", "{first}{last}"])
    # Companies whose staff mail lives on another domain (website domain → mail domain).
    mail_domains: dict[str, str] = Field(default_factory=dict)

    @field_validator("default_function")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("default_function can't be empty")
        return v


class DraftRules(Strict):
    drafts_per_day: int = Field(15, ge=0)        # new or rewritten drafts (Sonnet, ~2-3 cents each)
    drafts_per_tick: int = Field(3, ge=0)
    template_file: str = "email_template.md"     # in the config folder
    max_rewrites: int = Field(1, ge=0, le=3)     # truth check: rewrite rounds before unsupported claims are cut


class SendingRules(Strict):
    """Throttle stack (spec 3.3), applied in this order; none of it can be bypassed."""
    global_per_day: int = Field(12, ge=0, le=50)
    per_company_per_day: int = Field(2, ge=0, le=10)
    stagger_minutes: tuple[int, int] = (30, 60)
    wave_sizes: tuple[int, int] = (2, 2)          # day 0: primary + one more; day 1: next two; day 2: the rest
    # Bounce breaker: trips at this many bounces, or above this rate once enough has been sent.
    breaker_bounces: int = Field(2, ge=1)
    breaker_rate: float = Field(0.05, gt=0, lt=1)
    breaker_min_sends: int = Field(30, ge=1)
    breaker_cooldown_hours: int = Field(6, ge=1)
    delivered_after_hours: int = Field(48, ge=1)  # sent + no bounce this long = delivered (proves the address)
    edit_examples: int = Field(5, ge=0, le=20)    # your recent edits shown to the AI as style examples


class ReplyRules(Strict):
    min_confidence: float = Field(0.75, ge=0, le=1)   # below this a reply counts as "unsure": shown to you raw
    silent_after_days: int = Field(7, ge=1)          # waves done + no human reply this long = silent


class OutreachConfig(Strict):
    contacts: ContactRules = Field(default_factory=ContactRules)
    drafts: DraftRules = Field(default_factory=DraftRules)
    sending: SendingRules = Field(default_factory=SendingRules)
    replies: ReplyRules = Field(default_factory=ReplyRules)


# --- llm.yaml ---------------------------------------------------------------

class LLMTier(Strict):
    effort: Literal["low", "medium", "high", "xhigh", "max"]
    max_tokens: int = Field(ge=256)


class LLMConfig(Strict):
    fast: LLMTier = Field(default_factory=lambda: LLMTier(effort="low", max_tokens=4000))
    judge: LLMTier = Field(default_factory=lambda: LLMTier(effort="medium", max_tokens=8000))
    server_side_fallback: bool = True
    prices: dict[str, list[float]] = Field(default_factory=dict)
