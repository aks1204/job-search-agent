"""Module 4 against the real database, with fake hiring-system emails (nothing is read from Gmail)."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.models import (
    Application, Company, CompanyFeed, InboxMessage, JobAssessment, JobPosting, JobScore, Notification, RawPosting,
)
from app.modules.m1_companies.candidate import Candidate
from app.modules.m1_companies.identity import intake as company_intake
from app.modules.m2_jobs.pipeline import intake
from app.modules.m4_apply import inbox as I
from app.modules.m4_apply import tracker as T

pytestmark = pytest.mark.integration
DOMAIN = "m4apply-test.io"


@pytest.fixture
def roles(db_session):
    """Two roles at one company: a Data Analyst (two openings, fast lane) and a Product Analyst (not pursued)."""
    company_intake(db_session, Candidate(scout="seeds", external_id=DOMAIN, name="Apply Co", domain=DOMAIN,
                                         type_hint="established"))
    company = db_session.scalars(select(Company).where(Company.domain == DOMAIN)).one()
    feed = CompanyFeed(company_id=company.id, method="greenhouse", slug="m4", cadence="active", health="ok",
                       detected_vendors=[])
    db_session.add(feed)
    db_session.flush()
    now = datetime.now(timezone.utc)
    for ext, title in (("R-700001", "Data Analyst"), ("R-700002", "Data Analyst"), ("R-700003", "Product Analyst")):
        db_session.add(RawPosting(feed_id=feed.id, company_id=company.id, external_id=ext, title=title,
                                  location="Bengaluru, India", posted_at=now.isoformat(), payload={},
                                  url=f"https://jobs.{DOMAIN}/{ext}", description="Analytics. " * 40,
                                  content_hash=ext[-4:], first_seen_at=now, last_seen_at=now))
    db_session.flush()
    intake(db_session, company_ids=[company.id])
    jobs = db_session.scalars(select(JobPosting).where(JobPosting.company_id == company.id)).all()
    data = next(j for j in jobs if j.title == "Data Analyst")
    product = next(j for j in jobs if j.title == "Product Analyst")
    for job, band in ((data, "fast_lane"), (product, "maybe")):
        a = JobAssessment(cluster_id=job.cluster_id, job_id=job.id, content_hash=job.content_hash, profile_version="t",
                          rules_version="t", outcome="passed", read={}, reasons=[], flags=[], penalties=[], comp={},
                          level={})
        db_session.add(a)
        db_session.flush()
        db_session.add(JobScore(cluster_id=job.cluster_id, assessment_id=a.id, similarity=0.8, similarity_version="t",
                                score=90 if band == "fast_lane" else 50, band=band, graded=True))
    db_session.flush()
    return company, data.cluster_id, product.cluster_id


def _mine(db_session, cluster_id):
    return db_session.scalars(select(Application).where(Application.cluster_id == cluster_id)).one_or_none()


def _mail(mid, subject, kind, *, job_id="", title="", sender="Apply Co <noreply@myworkday.com>", days_ago=0):
    m = {"id": mid, "thread_id": f"t-{mid}", "from": sender, "subject": subject, "snippet": subject,
         "internal_ms": int((datetime.now(timezone.utc) - timedelta(days=days_ago)).timestamp() * 1000)}
    read = I.AtsRead(about_application=True, kind=kind, company="Apply Co", job_title=title, job_id=job_id,
                     summary=subject, confidence=0.95)
    return m, read


def test_pursued_role_goes_on_the_list_with_every_opening_and_others_dont(db_session, roles):
    _, data, product = roles
    T.queue_roles(db_session)
    app = _mine(db_session, data)
    assert app.status == "queued" and app.on_list
    assert sorted(l["req_id"] for l in app.links) == ["R-700001", "R-700002"]       # apply to all openings
    assert _mine(db_session, product) is None                                         # a Maybe isn't listed
    assert db_session.scalars(select(Notification).where(Notification.subject_type == "job_cluster",
                                                    Notification.subject_id == str(data))).one().kind == "needs_you"
    full, _ = T.apply_list(db_session)
    assert data in [cl.id for _, cl, _ in full]
    T.check_links(db_session, None, limit=10_000, checker=lambda http, url: (True, "live"))
    assert all(l["ok"] for l in _mine(db_session, data).links)


def test_confirmation_email_marks_applied_then_status_follows(db_session, roles):
    _, data, _ = roles
    T.queue_roles(db_session)
    m, read = _mail("m1", "We received your application for Data Analyst (R-700002)", "received", job_id="R-700002")
    assert I.handle(db_session, m, read) == "matched"
    app = _mine(db_session, data)
    assert app.status == "confirmed" and app.applied_how == "email" and app.receipt_link.endswith("t-m1")
    full, folded = T.apply_list(db_session)
    assert data not in [cl.id for _, cl, _ in full]                                   # off the to-do list
    m, read = _mail("m2", "Interview invitation", "interview", title="Data Analyst")
    I.handle(db_session, m, read)
    assert app.status == "interviewing"
    assert db_session.get(InboxMessage, "m2").outcome == "matched"                     # each email read once


def test_applying_to_a_role_not_on_your_list_is_still_tracked(db_session, roles):
    _, _, product = roles
    m, read = _mail("m3", "Thanks for applying: Product Analyst", "received", title="Product Analyst")
    assert I.handle(db_session, m, read) == "matched"
    app = _mine(db_session, product)
    assert app.status == "confirmed" and not app.on_list


def test_unclear_email_asks_you_instead_of_guessing(db_session, roles):
    company, data, product = roles
    T.mark_applied(db_session, data)
    T.mark_applied(db_session, product)
    m, read = _mail("m4", "Update on your application", "rejection")
    assert I.handle(db_session, m, read) == "ambiguous"
    assert _mine(db_session, data).status == "applied" and _mine(db_session, product).status == "applied"
    ask = db_session.scalars(select(Notification).where(Notification.title.like(f"{company.name}: which role%"))).one()
    assert ask.kind == "needs_you"


def test_silence_skip_close_and_the_story(db_session, roles):
    _, data, product = roles
    T.queue_roles(db_session)
    T.mark_applied(db_session, data, "applied on the careers site")
    later = datetime.now(timezone.utc) + timedelta(days=22)
    assert T.stamp_silence(db_session, later) >= 1 and _mine(db_session, data).status == "silent"
    texts = [t for _, _, t in T.story(db_session, data)]
    assert texts[0].startswith("queued") and any("applied" in t for t in texts) and texts[-1].startswith("silent")
    with pytest.raises(ValueError):
        T.skip(db_session, product, "not for me")                                    # never on the list
    for j in db_session.scalars(select(JobPosting).where(JobPosting.cluster_id == data)).all():
        j.status = "closed"
    T.queue_roles(db_session)
    assert _mine(db_session, data).status == "silent"                                 # applied stays applied


def test_an_old_rejected_application_doesnt_swallow_a_new_unclear_email(db_session, roles):
    _, data, product = roles
    T.mark_applied(db_session, data)
    T.set_status(db_session, _mine(db_session, data), "rejected", source="email")
    m, read = _mail("m5", "Update on your application", "in_review")   # no title, no job id
    assert I.handle(db_session, m, read) == "ambiguous"                # you're asked; not pinned on the old one
    assert _mine(db_session, data).status == "rejected"


def test_emails_from_a_companys_other_mail_domain_are_searched(db_session, roles, monkeypatch):
    from app.config_loader import get_config

    T.mark_applied(db_session, roles[1])
    monkeypatch.setitem(get_config().outreach.contacts.mail_domains, DOMAIN, "mail-m4apply-test.io")
    q = I.build_query(db_session)
    assert "from:mail-m4apply-test.io" in q and "from:myworkday.com" in q
