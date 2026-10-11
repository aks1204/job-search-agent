"""Deterministic pieces of Part 1b: schema shaping, fact derivation, rule checks. No DB, no AI."""

import pytest

from app.config_schemas import CompanyRules
from app.core.llm import strict_schema
from app.modules.m1_companies import checks as C
from app.modules.m1_companies.enrich import CompanyProfile, html_to_text
from app.modules.m1_companies.evaluator import BrandJudgment
from app.modules.m1_companies.facts import feeder_facts

RULES = CompanyRules()


def card(**facts) -> dict:
    return {k: {"value": v, "confidence": "inferred", "source": "test"} for k, v in facts.items()}


def test_strict_schema_requires_everything_and_forbids_extras():
    s = strict_schema(CompanyProfile)
    assert s["additionalProperties"] is False
    assert set(s["required"]) == set(s["properties"])
    assert "title" not in s
    assert s["properties"]["business_model"]["enum"][0] == "product"
    assert strict_schema(BrandJudgment)["properties"]["direction"]["enum"] == ["up", "lateral", "down", "unknown"]


def test_feeder_facts_from_yc_and_aggregators():
    facts = {(k, v, conf) for k, v, conf, _ in feeder_facts({
        "yc": {"isHiring": True, "team_size": 35, "regions": ["India", "Remote"], "industries": ["Fintech"],
               "one_liner": "Payroll for India"},
        "adzuna": {"title": "Analyst"},
    })}
    assert ("actively_hiring", "yes", "verified") in facts
    assert ("employee_estimate", "11-50", "verified") in facts
    assert ("india_presence", "yes", "verified") in facts
    assert ("hires_remote_from_india", "yes", "inferred") in facts
    assert ("what_they_build", "Payroll for India", "verified") in facts


def test_feeder_facts_hn_remote_needs_india_or_global():
    us_only = {k for k, *_ in feeder_facts({"hn": {"headline": "Acme | Analyst | Remote (US only)"}})}
    assert "hires_remote_from_india" not in us_only
    global_ = {k for k, *_ in feeder_facts({"hn": {"headline": "Acme | Analyst | REMOTE (worldwide)"}})}
    assert "hires_remote_from_india" in global_


def test_avoid_list():
    assert C.avoid_list(card(business_model="it_outsourcing"), RULES).failed
    assert not C.avoid_list(card(business_model="consulting"), RULES).failed  # strategy consulting allowed
    assert not C.avoid_list(card(), RULES).failed


@pytest.mark.parametrize("facts, outcome, on_unknown", [
    ({"india_presence": "yes"}, "pass", False),
    ({"india_presence": "no", "hires_remote_from_india": "no", "sponsors_visa": "no"}, "fail", False),
    ({"india_presence": "no"}, "pass", True),  # rest unknown → lenient
])
def test_geography(facts, outcome, on_unknown):
    c = C.geography(card(**facts), RULES)
    assert (c.outcome, c.on_unknown) == (outcome, on_unknown)


def test_blocklist_by_tag_is_permanent_but_b2b_tools_are_exempt():
    c = C.category_blocklist(card(), RULES, feeder_tags=["Consumer", "E-commerce"])
    assert c.failed and c.permanent
    assert not C.category_blocklist(card(), RULES, feeder_tags=["B2B", "E-commerce"]).failed
    assert C.category_blocklist(card(blocklisted_category="food_delivery"), RULES, feeder_tags=[]).failed
    assert not C.category_blocklist(card(blocklisted_category="none"), RULES, feeder_tags=[]).failed


@pytest.mark.parametrize("facts, outcome", [
    ({"actively_hiring": "yes"}, "pass"),
    ({"raised_within_window": "yes"}, "pass"),
    ({"actively_hiring": "yes", "distress": "yes"}, "fail"),
    ({"actively_hiring": "no", "raised_within_window": "no"}, "fail"),
    ({}, "pass"),  # unknown → pass (startup viability policy)
])
def test_viability(facts, outcome):
    assert C.viability(card(**facts), RULES).outcome == outcome


def test_needs_news_only_when_it_could_matter():
    assert C.needs_news(card())
    assert not C.needs_news(card(actively_hiring="yes"))


def test_html_to_text_keeps_title_and_drops_scripts():
    raw = "<html><head><title>Acme &amp; Co</title><script>var x=1</script></head><body><h1>We build rockets</h1></body></html>"
    text = html_to_text(raw)
    assert text.startswith("Acme & Co")
    assert "rockets" in text and "var x" not in text
