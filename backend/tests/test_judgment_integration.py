"""The evaluator end-to-end against the real database, with a scripted fake AI and mocked websites."""

import pytest
import respx
from sqlalchemy import select

from app.config_loader import get_config
from app.core.http import PoliteClient
from app.core.llm import LLMResult
from app.models import AuditLog, Company, MemoryEntry
from app.modules.m1_companies.candidate import Candidate
from app.modules.m1_companies.enrich import CompanyProfile
from app.modules.m1_companies.evaluator import BrandJudgment, NoveltyJudgment, evaluate
from app.modules.m1_companies.identity import intake
from app.modules.m1_companies.override import override
from app.modules.m1_companies.revival import check_rules_changes, on_new_sighting

pytestmark = pytest.mark.integration


class FakeLLM:
    """Returns the scripted object for each schema and records every call."""

    def __init__(self, **by_schema):
        self.by_schema = by_schema
        self.calls: list[tuple[str, str]] = []

    def structured(self, tier, *, system, user, schema, purpose, session=None, subject=None):
        self.calls.append((tier, purpose))
        return LLMResult(parsed=self.by_schema[schema.__name__], model="fake", cost_usd=0.01,
                         input_tokens=100, output_tokens=50)


def profile(**overrides) -> CompanyProfile:
    base = dict(
        what_they_build="Forecasting software for grid operators", categories=["energy", "b2b saas"],
        business_model="product", blocklisted_category="none", company_stage="startup",
        employee_estimate="11-50", hq_location="Bengaluru, India", india_presence="yes",
        hires_remote_from_india="unknown", sponsors_visa="unknown", actively_hiring="yes",
    )
    return CompanyProfile(**{**base, **overrides})


def novelty(verdict="novel") -> NoveltyJudgment:
    return NoveltyJudgment(verdict=verdict, reasoning="Because.", one_liner="Grid forecasting")


@pytest.fixture
def http():
    with PoliteClient(default_interval=0, use_redis=False) as client, respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=r"https://(www\.)?[^/]+/robots\.txt").respond(404)
        mock.get(url__regex=r"https://(www\.)?[^/]+/?$").respond(
            200, headers={"content-type": "text/html"},
            html="<title>Gridly</title><body>" + "We forecast electricity demand for grid operators. " * 10 + "</body>",
        )
        yield client


def _new(session, name, domain, scout="hn", data=None, type_hint=None) -> Company:
    intake(session, Candidate(scout=scout, external_id=f"t-{domain}", name=name, domain=domain,
                              data=data or {"headline": f"{name} | Analyst | Bengaluru"}, type_hint=type_hint))
    return session.scalars(select(Company).where(Company.domain == domain)).one()


def test_novel_startup_passes_with_two_ai_calls_and_audit(db_session, http):
    company = _new(db_session, "Gridly", "gridly-test.io")
    llm = FakeLLM(CompanyProfile=profile(), NoveltyJudgment=novelty("novel"))

    v = evaluate(db_session, company, llm=llm, http=http)

    assert v.passed and company.status == "passed" and company.type == "startup"
    assert [p for _, p in llm.calls] == ["m1.extract_website", "m1.judge_novelty"]
    assert v.cost_usd == pytest.approx(0.02)
    assert {c["name"] for c in v.checks} >= {"avoid_list", "geography", "category_blocklist", "viability", "novelty"}
    entry = db_session.scalars(select(AuditLog).where(AuditLog.action == "company_verdict",
                                                     AuditLog.subject_id == str(company.id))).one()
    assert entry.is_judgment


def test_borderline_startup_is_rejected(db_session, http):
    company = _new(db_session, "Clonely", "clonely-test.io")
    v = evaluate(db_session, company, llm=FakeLLM(CompanyProfile=profile(), NoveltyJudgment=novelty("borderline")), http=http)
    assert not v.passed and v.failed_check == "novelty" and company.status == "rejected"


def test_blocklisted_yc_tag_rejects_with_no_ai_at_all(db_session, http):
    company = _new(db_session, "Snackr", "snackr-test.io", scout="yc", type_hint="startup",
                   data={"industries": ["Consumer", "Food Delivery"], "isHiring": True})
    llm = FakeLLM()
    v = evaluate(db_session, company, llm=llm, http=http)
    assert v.failed_check == "category_blocklist" and v.permanent and company.status == "blocked"
    assert llm.calls == []


def test_outsourcing_shop_fails_shared_check_before_judgment(db_session, http):
    company = _new(db_session, "Codefarm", "codefarm-test.io")
    llm = FakeLLM(CompanyProfile=profile(business_model="it_outsourcing"))
    v = evaluate(db_session, company, llm=llm, http=http)
    assert v.failed_check == "avoid_list"
    assert [p for _, p in llm.calls] == ["m1.extract_website"]


