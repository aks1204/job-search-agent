"""Part 2b, no database: the free seniority/location rules, the AI-read schema, the Kind-2/level/pay rules."""

from types import SimpleNamespace

import pytest

from app.config_schemas import JobRules, OwnerStub
from app.core.llm import strict_schema
from app.modules.m2_jobs.filters import TitleNet
from app.modules.m2_jobs.rates import BUILTIN, to_inr
from app.modules.m2_jobs.reader import read_model
from app.modules.m2_jobs.rules import Context, assess

LADDER = ["Analyst", "Senior Analyst", "Associate / Lead", "Manager", "Senior Manager", "Director"]


def owner(**overrides) -> OwnerStub:
    base = {
        "name": "T", "career_field": "analytics",
        "current_role": {"title": "Senior Associate", "company": "Co", "started": "2024-03"},
        "employment_history": [{"company": "Co", "title": "Senior Associate", "start": "2024-03"}],
        "target_roles": ["Senior Data Analyst", "Strategy Associate"],
        "level_ladder": LADDER, "current_level": "Associate / Lead",
        "current_compensation": {"annual_ctc": 2400000},
        "role_families": {"progression": ["analytics manager"], "lateral": ["product analyst"], "excluded": ["sales"]},
    }
    return OwnerStub.model_validate({**base, **overrides})


RULES = JobRules()
TABLE = {cur: (*vals, "builtin") for cur, vals in BUILTIN.items()}


def read(**overrides) -> dict:
    base = {
        "doable_from_india": "yes", "geography_basis": "india_location", "geography_evidence": "Bengaluru office",
        "six_day_week": "no", "role_family": "data analytics", "family_match": "progression",
        "level": "Associate / Lead", "years_experience_min": 5, "people_management": "individual_contributor",
        "employment_type": "full_time", "services_flavored": False, "stated_pay": "none",
        "pay_estimate_currency": "INR", "pay_estimate_min": -1, "pay_estimate_max": -1,
        "pay_estimate_confidence": "low",
        "flags": {"travel": "none", "relocation_required": "no", "on_call": "no", "shift": "standard"},
        "one_liner": "Analytics for commercial teams",
    }
    return {**base, **overrides}


def ctx(**overrides) -> Context:
    base = dict(company_domain="acme.com", brand_level="unknown", job_is_india=True, job_is_remote=False,
                stated_salary=None)
    return Context(**{**base, **overrides})


# --- free rules -------------------------------------------------------------------------

def test_title_seniority_rule_and_exceptions():
    net = TitleNet.build(owner(), JobRules())
    assert net.check("Senior Director, Data Strategy").startswith("title seniority")
    assert net.check("VP, Analytics").startswith("title seniority")
    assert net.check("Associate Director, Analytics") is None      # ambiguous → AI read
    assert net.check("Chief of Staff, Strategy") is None            # kept for the AI read
    assert net.check("Senior Data Analyst") is None


def test_location_rule_only_when_not_open_to_relocation():
    stay = TitleNet.build(owner(work_authorization={"open_to_relocation": False}), JobRules())
    move = TitleNet.build(owner(work_authorization={"open_to_relocation": True}), JobRules())
    abroad = SimpleNamespace(is_india=False, is_remote=False, location="Ireland, Cork")
    unknown = SimpleNamespace(is_india=False, is_remote=False, location="2 Locations")
    assert stay.check_location(abroad) and move.check_location(abroad) is None
    assert stay.check_location(unknown) is None


# --- AI read schema ----------------------------------------------------------------------

