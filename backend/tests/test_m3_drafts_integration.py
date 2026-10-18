"""Part 3b against the real database with a fake AI: draft → truth check → rewrite → cut → assemble."""

from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.config_loader import get_config
from app.core.llm import LLMResult
from app.models import Company, CompanyFeed, JobAssessment, JobPosting, JobScore, RawPosting, RoleChannel
from app.modules.m1_companies.candidate import Candidate
from app.modules.m1_companies.identity import intake as company_intake
from app.modules.m2_jobs.pipeline import intake
from app.modules.m3_outreach import drafting as D
from app.modules.m3_outreach.pipeline import pursued_roles
from app.modules.m3_outreach.template import load_template

pytestmark = pytest.mark.integration
DOMAIN = "m3draft-test.io"
FAKE = "I managed a team of 40 analysts."


@pytest.fixture
def role(db_session):
    if get_config().owner is None:
        pytest.skip("needs config/owner_stub.yaml")
    company_intake(db_session, Candidate(scout="seeds", external_id=DOMAIN, name="Draft Co", domain=DOMAIN,
                                         type_hint="established"))
    company = db_session.scalars(select(Company).where(Company.domain == DOMAIN)).one()
    feed = CompanyFeed(company_id=company.id, method="greenhouse", slug="m3d", cadence="active", health="ok",
                       detected_vendors=[])
    db_session.add(feed)
    db_session.flush()
    now = datetime.now(timezone.utc)
    for ext in ("1", "2"):  # two openings for the same role → multi-req
        db_session.add(RawPosting(feed_id=feed.id, company_id=company.id, external_id=f"/jobs/R-55500{ext}",
                                  title="Senior Data Analyst", location="Bengaluru, India", posted_at=now.isoformat(),
                                  payload={}, description="Commercial analytics with SQL. " * 20,
                                  content_hash=f"d{ext}", first_seen_at=now, last_seen_at=now))
    db_session.flush()
    intake(db_session, company_ids=[company.id])
    job = db_session.scalars(select(JobPosting).where(JobPosting.company_id == company.id)
                             .order_by(JobPosting.id)).first()
    a = JobAssessment(cluster_id=job.cluster_id, job_id=job.id, content_hash=job.content_hash, profile_version="t",
                      rules_version="t", outcome="passed", read={"role_family": "business analytics"},
                      reasons=[], flags=[], penalties=[], comp={}, level={})
    db_session.add(a)
    db_session.flush()
    db_session.add(JobScore(cluster_id=job.cluster_id, assessment_id=a.id, similarity=0.8, similarity_version="t",
                            score=90, band="fast_lane", graded=True, grade={"reasons": ["Strong SQL"]}))
    db_session.add(RoleChannel(cluster_id=job.cluster_id, company_id=company.id, mode="designated",
                               designated_email=f"jobs@{DOMAIN}"))
    db_session.flush()
    return job


class FakeAI:
    """Draft has one made-up claim; the rewrite keeps it; the checker keeps flagging it → it gets cut."""

    def __init__(self):
        self.calls = []

    def structured(self, tier, *, system, user, schema, purpose, session=None, subject=None):
        self.calls.append(purpose)
        if schema is D.DraftOut:
            assert "--- resume ---" in system and "Never mention gaps" in system
            parsed = D.DraftOut(role_name="Senior Data Analyst", role_short="Senior Data Analyst",
                                intro=f"I am an analytics consultant. {FAKE}",
                                points=[D.Point(heading=f"Area {i}", text=f"Did real thing {i}.") for i in range(1, 5)])
        else:
            parsed = D.TruthCheck(claims=[
                D.Claim(part="intro", sentence="I am an analytics consultant.", claim="consultant", supported=True,
                        evidence="Consultant"),
                D.Claim(part="intro", sentence=FAKE, claim="managed 40", supported=False, evidence="")])
        return LLMResult(parsed=parsed, model="fake", cost_usd=0.01, input_tokens=1, output_tokens=1)


def test_draft_is_checked_rewritten_cut_and_assembled(db_session, role):
    ai = FakeAI()
    row = pursued_roles(db_session, [role.cluster_id])[0]
    draft = D.write_draft(db_session, ai, *row)

    assert ai.calls == ["m3.draft", "m3.verify_draft", "m3.draft", "m3.verify_draft"]  # one rewrite, then cut
    assert FAKE not in draft.body and draft.truth["cut"] == [FAKE] and draft.status == "ready"
    assert draft.scenario == "multi_req" and draft.req_ids == ["R-555001", "R-555002"]
    assert "(job IDs: R-555001, R-555002)" in draft.body
    assert "1. Area 1: Did real thing 1." in draft.body and "4. Area 4" in draft.body
    assert get_config().owner.name in draft.body and "{greeting}" in draft.body
    assert draft.subject.startswith("Application for Senior Data Analyst in Draft Co")

    _, body = D.render_for(draft, load_template(), "Priya Sharma")
    assert body.startswith("Hi Priya,")
    assert role.cluster_id not in D.due_drafts(db_session, load_template())  # nothing changed → no rewrite
