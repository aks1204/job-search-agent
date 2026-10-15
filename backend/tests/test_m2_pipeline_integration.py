"""Part 2a intake against the real database: normalise, cluster siblings, filter, re-sync, close."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.models import Company, CompanyFeed, JobCluster, JobPosting, RawPosting
from app.modules.m1_companies.candidate import Candidate
from app.modules.m1_companies.identity import intake as company_intake
from app.modules.m2_jobs.pipeline import intake

pytestmark = pytest.mark.integration


@pytest.fixture
def feed(db_session):
    company_intake(db_session, Candidate(scout="seeds", external_id="m2co-test.io", name="M2co",
                                         domain="m2co-test.io", type_hint="established"))
    company = db_session.scalars(select(Company).where(Company.domain == "m2co-test.io")).one()
    f = CompanyFeed(company_id=company.id, method="workday", slug="m2co", site="EXT", host="m2co.wd5",
                    cadence="active", health="ok", detected_vendors=[])
    db_session.add(f)
    db_session.flush()
    return f


def _raw(db_session, feed, ext, title, location="IN: Bengaluru", posted="Posted 2 Days Ago", hash_="h1", **kw):
    now = datetime.now(timezone.utc)
    row = RawPosting(feed_id=feed.id, company_id=feed.company_id, external_id=ext, title=title, location=location,
                     url=f"https://m2co.wd5.myworkdayjobs.com/EXT/job/{ext}", posted_at=posted, payload={},
                     content_hash=hash_, first_seen_at=now, last_seen_at=now, **kw)
    db_session.add(row)
    db_session.flush()
    return row


def _job(db_session, raw) -> JobPosting:
    return db_session.scalars(select(JobPosting).where(JobPosting.raw_posting_id == raw.id)).one()


def test_intake_normalises_clusters_siblings_and_filters(db_session, feed):
    a = _raw(db_session, feed, "R-1", "Senior Data Analyst")
    b = _raw(db_session, feed, "R-2", "Senior Data Analyst")  # sibling: same role, another req ID
    c = _raw(db_session, feed, "R-3", "Senior Microbiologist", location="US, Concord NC")
    old = _raw(db_session, feed, "R-4", "Product Analyst", posted="Posted 30+ Days Ago")

    counts = intake(db_session, company_ids=[feed.company_id])

    assert counts["new"] == 4
    ja, jb, jc, jo = (_job(db_session, r) for r in (a, b, c, old))
    assert ja.cluster_id == jb.cluster_id and ja.cluster_id != jc.cluster_id
    assert db_session.get(JobCluster, ja.cluster_id).size == 2
    assert ja.is_india and ja.filter_status == "passed" and ja.description_status == "pending"  # Workday: fetch later
    assert jc.filter_status == "failed"  # outside the net or a noise title — either way, not shown
    assert jo.filter_status == "failed" and jo.filter_reasons[0].startswith("posted 3")


def test_resync_only_moves_last_seen_and_change_marks_rescore(db_session, feed):
    a = _raw(db_session, feed, "R-9", "Senior Data Analyst")
    intake(db_session, company_ids=[feed.company_id])
    job = _job(db_session, a)

    a.last_seen_at = datetime.now(timezone.utc) + timedelta(hours=6)
    db_session.flush()
    intake(db_session, company_ids=[feed.company_id])
    db_session.refresh(job)
    assert job.changed_at is None and job.last_seen_at == a.last_seen_at

    a.content_hash, a.title = "h2", "Senior Data Analyst, Commercial"
    db_session.flush()
    intake(db_session, company_ids=[feed.company_id])
    db_session.refresh(job)
    assert job.changed_at is not None and job.title == "Senior Data Analyst, Commercial"


def test_closed_raw_posting_closes_the_job(db_session, feed):
    a = _raw(db_session, feed, "R-7", "Senior Data Analyst")
    intake(db_session, company_ids=[feed.company_id])
    a.closed_at = datetime.now(timezone.utc)
    db_session.flush()
    assert intake(db_session, company_ids=[feed.company_id])["closed"] == 1
    assert _job(db_session, a).status == "closed"
