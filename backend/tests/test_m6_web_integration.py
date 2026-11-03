"""Part 6a against the real database: every screen opens, shows the right things, and shows them safely.
The pages read through the test's own session (rolled back afterwards); nothing is written."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import get_session
from app.main import app
from app.models import Application, ApplicationEvent, Company, CompanyFeed, JobAssessment, JobPosting, JobScore, RawPosting
from app.modules.m1_companies.candidate import Candidate
from app.modules.m1_companies.identity import intake as company_intake
from app.modules.m2_jobs.pipeline import intake

pytestmark = pytest.mark.integration
DOMAIN = "m6web-test.io"
NOW = datetime.now(timezone.utc)
EVIL_TITLE = "Data Analyst <script>alert(1)</script>"


@pytest.fixture
def client(db_session):
    app.dependency_overrides[get_session] = lambda: db_session
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_session, None)


@pytest.fixture
def roles(db_session):
    """A fast-lane Data Analyst (title with page code in it, a javascript: link and a hostile description),
    and a fast-lane Analytics Manager in the same family."""
    company_intake(db_session, Candidate(scout="seeds", external_id=DOMAIN, name="Web Co", domain=DOMAIN,
                                         type_hint="established"))
    company = db_session.scalars(select(Company).where(Company.domain == DOMAIN)).one()
    feed = CompanyFeed(company_id=company.id, method="greenhouse", slug="m6w", cadence="active", health="ok",
                       detected_vendors=[])
    db_session.add(feed)
    db_session.flush()
    for ext, title, url in (("W-1", EVIL_TITLE, "javascript:alert(2)"),
                            ("W-2", "Analytics Manager", f"https://jobs.{DOMAIN}/W-2")):
        db_session.add(RawPosting(feed_id=feed.id, company_id=company.id, external_id=ext, title=title,
                                  location="Bengaluru, India", posted_at=NOW.isoformat(), payload={}, url=url,
                                  description='<img src=x onerror="alert(3)"> Analytics. ' * 20,
                                  content_hash=ext, first_seen_at=NOW, last_seen_at=NOW))
    db_session.flush()
    intake(db_session, company_ids=[company.id])
    jobs = {j.title: j for j in db_session.scalars(select(JobPosting).where(JobPosting.company_id == company.id))}
    out = []
    for title in (EVIL_TITLE, "Analytics Manager"):
        job = jobs[title]
        a = JobAssessment(cluster_id=job.cluster_id, job_id=job.id, content_hash=job.content_hash, profile_version="t",
                          rules_version="t", outcome="passed", read={"role_family": "Analytics"}, reasons=[], flags=[],
                          penalties=[], comp={}, level={})
        db_session.add(a)
        db_session.flush()
        db_session.add(JobScore(cluster_id=job.cluster_id, assessment_id=a.id, similarity=0.8, similarity_version="t",
                                score=93, band="fast_lane", graded=True, grade={"one_liner": "A strong fit"},
                                banded_at=NOW))
        out.append(job.cluster_id)
    db_session.flush()
    return company, out[0], out[1]


SCREENS = ["/jobs", "/jobs?tab=fast", "/jobs?tab=maybe", "/jobs?tab=applied", "/review", "/companies",
           "/companies?status=all", "/profile", "/memory", "/audit", "/audit?group=outreach", "/settings", "/maintenance"]


def test_every_screen_opens(client, roles):
    company, data, manager = roles
    assert client.get("/", follow_redirects=False).headers["location"] == "/jobs"
    for path in [*SCREENS, f"/roles/{data}", f"/roles/{manager}", f"/companies/{company.id}"]:
        r = client.get(path)
        assert r.status_code == 200, path
        assert "<nav" in r.text and "Job agent" in r.text, path
    assert client.get("/roles/999999999").status_code == 404


def test_text_from_postings_is_shown_never_run(client, roles):
    _, data, _ = roles
    for page in (client.get("/jobs?tab=fast").text, client.get(f"/roles/{data}").text):
        assert "<script>alert(1)</script>" not in page and "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    role = client.get(f"/roles/{data}").text
    assert '<img src=x onerror' not in role and "&lt;img src=x onerror" in role
    assert 'href="javascript:' not in role                          # a posting's javascript: link isn't a link


def test_a_role_hidden_by_a_wait_is_left_out(client, db_session, roles):
    company, data, manager = roles
    assert f'/roles/{manager}"' in client.get("/jobs?tab=fast").text
    app_row = Application(cluster_id=data, company_id=company.id, status="rejected", status_at=NOW - timedelta(days=2))
    db_session.add(app_row)
    db_session.flush()
    db_session.add(ApplicationEvent(application_id=app_row.id, kind="rejected", source="email", at=app_row.status_at))
    db_session.flush()
    jobs = client.get("/jobs?tab=fast").text
    assert f'/roles/{manager}"' not in jobs                          # similar role: hidden during the wait
    assert "hidden until" in client.get(f"/roles/{manager}").text   # (its own page says why)


def test_the_new_badge_counts_this_weeks_matches(db_session, roles):
    from app.web.pages import _nav

    _, data, _ = roles
    before = _nav(db_session)["new"]
    score = db_session.scalars(select(JobScore).where(JobScore.cluster_id == data)).one()
    score.banded_at = NOW - timedelta(days=8)                        # older than a week
    db_session.flush()
    assert _nav(db_session)["new"] == before - 1


def test_api_keys_are_never_shown(client, monkeypatch):
    from app.settings import get_settings

    monkeypatch.setattr(get_settings(), "anthropic_api_key", "sk-ant-TEST-never-show-me-123")
    page = client.get("/settings").text
    assert "never-show-me" not in page and "set ✓" in page


def test_pay_shows_on_profile_and_nowhere_else(client, db_session, roles):
    """Your pay, and the lowest pay you'd take, in every form they're stored in — memory answers, a role's
    reasons, audit entries (including wording saved before 2026-11-03) — never appear outside Profile."""
    from app.core import audit
    from app.modules.m5_profile.memory import PAY_QUESTION, act, record_answer

    company, data, _ = roles
    pay = record_answer(db_session, PAY_QUESTION, "9876543 INR a year", module="m5.profile", expires_days=180)
    record_answer(db_session, "Only consider roles paying at least ~87 LPA?", "yes", module="m5.profile")
    a = db_session.scalars(select(JobAssessment).where(JobAssessment.cluster_id == data)).one()
    a.reasons = ["pay: below the floor you set (about 22 vs 87 LPA)", "pay: about 26% of your current pay (ctc)"]
    audit.record(db_session, "m5", "direction_declined", "You said no: Only consider roles paying at least ~87 LPA? "
                 "(You usually skip roles paying below ~87 LPA (5 skipped).)", subject_type="profile")
    db_session.flush()
    act(db_session, pay.id, "pin")                                    # a memory action writes an audit entry
    for path in [p for p in SCREENS if p != "/profile"] + [f"/roles/{data}", f"/companies/{company.id}",
                                                           "/memory?q=pay"]:
        page = client.get(path).text
        for secret in ("9876543", "98,76,543", "87 LPA", "26% of your current pay"):
            assert secret not in page, (path, secret)
    role = client.get(f"/roles/{data}").text
    assert "this job: about 22 LPA" in role                           # the job's own pay still shows
    assert "Your current pay — kept private, see Profile" in client.get("/memory").text


def test_a_hidden_roles_page_has_no_apply_links(client, db_session, roles):
    company, data, manager = roles
    link = f"https://jobs.{DOMAIN}/W-2"
    assert link in client.get(f"/roles/{manager}").text
    app_row = Application(cluster_id=data, company_id=company.id, status="rejected", status_at=NOW - timedelta(days=2))
    db_session.add(app_row)
    db_session.flush()
    page = client.get(f"/roles/{manager}").text
    assert link not in page and "Hidden until" in page and "similar role" in page


def test_looking_at_memory_doesnt_count_as_using_it(client, db_session):
    from app.modules.m5_profile.memory import record_answer

    entry = record_answer(db_session, "Are you open to relocating abroad for m6web?", "yes", module="m5.profile")
    entry.last_used_at, entry.usage_count = None, 0
    db_session.flush()
    page = client.get("/memory?q=relocating abroad").text
    assert "relocating abroad for m6web" in page                         # the search did find it
    assert entry.last_used_at is None and entry.usage_count == 0          # (same session: no refresh needed)


def test_settings_opens_when_the_counter_store_is_down(client, monkeypatch):
    import app.core.quota as quota
    from app.settings import get_settings

    def down(_service):
        raise ConnectionError("redis is not running")

    monkeypatch.setattr(get_settings(), "brave_api_key", "test-key")
    monkeypatch.setattr(quota, "used_this_month", down)
    monkeypatch.setattr(quota, "used_today", down)
    monkeypatch.setattr("app.modules.m3_outreach.sender.paused", lambda: down("paused"))
    r = client.get("/settings")
    assert r.status_code == 200
    assert "usage unknown" in r.text and "company websites" in r.text
    assert 'ch-gray">unknown<' in r.text                                # the Sending switch: unknown, not an error


def test_the_badge_leaves_out_blocked_companies(db_session, roles):
    from app.web.pages import _nav

    company, _, _ = roles
    before = _nav(db_session)["new"]
    company.status = "blocked"                                       # e.g. your past employer
    db_session.flush()
    assert _nav(db_session)["new"] == before - 2