def test_read_schema_uses_the_owners_ladder_and_is_strict():
    schema = strict_schema(read_model(tuple(LADDER)))
    assert schema["properties"]["level"]["enum"] == [*LADDER, "unclear"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["flags"]["additionalProperties"] is False  # nested model inlined + strict


# --- rules on the read ----------------------------------------------------------------------

def test_clean_progression_role_passes():
    a = assess(read(), owner(), RULES, ctx(), TABLE)
    assert a.outcome == "passed" and a.level["gap"] == 0 and "comp: unverified" in a.flags


@pytest.mark.parametrize("level, passed", [("Senior Analyst", False), ("Associate / Lead", True),
                                           ("Manager", True), ("Senior Manager", False), ("unclear", True)])
def test_level_ladder_current_or_one_up(level, passed):
    assert (assess(read(level=level), owner(), RULES, ctx(), TABLE).outcome == "passed") is passed


def test_ic_to_management_jump_needs_permission():
    r = read(level="Manager", people_management="manages_people")
    assert assess(r, owner(), RULES, ctx(), TABLE).outcome == "rejected"
    assert assess(r, owner(open_to_management_track=True), RULES, ctx(), TABLE).outcome == "passed"


def test_lateral_needs_step_up_brand():
    r = read(family_match="lateral", role_family="product analytics")
    assert assess(r, owner(), RULES, ctx(brand_level="unknown"), TABLE).outcome == "rejected"
    ok = assess(r, owner(), RULES, ctx(brand_level="up"), TABLE)
    assert ok.outcome == "passed" and "lateral move: product analytics" in ok.flags


def test_dealbreakers():
    us_only = read(doable_from_india="no", geography_evidence="Must be authorized to work in the US")
    assert "authorized to work in the US" in assess(us_only, owner(), RULES, ctx(), TABLE).reasons[0]
    assert assess(read(six_day_week="yes"), owner(), RULES, ctx(), TABLE).reasons == ["6-day work week"]
    assert assess(read(employment_type="contract"), owner(), RULES, ctx(), TABLE).outcome == "rejected"
    assert "geo: confirm" in assess(read(doable_from_india="unclear"), owner(), RULES, ctx(), TABLE).flags


def test_stated_pay_in_inr():
    def verdict(text):
        return assess(read(stated_pay=text), owner(), RULES, ctx(), TABLE)

    assert verdict("₹35L - ₹45L").comp["verdict"] == "above"
    assert verdict("₹22L - ₹26L").comp["verdict"] == "marginal" and "comp: marginal" in verdict("₹22L - ₹26L").flags
    low = verdict("₹10L - ₹12L")
    assert low.outcome == "rejected" and low.reasons[0].startswith("pay:")


def test_salary_and_seniority_are_independent_gates():
    a = assess(read(level="Senior Manager", stated_pay="₹80L - ₹90L"), owner(), RULES, ctx(), TABLE)
    assert a.comp["verdict"] == "above" and a.outcome == "rejected"  # money doesn't buy back the ladder


def test_relocation_uses_ppp_but_remote_from_abroad_uses_nominal():
    usd = {"min": 100000, "max": 100000, "currency": "USD", "period": "year", "raw": "$100K"}
    onsite = assess(read(), owner(), RULES, ctx(job_is_india=False, stated_salary=usd), TABLE)
    remote = assess(read(), owner(), RULES, ctx(job_is_india=False, job_is_remote=True, stated_salary=usd), TABLE)
    assert onsite.comp["basis"] == "ppp" and remote.comp["basis"] == "nominal"
    assert onsite.comp["mid_inr"] < remote.comp["mid_inr"]  # $100K buys less in the US than it converts to


def test_estimates_only_reject_when_confident_and_clearly_below():
    low_conf = read(pay_estimate_min=800000, pay_estimate_max=1000000, pay_estimate_confidence="medium")
    high_conf = read(pay_estimate_min=800000, pay_estimate_max=1000000, pay_estimate_confidence="high")
    assert assess(low_conf, owner(), RULES, ctx(), TABLE).outcome == "passed"
    assert assess(high_conf, owner(), RULES, ctx(), TABLE).outcome == "rejected"


def test_services_penalty_spares_prestige_and_step_up_brands():
    r = read(services_flavored=True)
    assert assess(r, owner(), RULES, ctx(), TABLE).penalties[0]["points"] == -10
    assert assess(r, owner(), RULES, ctx(company_domain="bcg.com"), TABLE).penalties == []
    assert assess(r, owner(), RULES, ctx(brand_level="up"), TABLE).penalties == []
    assert assess(r, owner(), RULES, ctx(brand_level="lateral"), TABLE).penalties == []    # comparable brand
    assert assess(r, owner(), RULES, ctx(company_domain="morganstanley.com"), TABLE).penalties == []  # prestige
    assert assess(r, owner(), RULES, ctx(brand_level="down"), TABLE).penalties[0]["points"] == -10


def test_flags_have_no_score_impact():
    r = read(flags={"travel": "heavy", "relocation_required": "yes", "on_call": "yes", "shift": "rigid_night_shift"})
    a = assess(r, owner(), RULES, ctx(), TABLE)
    assert a.outcome == "passed"
    assert {"hours: US-aligned shift", "travel: heavy", "relocation: onsite", "on-call"} <= set(a.flags)


def test_to_inr():
    assert to_inr(1000, "INR", basis="ppp", table=TABLE) == 1000
    assert round(to_inr(1, "USD", basis="nominal", table=TABLE)) == round(BUILTIN["INR"][1])
    assert round(to_inr(1, "USD", basis="ppp", table=TABLE), 1) == BUILTIN["INR"][0]
