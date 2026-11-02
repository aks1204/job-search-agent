"""Part 6.0 against the real database: the reopening matrix (owner's decisions 2026-11-01). Nothing is sent."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.models import (
    Application, ApplicationEvent, Company, CompanyFeed, JobAssessment, JobPosting, OutreachCampaign, OutreachDraft,
    OutreachEmail, RawPosting, RoleChannel,
)
from app.modules.m1_companies.candidate import Candidate
from app.modules.m1_companies.identity import intake as company_intake
from app.modules.m2_jobs.pipeline import intake
from app.modules.m3_outreach import campaigns as C
from app.modules.m3_outreach import reopening as RO
from app.modules.m3_outreach.drafting import scenario_for
from app.modules.m4_apply import tracker as T

pytestmark = pytest.mark.integration
DOMAIN = "m6reopen-test.io"
NOW = datetime.now(timezone.utc)


@pytest.fixture
def roles(db_session):
    """Three roles at one company: Data Analyst and Analytics Manager (family "Analytics"), Product Manager
    (family "Product")."""
    company_intake(db_session, Candidate(scout="seeds", external_id=DOMAIN, name="Reopen Co", domain=DOMAIN,
                                         type_hint="established"))
    company = db_session.scalars(select(Company).where(Company.domain == DOMAIN)).one()
    feed = CompanyFeed(company_id=company.id, method="greenhouse", slug="m6", cadence="active", health="ok",
                       detected_vendors=[])
    db_session.add(feed)
    db_session.flush()
    titles = {"R-800001": "Data Analyst", "R-800002": "Analytics Manager", "R-800003": "Product Manager"}
    for ext, title in titles.items():
        db_session.add(RawPosting(feed_id=feed.id, company_id=company.id, external_id=ext, title=title,
                                  location="Bengaluru, India", posted_at=NOW.isoformat(), payload={},
                                  url=f"https://jobs.{DOMAIN}/{ext}", description="Analytics. " * 40,
                                  content_hash=ext[-4:], first_seen_at=NOW, last_seen_at=NOW))
    db_session.flush()
    intake(db_session, company_ids=[company.id])
    jobs = {j.title: j for j in db_session.scalars(select(JobPosting).where(JobPosting.company_id == company.id))}
    for title, family in (("Data Analyst", "Analytics"), ("Analytics Manager", "analytics"),
                          ("Product Manager", "Product")):
        job = jobs[title]
        db_session.add(JobAssessment(cluster_id=job.cluster_id, job_id=job.id, content_hash=job.content_hash,
                                     profile_version="t", rules_version="t", outcome="passed",
                                     read={"role_family": family}, reasons=[], flags=[], penalties=[], comp={},
                                     level={}))
    db_session.flush()
    return company, jobs["Data Analyst"].cluster_id, jobs["Analytics Manager"].cluster_id, jobs["Product Manager"].cluster_id


def _rejected(db_session, company, cluster_id, at, *, outside=False, interviewed=False):
    app = Application(cluster_id=cluster_id, company_id=company.id, status="rejected", status_at=at, outside=outside)
    db_session.add(app)
    db_session.flush()
    if interviewed:
        db_session.add(ApplicationEvent(application_id=app.id, kind="interviewing", source="email",
                                        at=at - timedelta(days=5)))
    db_session.add(ApplicationEvent(application_id=app.id, kind="rejected", source="email", at=at))
    db_session.flush()
    return app


def test_a_rejection_holds_outreach_for_similar_roles_for_30_days(db_session, roles):
    company, data, manager, product = roles
    at = NOW - timedelta(days=10)
    _rejected(db_session, company, data, at)
    until, why = RO.outreach_wait(db_session, manager)
    assert until == at + timedelta(days=30) and "similar role (analytics)" in why     # same family, any capitals
    assert RO.outreach_wait(db_session, product) is None                                # another family: not held
    assert RO.outreach_wait(db_session, manager, now=at + timedelta(days=31)) is None   # the wait is over
    assert "this role" in RO.outreach_wait(db_session, data)[1]                         # the role itself: never again


def test_after_an_interview_the_wait_is_90_days(db_session, roles):
    company, data, manager, _ = roles
    at = NOW - timedelta(days=40)
    _rejected(db_session, company, data, at, interviewed=True)
    until, why = RO.outreach_wait(db_session, manager)
    assert until == at + timedelta(days=90) and "after an interview" in why


def test_an_outside_application_starts_no_wait(db_session, roles):
    company, data, manager, _ = roles
    _rejected(db_session, company, data, NOW - timedelta(days=3), outside=True)
    assert RO.outreach_wait(db_session, manager) is None
    assert RO.history_scenario(db_session, manager) is None


def test_a_rejection_reply_to_outreach_counts_too(db_session, roles):
    company, data, manager, _ = roles
    draft = _draft(db_session, data)
    db_session.add(OutreachCampaign(cluster_id=data, company_id=company.id, draft_id=draft.id, draft_version=1,
                                    subject="s", body="{greeting}", status="cancelled", outcome="rejected",
                                    outcome_at=NOW - timedelta(days=2)))
    db_session.flush()
    assert RO.outreach_wait(db_session, manager) is not None


def _draft(db_session, cluster_id):
    d = OutreachDraft(cluster_id=cluster_id, version=1, scenario="fresh", role_name="Role", subject="Hello",
                      body="{greeting}\n\nAbout the opening.\n\nBest regards,\nMe", intro="i", points=[], req_ids=[],
                      attach_resume=False, status="ready", truth={}, inputs_version="x")
    db_session.add(d)
    db_session.flush()
    return d


def _channel(db_session, company, cluster_id, address):
    db_session.add(RoleChannel(cluster_id=cluster_id, company_id=company.id, mode="designated",
                               designated_email=address))
    db_session.flush()


def _campaigns(db_session, cluster_id):
    return db_session.scalars(select(OutreachCampaign).where(OutreachCampaign.cluster_id == cluster_id)
                              .order_by(OutreachCampaign.id)).all()


def test_a_draft_written_before_the_rejection_waits_too(db_session, roles):
    company, data, manager, product = roles
    _rejected(db_session, company, data, NOW - timedelta(days=1))
    for cluster_id in (manager, product):
        _draft(db_session, cluster_id)
        _channel(db_session, company, cluster_id, f"jobs{cluster_id}@{DOMAIN}")
    C.prepare_campaigns(db_session, [manager, product])
    assert _campaigns(db_session, manager) == []                  # similar: waits
    assert len(_campaigns(db_session, product)) == 1              # another family: goes ahead


def _redrafted(db_session, cluster_id):
    """What the draft step does for a role that opened again: a fresh draft, one version up."""
    d = db_session.scalars(select(OutreachDraft).where(OutreachDraft.cluster_id == cluster_id)).one()
    d.version, d.scenario, d.body = d.version + 1, "reopening", "{greeting}\n\nA fresh note.\n\nBest regards,\nMe"
    db_session.flush()


def _first_seen_long_ago(db_session, cluster_id):
    for j in db_session.scalars(select(JobPosting).where(JobPosting.cluster_id == cluster_id)).all():
        j.first_seen_at = NOW - timedelta(days=30)
    db_session.flush()


def test_after_silence_only_a_new_opening_gets_one_more_campaign(db_session, roles):
    company, data, _, _ = roles
    _first_seen_long_ago(db_session, data)
    _draft(db_session, data)
    _channel(db_session, company, data, f"careers@{DOMAIN}")
    C.prepare_campaigns(db_session, [data])
    first = _campaigns(db_session, data)[0]
    email = C.emails(db_session, first)[0]
    email.decision, email.status, email.sent_at = "approved", "sent", NOW - timedelta(days=12)
    first.status, first.outcome, first.ended_at, first.outcome_at = "done", "silent", NOW - timedelta(days=5), NOW - timedelta(days=5)
    db_session.flush()

    C.prepare_campaigns(db_session, [data])
    assert len(_campaigns(db_session, data)) == 1                 # the same posting: never emailed again
    assert RO.history_scenario(db_session, data) is None          # (and no redraft for it)

    job = db_session.scalars(select(JobPosting).where(JobPosting.cluster_id == data)).first()
    job.reopened_at = NOW                                         # closed, then reposted
    db_session.flush()
    assert RO.history_scenario(db_session, data) == "reopening"
    C.prepare_campaigns(db_session, [data])
    assert len(_campaigns(db_session, data)) == 1                 # not redrafted yet: the old text never goes again
    _redrafted(db_session, data)
    C.prepare_campaigns(db_session, [data])
    second = _campaigns(db_session, data)[-1]
    assert len(_campaigns(db_session, data)) == 2 and second.status == "pending_review"
    again = C.emails(db_session, second)[0]
    assert again.to_email == f"careers@{DOMAIN}" and again.previously_emailed_at == email.sent_at   # labelled
    C.prepare_campaigns(db_session, [data])
    assert len(_campaigns(db_session, data)) == 2                 # one more, not one per run


def test_people_emailed_before_go_last(db_session, roles):
    company, data, _, _ = roles
    _first_seen_long_ago(db_session, data)
    _draft(db_session, data)
    db_session.add(RoleChannel(cluster_id=data, company_id=company.id, mode="designated",
                               designated_email=f"old@{DOMAIN}", also_emails=[f"new@{DOMAIN}"]))
    db_session.flush()
    C.prepare_campaigns(db_session, [data])
    first = _campaigns(db_session, data)[0]
    for e in C.emails(db_session, first):
        if e.to_email == f"old@{DOMAIN}":
            e.decision, e.status, e.sent_at = "approved", "sent", NOW - timedelta(days=12)
        else:
            e.decision, e.status = "skipped", "cancelled"
    first.status, first.outcome, first.outcome_at = "done", "silent", NOW - timedelta(days=5)
    db_session.add(RawPosting(feed_id=db_session.scalars(select(CompanyFeed).where(CompanyFeed.company_id == company.id)).one().id,
                              company_id=company.id, external_id="R-800009", title="Data Analyst",
                              location="Bengaluru, India", posted_at=NOW.isoformat(), payload={},
                              url=f"https://jobs.{DOMAIN}/R-800009", description="Analytics. " * 40,
                              content_hash="0009", first_seen_at=NOW, last_seen_at=NOW))
    db_session.flush()
    intake(db_session, company_ids=[company.id])                  # a second opening (new job ID) for the role
    _redrafted(db_session, data)
    C.prepare_campaigns(db_session, [data])
    second = _campaigns(db_session, data)[-1]
    order = [(e.to_email, e.previously_emailed_at is not None) for e in C.emails(db_session, second)]
    assert order == [(f"new@{DOMAIN}", False), (f"old@{DOMAIN}", True)]


def test_the_draft_is_told_the_history_and_never_to_mention_it(db_session, roles):
    from app.modules.m3_outreach.drafting import SCENARIO_NOTES

    company, data, manager, _ = roles
    assert scenario_for(db_session, manager, ["R-1", "R-2"]) == "multi_req"
    _rejected(db_session, company, data, NOW - timedelta(days=100))
    assert scenario_for(db_session, manager, ["R-1", "R-2"]) == "post_rejection"
    db_session.add(ApplicationEvent(application_id=db_session.scalars(select(Application).where(
        Application.cluster_id == data)).one().id, kind="interviewing", source="you", at=NOW - timedelta(days=110)))
    db_session.flush()
    assert scenario_for(db_session, manager, []) == "post_interview"
    for key in ("reopening", "post_rejection", "post_interview"):
        assert "never mention" in SCENARIO_NOTES[key]


def test_the_roles_story_says_when_outreach_resumes(db_session, roles):
    company, data, manager, _ = roles
    _rejected(db_session, company, data, NOW - timedelta(days=10))
    last = [t for _, _, t in T.story(db_session, manager)][-1]
    assert last.startswith("outreach waits until") and "similar role" in last
    assert [t for _, _, t in T.story(db_session, data)][-1].startswith("no new outreach")


def test_a_reposted_job_records_when_it_came_back(db_session, roles):
    company, _, _, _ = roles
    raws = {r.external_id: r for r in db_session.scalars(select(RawPosting).where(RawPosting.company_id == company.id))}
    gone_a_while, missed_once = raws["R-800001"], raws["R-800002"]
    gone_a_while.closed_at, missed_once.closed_at = NOW - timedelta(days=4), NOW - timedelta(hours=2)
    db_session.flush()
    intake(db_session, company_ids=[company.id])
    gone_a_while.closed_at = missed_once.closed_at = None
    db_session.flush()
    intake(db_session, company_ids=[company.id])
    job = {r: db_session.scalars(select(JobPosting).where(JobPosting.raw_posting_id == raws[r].id)).one()
           for r in ("R-800001", "R-800002")}
    assert job["R-800001"].status == "open" and job["R-800001"].reopened_at is not None   # a repost
    assert job["R-800002"].status == "open" and job["R-800002"].reopened_at is None       # a feed hiccup


def test_a_rejection_stops_the_roles_outreach_and_holds_similar_reviews(db_session, roles):
    company, data, manager, _ = roles
    for cluster_id in (data, manager):
        _draft(db_session, cluster_id)
        _channel(db_session, company, cluster_id, f"jobs{cluster_id}@{DOMAIN}")
    C.prepare_campaigns(db_session, [data, manager])
    app = Application(cluster_id=data, company_id=company.id, status="applied", status_at=NOW - timedelta(days=3))
    db_session.add(app)
    db_session.flush()
    T.set_status(db_session, app, "rejected", source="email")
    own = _campaigns(db_session, data)[0]
    assert own.status == "cancelled" and all(e.status == "cancelled" for e in C.emails(db_session, own))
    similar = _campaigns(db_session, manager)[0]
    with pytest.raises(C.ReviewError, match="waits until"):
        C.decide(db_session, similar, C.emails(db_session, similar)[0], "approve", allow_low=True)
    assert similar.status == "pending_review"                     # held, not cancelled: it can go when the wait ends


def test_finished_outreach_is_not_redrafted(db_session, roles):
    from app.config_loader import get_config
    from app.models import JobScore
    from app.modules.m3_outreach.drafting import due_drafts
    from app.modules.m3_outreach.template import load_template

    if get_config().owner is None:
        pytest.skip("needs config/owner_stub.yaml")
    company, data, _, _ = roles
    a = db_session.scalars(select(JobAssessment).where(JobAssessment.cluster_id == data)).one()
    db_session.add(JobScore(cluster_id=data, assessment_id=a.id, similarity=0.8, similarity_version="t", score=90,
                            band="fast_lane", graded=True))
    _draft(db_session, data)                                       # inputs_version "x": out of date
    _channel(db_session, company, data, f"careers@{DOMAIN}")
    t = load_template()
    assert data in due_drafts(db_session, t, [data])               # no campaign yet: rewritten
    C.prepare_campaigns(db_session, [data])
    assert data in due_drafts(db_session, t, [data])               # waiting for your review: still rewritten
    first = _campaigns(db_session, data)[0]
    first.status, first.outcome = "done", "interested"
    db_session.flush()
    assert data not in due_drafts(db_session, t, [data])           # its outreach has run: a rewrite would never go
