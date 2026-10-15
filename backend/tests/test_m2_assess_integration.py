"""Part 2b against the real database, with a scripted fake AI (no API cost)."""

from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.config_loader import get_config
from app.core.llm import LLMResult
from app.models import Company, CompanyFeed, JobAssessment, JobPosting, RawPosting
from app.modules.m1_companies.candidate import Candidate
from app.modules.m1_companies.identity import intake as company_intake
from app.modules.m2_jobs.assess import assess_one, due_clusters, profile_version, reapply_rules
from app.modules.m2_jobs.pipeline import intake

pytestmark = pytest.mark.integration


@pytest.fixture
def owner():
    o = get_config().owner
    if o is None or not o.level_ladder or not o.current_level:
        pytest.skip("needs level_ladder/current_level in config/owner_stub.yaml")
    return o


class FakeLLM:
    def __init__(self, **read):
        self.read = read
        self.calls = 0

    def structured(self, tier, *, system, user, schema, purpose, session=None, subject=None):
        self.calls += 1
        base = {
            "doable_from_india": "yes", "geography_basis": "india_location", "geography_evidence": "Bengaluru",
            "six_day_week": "no", "role_family": "data analytics", "family_match": "progression",
            "level": "unclear", "years_experience_min": -1, "people_management": "individual_contributor",
            "employment_type": "full_time", "services_flavored": False, "stated_pay": "none",
            "pay_estimate_currency": "INR", "pay_estimate_min": -1, "pay_estimate_max": -1,
            "pay_estimate_confidence": "low",
            "flags": {"travel": "none", "relocation_required": "no", "on_call": "no", "shift": "standard"},
            "one_liner": "Analytics role",
        }
        return LLMResult(parsed=schema.model_validate({**base, **self.read}), model="fake", cost_usd=0.0007,
                         input_tokens=1, output_tokens=1)


@pytest.fixture
def job(db_session, owner):
    company_intake(db_session, Candidate(scout="seeds", external_id="m2b-test.io", name="M2b",
                                         domain="m2b-test.io", type_hint="established"))
    company = db_session.scalars(select(Company).where(Company.domain == "m2b-test.io")).one()
    feed = CompanyFeed(company_id=company.id, method="greenhouse", slug="m2b", cadence="active", health="ok",
                       detected_vendors=[])
    db_session.add(feed)
    db_session.flush()
    now = datetime.now(timezone.utc)
    title = owner.target_roles[0] if owner.target_roles else "Senior Data Analyst"
    db_session.add(RawPosting(feed_id=feed.id, company_id=company.id, external_id="1", title=title,
                              location="Bengaluru, India", posted_at=now.isoformat(), payload={},
                              description="Own analytics. " * 30, content_hash="x1",
                              first_seen_at=now, last_seen_at=now))
    db_session.flush()
    intake(db_session, company_ids=[company.id])
    j = db_session.scalars(select(JobPosting).where(JobPosting.company_id == company.id)).one()
    assert j.filter_status == "passed", j.filter_reasons
    return j


def test_read_then_rules_once_per_cluster_and_free_reapply(db_session, job):
    version = profile_version()
    due = [(c, j) for c, j in due_clusters(db_session, version, 500) if j.id == job.id]
    assert due, "the passed posting should be due for its AI read"

    llm = FakeLLM(six_day_week="yes")
    row = assess_one(db_session, llm, due[0][0], job, version)
    assert row.outcome == "rejected" and "6-day work week" in row.reasons and llm.calls == 1

    # Not due again: same content, same profile.
    assert not [1 for c, j in due_clusters(db_session, version, 500) if j.id == job.id]

    # A rules-only change re-applies for free (no AI call).
    row.rules_version = "stale"
    db_session.flush()
    assert reapply_rules(db_session) >= 1
    assert llm.calls == 1 and db_session.get(JobAssessment, row.id).rules_version != "stale"
