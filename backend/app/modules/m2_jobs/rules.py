"""Rules applied to the AI read (spec §2.1). Plain code: predictable, testable, explainable.

Salary and seniority are independent gates: money never buys back a failed ladder check.
"""

from dataclasses import dataclass, field

from app.config_schemas import JobRules, OwnerStub
from app.modules.m2_jobs.normalize import parse_salary
from app.modules.m2_jobs.rates import PERIOD_TO_YEAR, to_inr


@dataclass
class Assessment:
    reasons: list[str] = field(default_factory=list)    # why it was rejected (empty = passed)
    flags: list[str] = field(default_factory=list)      # shown in the digest, no score impact
    penalties: list[dict] = field(default_factory=list)  # applied in scoring (Part 2c)
    comp: dict = field(default_factory=dict)
    level: dict = field(default_factory=dict)

    @property
    def outcome(self) -> str:
        return "rejected" if self.reasons else "passed"


@dataclass
class Context:
    """Facts about the company the rules need (from Module 1)."""

    company_domain: str | None
    brand_step_up: bool          # Module 1's brand verdict for this company was "up"
    job_is_india: bool
    job_is_remote: bool
    stated_salary: dict | None   # parsed from the listing itself, if any


def _level(read: dict, owner: OwnerStub, a: Assessment) -> int | None:
    ladder = owner.level_ladder
    job_level = read.get("level", "unclear")
    if not ladder or not owner.current_level or job_level not in ladder:
        a.flags.append("level: unclear")
        a.level = {"job": job_level, "current": owner.current_level, "gap": None}
        return None
    gap = ladder.index(job_level) - ladder.index(owner.current_level)
    a.level = {"job": job_level, "current": owner.current_level, "gap": gap}
    if gap < 0:
        a.reasons.append(f"level: {job_level} is below your level ({owner.current_level})")
    elif gap >= 2:
        a.reasons.append(f"level: {job_level} is {gap} rungs above {owner.current_level}")
    return gap


def _family_and_track(read: dict, owner: OwnerStub, ctx: Context, gap: int | None, a: Assessment) -> None:
    match = read.get("family_match")
    family = read.get("role_family") or "?"
    if match == "excluded":
        a.reasons.append(f"family: {family} is one of your excluded families")
    elif match == "other":
        a.reasons.append(f"family: {family} is outside your role families")
    elif match == "lateral":
        # Laterals require a step-up brand AND senior level within the family.
        if not ctx.brand_step_up:
            a.reasons.append(f"lateral ({family}) at a company that isn't a step up for you")
        elif gap is not None and gap < 0:
            a.reasons.append(f"lateral ({family}) below your level")
        else:
            a.flags.append(f"lateral move: {family}")
    if (read.get("people_management") == "manages_people" and owner.track == "individual_contributor"
            and not owner.open_to_management_track):
        a.reasons.append("people-manager role (you're an individual contributor, not open to the management track)")


def _dealbreakers(read: dict, a: Assessment) -> None:
    geo = read.get("doable_from_india")
    if geo == "no":
        evidence = read.get("geography_evidence") or ""
        a.reasons.append("not doable from India without a visa you'd arrange"
                         + (f" (“{evidence[:80]}”)" if evidence and evidence != "none" else ""))
    elif geo == "unclear":
        a.flags.append("geo: confirm")
    if read.get("six_day_week") == "yes":
        a.reasons.append("6-day work week")
    etype = read.get("employment_type")
    if etype in ("part_time", "contract", "internship", "temporary"):
        a.reasons.append(f"{etype.replace('_', '-')} role")


def _flags(read: dict, a: Assessment) -> None:
    f = read.get("flags") or {}
    shift = f.get("shift")
    if shift == "rigid_night_shift":
        a.flags.append("hours: US-aligned shift")
    elif shift in ("us_overlap_flexible", "unclear") and read.get("geography_basis") in ("remote_anywhere",
                                                                                      "visa_sponsorship"):
        a.flags.append("hours: confirm")
    if f.get("travel") == "heavy":
        a.flags.append("travel: heavy")
    if f.get("relocation_required") == "yes":
        a.flags.append("relocation: onsite")
    if f.get("on_call") == "yes":
        a.flags.append("on-call")