@pytest.mark.parametrize("direction, passed", [("up", True), ("lateral", True), ("down", False), ("unknown", False)])
def test_established_brand_judgment(db_session, http, direction, passed):
    owner = get_config().owner
    if owner is None or owner.placeholder_fields():
        pytest.skip("needs a filled-in config/owner_stub.yaml")
    company = _new(db_session, "Megacorp", "megacorp-test.com", scout="seeds", type_hint="established")
    llm = FakeLLM(
        CompanyProfile=profile(company_stage="established", employee_estimate="10000+"),
        BrandJudgment=BrandJudgment(direction=direction, reasoning="Because.", one_liner="Big company"),
    )
    v = evaluate(db_session, company, llm=llm, http=http)
    assert v.passed is passed and v.profile == "established"
    assert [p for _, p in llm.calls] == ["m1.extract_knowledge", "m1.judge_brand"]  # MNCs: no website scrape


def test_scaled_startup_in_blocked_category_is_judged_on_brand(db_session, http):
    """Urban-Company/Zepto case: a 1,000+-person startup isn't blocklisted or novelty-tested."""
    owner = get_config().owner
    if owner is None or owner.placeholder_fields():
        pytest.skip("needs a filled-in config/owner_stub.yaml")
    company = _new(db_session, "Quickcart", "quickcart-test.in", scout="seeds", type_hint="startup", data={})
    llm = FakeLLM(
        CompanyProfile=profile(blocklisted_category="quick_commerce", employee_estimate="1001-10000",
                               company_stage="startup"),
        BrandJudgment=BrandJudgment(direction="up", reasoning="Large, fast-growing.", one_liner="Quick commerce"),
    )
    v = evaluate(db_session, company, llm=llm, http=http)
    assert v.passed and v.profile == "established"
    assert any(c["name"] == "scaleup" for c in v.checks)
    assert "category_blocklist" not in {c["name"] for c in v.checks}
    assert [p for _, p in llm.calls] == ["m1.extract_website", "m1.judge_brand"]


def test_small_startup_in_blocked_category_is_still_blocked(db_session, http):
    company = _new(db_session, "Tinycart", "tinycart-test.in", scout="seeds", type_hint="startup", data={})
    llm = FakeLLM(CompanyProfile=profile(blocklisted_category="quick_commerce", employee_estimate="11-50"))
    v = evaluate(db_session, company, llm=llm, http=http)
    assert v.failed_check == "category_blocklist" and v.permanent


def test_override_flips_status_and_becomes_a_precedent_used_next_time(db_session, http):
    company = _new(db_session, "Clonely", "clonely-test.io")
    evaluate(db_session, company, llm=FakeLLM(CompanyProfile=profile(), NoveltyJudgment=novelty("clone")), http=http)

    v = override(db_session, company, "pass", "I know their team — real deep-tech angle")
    assert v.decided_by == "owner" and company.status == "passed"
    precedent = db_session.get(MemoryEntry, v.precedent_ids[0])
    assert precedent.kind == "precedent" and precedent.context["profile"] == "startup"
    machine_entry = db_session.scalars(select(AuditLog).where(
        AuditLog.action == "company_verdict", AuditLog.subject_id == str(company.id))).one()
    assert machine_entry.override_memory_id == precedent.id

    sibling = _new(db_session, "Clonely Labs", "clonely-labs-test.io")
    v2 = evaluate(db_session, sibling, llm=FakeLLM(CompanyProfile=profile(), NoveltyJudgment=novelty()), http=http)
    assert precedent.id in v2.precedent_ids


def test_permanent_thumbs_down_blocks(db_session):
    company = _new(db_session, "Nopeco", "nopeco-test.io")
    override(db_session, company, "fail", "never", permanent=True)
    assert company.status == "blocked"


def test_viability_reject_revives_on_new_hiring_signal(db_session, http):
    company = _new(db_session, "Sleepy", "sleepy-test.io", scout="seeds", type_hint="startup", data={})
    llm = FakeLLM(CompanyProfile=profile(actively_hiring="no"))
    # Force a known "not raised" so viability fails deterministically without search.
    from app.modules.m1_companies.facts import upsert_fact
    upsert_fact(db_session, company.id, "raised_within_window", "no", "inferred", "test")
    v = evaluate(db_session, company, llm=llm, http=http)
    assert v.failed_check == "viability" and company.status == "rejected"

    assert on_new_sighting(db_session, company, "remotive")
    assert company.status == "candidate" and company.reeval_reason == "refresh"


def test_rules_change_requeues_once(db_session, http):
    company = _new(db_session, "Clonely", "clonely-test.io")
    v = evaluate(db_session, company, llm=FakeLLM(CompanyProfile=profile(), NoveltyJudgment=novelty("clone")), http=http)
    check_rules_changes(db_session)
    assert company.status == "rejected"  # rules unchanged → stays rejected

    v.rules_versions = {**v.rules_versions, "startup": "old-version"}
    db_session.flush()
    check_rules_changes(db_session)
    assert company.status == "candidate" and company.reeval_reason == "rules_change"
