"""Activation → first sync → later syncs → gardening, against the real DB with mocked HTTP."""

from datetime import datetime, timedelta, timezone

import pytest
import respx
from sqlalchemy import select

from app.core.http import PoliteClient
from app.models import Company, CompanyFeed, RawPosting
from app.modules.m1_companies.candidate import Candidate
from app.modules.m1_companies.identity import intake
from app.modules.m1_companies.watch.activation import activate
from app.modules.m1_companies.watch.gardening import check_staleness, prune, tune_cadence
from app.modules.m1_companies.watch.sync import sync_feed

pytestmark = pytest.mark.integration

JOBS = {"jobs": [
    {"id": 1, "title": "Senior Data Analyst", "location": {"name": "Bengaluru, India"},
     "absolute_url": "https://boards.greenhouse.io/zentest/jobs/1", "content": "Analyse"},
    {"id": 2, "title": "Product Analyst", "location": {"name": "Remote"},
     "absolute_url": "https://boards.greenhouse.io/zentest/jobs/2", "content": "Products"},
]}


@pytest.fixture(autouse=True)
def no_web_search(monkeypatch):
    monkeypatch.setattr("app.modules.m1_companies.watch.careers.search_available", lambda: False)


@pytest.fixture
def mock():
    with respx.mock(assert_all_called=False) as m:
        m.get(url__regex=r"https://[^/]+/robots\.txt").respond(404)
        yield m


@pytest.fixture
def http():
    with PoliteClient(default_interval=0, use_redis=False) as client:
        yield client


def _company(session, name="Zentest", domain="zentest-co.io", status="passed") -> Company:
    intake(session, Candidate(scout="hn", external_id=f"w-{domain}", name=name, domain=domain,
                              data={"headline": f"{name} | Analyst | Remote"}))
    c = session.scalars(select(Company).where(Company.domain == domain)).one()
    c.status = status
    return c


def _greenhouse(mock, jobs=JOBS):
    """Homepage links a Greenhouse board. Returns the jobs route so tests can change its answer."""
    mock.get(url__regex=r"https://zentest-co\.io/?$").respond(
        200, headers={"content-type": "text/html"}, html='<a href="https://boards.greenhouse.io/zentest">Careers</a>')
    mock.get("https://boards-api.greenhouse.io/v1/boards/zentest").respond(json={"name": "Zentest"})
    return mock.get("https://boards-api.greenhouse.io/v1/boards/zentest/jobs").respond(json=jobs)


def _open(session, feed) -> list[str]:
    return sorted(session.scalars(select(RawPosting.title).where(
        RawPosting.feed_id == feed.id, RawPosting.closed_at.is_(None))))


def test_activation_finds_ats_verifies_and_syncs_immediately(db_session, mock, http):
    _greenhouse(mock)
    company = _company(db_session)

    feed, first = activate(db_session, company, http)

    assert (feed.method, feed.slug, feed.health) == ("greenhouse", "zentest", "ok")
    assert "linked from the company's own site" in feed.ownership_evidence
    assert company.status == "watching" and first.new == 2 and feed.open_jobs == 2
    assert _open(db_session, feed) == ["Product Analyst", "Senior Data Analyst"]


def test_site_linking_two_boards_keeps_the_bigger_one(db_session, mock, http):
    mock.get(url__regex=r"https://zentest-co\.io/?$").respond(
        200, headers={"content-type": "text/html"},
        html='<a href="https://boards.greenhouse.io/zentest-labs">Labs jobs</a>'
             '<a href="https://jobs.lever.co/zentest">Careers</a>')
    mock.get("https://boards-api.greenhouse.io/v1/boards/zentest-labs/jobs").respond(json={"jobs": JOBS["jobs"][:1]})
    mock.get("https://boards-api.greenhouse.io/v1/boards/zentest-labs").respond(json={"name": "Zentest Labs"})
    mock.get("https://api.lever.co/v0/postings/zentest").respond(json=[
        {"id": f"l{i}", "text": f"Role {i}", "categories": {}, "hostedUrl": f"https://jobs.lever.co/zentest/l{i}"}
        for i in range(3)
    ])
    company = _company(db_session)

    feed, first = activate(db_session, company, http)

    assert (feed.method, feed.slug) == ("lever", "zentest")  # greenhouse comes first on the ladder, but has 1 job
    assert first.new == 3