def _services(read: dict, rules: JobRules, ctx: Context, a: Assessment) -> None:
    if not read.get("services_flavored"):
        return
    prestige = (ctx.company_domain or "") in rules.kind2.prestige_service_domains or ctx.brand_step_up
    if not prestige:
        a.penalties.append({"name": "services-flavoured work at a mid-tier brand",
                            "points": -rules.kind2.services_penalty})


def _comp(read: dict, owner: OwnerStub, rules: JobRules, ctx: Context, table: dict, a: Assessment) -> None:
    current = owner.current_compensation
    if current is None or current.annual_ctc <= 0:
        a.flags.append("comp: unverified")
        a.comp = {"verdict": "unverified", "why": "current pay not set in owner_stub.yaml"}
        return
    # Where the money is spent decides the conversion: abroad onsite → PPP; remote paid from abroad → nominal.
    abroad = not ctx.job_is_india
    basis = "nominal" if (not abroad or ctx.job_is_remote) else "ppp"

    stated = ctx.stated_salary or parse_salary(read.get("stated_pay") if read.get("stated_pay") != "none" else None)
    if stated:
        source, low, high, currency, period = "stated", stated["min"], stated["max"], stated["currency"], stated["period"]
        confident = True
    elif read.get("pay_estimate_min", -1) > 0 and read.get("pay_estimate_max", -1) > 0:
        source, low, high = "estimate", read["pay_estimate_min"], read["pay_estimate_max"]
        currency, period = (read.get("pay_estimate_currency") or "INR").upper(), "year"
        confident = read.get("pay_estimate_confidence") in ("high", "medium")
    else:
        a.flags.append("comp: unverified")
        a.comp = {"verdict": "unverified", "why": "no stated pay and no estimate"}
        return

    mult = PERIOD_TO_YEAR.get(period, 1)
    mid_local = (low + high) / 2 * mult
    mid_inr = to_inr(mid_local, currency, basis=basis, table=table)
    if mid_inr is None:
        a.flags.append("comp: unverified")
        a.comp = {"verdict": "unverified", "why": f"no conversion factors for {currency}"}
        return
    ratio = mid_inr / current.annual_ctc
    band = rules.pay.marginal_band
    a.comp = {"source": source, "currency": currency, "range_local": [low * mult, high * mult],
              "mid_inr": round(mid_inr), "basis": basis, "ratio": round(ratio, 2),
              "current_inr": current.annual_ctc}

    if source == "stated":
        if ratio < 1 - band:
            a.comp["verdict"] = "below"
            a.reasons.append(f"pay: about {ratio:.0%} of your current pay ({basis})")
        elif ratio < 1 + band:
            a.comp["verdict"] = "marginal"
            a.flags.append("comp: marginal")
        else:
            a.comp["verdict"] = "above"
    else:  # an AI estimate only rejects when it's confident AND clearly below
        if ratio < 1 - band and confident and read.get("pay_estimate_confidence") == "high":
            a.comp["verdict"] = "below"
            a.reasons.append(f"pay (estimated): about {ratio:.0%} of your current pay ({basis})")
        elif ratio >= 1 + band and confident:
            a.comp["verdict"] = "above"
        else:
            a.comp["verdict"] = "unverified"
            a.flags.append("comp: unverified")


def assess(read: dict, owner: OwnerStub, rules: JobRules, ctx: Context, table: dict) -> Assessment:
    a = Assessment()
    gap = _level(read, owner, a)
    _family_and_track(read, owner, ctx, gap, a)
    _dealbreakers(read, a)
    _comp(read, owner, rules, ctx, table, a)  # independent of the level gate
    _services(read, rules, ctx, a)
    _flags(read, a)
    return a
