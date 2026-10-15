"""Part 2c against the real database: brand call, similarity, AI grade (fake), feedback."""

from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.config_loader import get_config
from app.core.llm import LLMResult
from app.models import Company, CompanyFeed, JobAssessment, JobPosting, JobScore, RawPosting
from app.modules.m1_companies.candidate import Candidate
from app.modules.m1_companies.identity import intake as company_intake
from app.modules.m2_jobs import scoring as S
from app.modules.m2_jobs.brand import brand_level, set_brand
from app.modules.m2_jobs.pipeline import intake

pytestmark = pytest.mark.integration


@pytest.fixture
def setup(db_session):
    owner = get_config().owner
    if owner is None or not owner.level_ladder or not owner.current_level:
        pytest.skip("needs a filled-in config/owner_stub.yaml")
    company_intake(db_session, Candidate(scout="seeds", external_id="m2c-test.io", name="M2c Bank",
                                         domain="m2c-test.io", type_hint="established"))
    company = db_session.scalars(select(Company).where(Company.domain == "m2c-test.io")).one()
    feed = CompanyFeed(company_id=company.id, method="greenhouse", slug="m2c", cadence="active", health="ok",
                       detected_vendors=[])
    db_session.add(feed)
    db_session.flush()
    now = datetime.now(timezone.utc)
    db_session.add(RawPosting(feed_id=feed.id, company_id=company.id, external_id="1",
                              title=owner.target_roles[0] if owner.target_roles else "Senior Data Analyst",
                              location="Bengaluru, India", posted_at=now.isoformat(), payload={},
                              description="SQL, Python and stakeholder management for commercial analytics. " * 10,
                              content_hash="c1", first_seen_at=now, last_seen_at=now))
    db_session.flush()
    intake(db_session, company_ids=[company.id])
    job = db_session.scalars(select(JobPosting).where(JobPosting.company_id == company.id)).one()
    a = JobAssessment(cluster_id=job.cluster_id, job_id=job.id, content_hash=job.content_hash,
                      profile_version="t", rules_version="t", outcome="passed",
                      read={"one_liner": "Commercial analytics", "role_family": "business analytics"},
                      reasons=[], flags=[], penalties=[], comp={}, level={})
    db_session.add(a)
    db_session.flush()
    return company, job, a


class FakeJudge:
    def __init__(self, value):
        self.value, self.calls = value, 0

    def structured(self, tier, *, system, user, schema, purpose, session=None, subject=None):
        self.calls += 1
        assert tier == "judge" and "--- resume ---" in system
        return LLMResult(parsed=schema.model_validate({
            "dimensions": {d: self.value for d in S.DIMENSIONS}, "reasons": ["Strong SQL match"],
            "missing": ["dbt"], "flags": [], "one_liner": "Core analytics role"}),
            model="fake", cost_usd=0.012, input_tokens=1, output_tokens=1)


def test_brand_call_is_kept_and_marks_assessments_for_reapply(db_session, setup):
    company, job, a = setup
    assert brand_level(db_session, company) == "unknown"
    assert set_brand(db_session, company, "up", "Stronger brand than my employer") == 1
    assert brand_level(db_session, company) == "up"
    db_session.refresh(a)
    assert a.rules_version == "brand-changed"


def test_similarity_then_grade_then_feedback(db_session, setup):
    company, job, a = setup
    S.update_similarities(db_session)
    row = db_session.scalars(select(JobScore).where(JobScore.cluster_id == job.cluster_id)).one()
    assert not row.graded and row.band in ("maybe", "archive")      # ungraded never above Maybe

    judge = FakeJudge(90)
    S.grade_one(db_session, judge, row)
    assert row.graded and row.score == 90 and row.band == "fast_lane" and row.grade["missing"] == ["dbt"]
    assert row not in S.due_for_grading(db_session, 500)              # not re-graded until something changes

    S.record_feedback(db_session, row, "skip", "Not the right domain")
    assert row.band == "archive" and row.owner_action == "skip"