def test_sync_closes_missing_jobs_and_reopens_returning_ones(db_session, mock, http):
    jobs_route = _greenhouse(mock)
    company = _company(db_session)
    feed, _ = activate(db_session, company, http)

    jobs_route.respond(json={"jobs": JOBS["jobs"][:1]})
    r = sync_feed(db_session, http, feed)
    assert r.closed == 1 and _open(db_session, feed) == ["Senior Data Analyst"]

    jobs_route.respond(json=JOBS)
    r = sync_feed(db_session, http, feed)
    assert r.reopened == 1 and len(_open(db_session, feed)) == 2


def test_sync_failure_backs_off_and_flags_reactivation(db_session, mock, http):
    jobs_route = _greenhouse(mock)
    company = _company(db_session)
    feed, _ = activate(db_session, company, http)

    jobs_route.respond(500)
    for _ in range(5):
        r = sync_feed(db_session, http, feed)
    assert r.error and feed.health == "failing" and feed.consecutive_failures == 5
    assert feed.needs_reactivation
    assert feed.next_sync_at > datetime.now(timezone.utc) + timedelta(hours=20)


def test_guessed_board_owned_by_someone_else_falls_back_to_aggregator(db_session, mock, http):
    company = _company(db_session, name="Quillmark", domain="quillmark-test.io")
    mock.get(url__regex=r"https://([\w-]+\.)?quillmark-test\.io.*").respond(404)  # no site, no careers page
    mock.get(url__regex=r"https://boards-api\.greenhouse\.io/v1/boards/[\w-]+/jobs").respond(json={"jobs": [
        {"id": 9, "title": "Plumber", "absolute_url": "https://boards.greenhouse.io/quillmark/jobs/9", "content": "Pipes"}]})
    mock.get(url__regex=r"https://boards-api\.greenhouse\.io/v1/boards/[\w-]+$").respond(json={"name": "Quillmark Plumbing"})
    mock.get(url__regex=r"https://api\.(lever\.co|ashbyhq\.com|smartrecruiters\.com)/.*").respond(404)

    feed, _ = activate(db_session, company, http)

    assert feed.method == "aggregator"
    assert "rejected" in feed.ownership_evidence


def test_gardening_cadence_staleness_and_pruning(db_session, mock, http):
    _greenhouse(mock)
    company = _company(db_session)
    feed, _ = activate(db_session, company, http)
    now = datetime.now(timezone.utc)

    # Quiet for 100 days with no fresh sightings → dormant cadence, then demoted.
    feed.last_change_at = now - timedelta(days=100)
    for s in company_sightings(db_session, company):
        s.last_seen_at = now - timedelta(days=100)
    db_session.flush()
    assert tune_cadence(db_session, now)["to_dormant"] >= 1 and feed.cadence == "dormant"
    prune(db_session, now)
    assert company.status == "dormant"

    # Job sites now list roles our feed doesn't → stale, flagged for re-activation.
    for i, title in enumerate(("Growth Lead", "Head of Data")):
        intake(db_session, Candidate(scout="adzuna", external_id=f"stale-{i}", name="Zentest", domain="zentest-co.io",
                                     data={"title": title}))
    assert check_staleness(db_session, now)["stale"] >= 1 and feed.needs_reactivation

    # Fresh sightings are a sign of life → active again → company revived.
    tune_cadence(db_session, now)
    prune(db_session, now)
    assert feed.cadence == "active" and company.status == "watching"


def company_sightings(session, company):
    from app.models import CompanySighting

    return session.scalars(select(CompanySighting).where(CompanySighting.company_id == company.id)).all()
