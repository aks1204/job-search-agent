"""Part 3a against the real database, with fake DNS / mail server / search (nothing leaves the box)."""

from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.core.redis_client import get_redis
from app.core.search import SearchResult
from app.models import (
    Bench, Company, CompanyFeed, Contact, ContactEmail, JobAssessment, JobPosting, JobScore, MailDomain, RawPosting,
)
from app.modules.m1_companies.candidate import Candidate
from app.modules.m1_companies.identity import intake as company_intake
from app.modules.m2_jobs.pipeline import intake
from app.modules.m3_outreach import addresses, enrich
from app.modules.m3_outreach import people as P
from app.modules.m3_outreach.bench import TickBudget, usable_members
from app.modules.m3_outreach.pipeline import decide_channel, pursued_roles
from app.modules.m3_outreach.verify import SMTP_BLOCKED_KEY, verify_candidates

pytestmark = pytest.mark.integration
DOMAIN = "m3acme-test.io"


@pytest.fixture(autouse=True)
def no_smtp_block():
    get_redis().delete(SMTP_BLOCKED_KEY)
    yield
    get_redis().delete(SMTP_BLOCKED_KEY)


@pytest.fixture(autouse=True)
def no_paid_lookups(monkeypatch):
    monkeypatch.setattr(enrich, "lookup_person", lambda *a, **k: None)
    monkeypatch.setattr(enrich, "hunter_pattern", lambda *a, **k: None)


def resolver(domain):
    return ["mx.test"]


class FakeSMTP:
    def __init__(self, valid=(), catch_all="no"):
        self.valid, self.catch_all, self.calls = set(valid), catch_all, 0

    def __call__(self, hosts, emails, *, timeout):
        self.calls += 1
        return {e: ("valid" if e in self.valid else "invalid") for e in emails} | {"__catch_all__": self.catch_all}


@pytest.fixture
def role(db_session):
    company_intake(db_session, Candidate(scout="seeds", external_id=DOMAIN, name="M3 Acme", domain=DOMAIN,
                                         type_hint="established"))
    company = db_session.scalars(select(Company).where(Company.domain == DOMAIN)).one()
    feed = CompanyFeed(company_id=company.id, method="greenhouse", slug="m3acme", cadence="active", health="ok",
                       detected_vendors=[])
    db_session.add(feed)
    db_session.flush()
    now = datetime.now(timezone.utc)
    db_session.add(RawPosting(feed_id=feed.id, company_id=company.id, external_id="1", title="Senior Data Analyst",
                              location="Bengaluru, India", posted_at=now.isoformat(), payload={},
                              description="Commercial analytics with SQL and Python. " * 10,
                              content_hash="m3", first_seen_at=now, last_seen_at=now))
    db_session.flush()
    intake(db_session, company_ids=[company.id])
    job = db_session.scalars(select(JobPosting).where(JobPosting.company_id == company.id)).one()
    a = JobAssessment(cluster_id=job.cluster_id, job_id=job.id, content_hash=job.content_hash, profile_version="t",
                      rules_version="t", outcome="passed", read={"role_family": "business analytics"},
                      reasons=[], flags=[], penalties=[], comp={}, level={})
    db_session.add(a)
    db_session.flush()
    db_session.add(JobScore(cluster_id=job.cluster_id, assessment_id=a.id, similarity=0.8, similarity_version="t",
                            score=90, band="fast_lane", graded=True))
    db_session.flush()
    return company, job


def _row(db_session, company, email):
    r = ContactEmail(company_id=company.id, email=email, source="constructed")
    db_session.add(r)
    db_session.flush()
    return r


def test_smtp_picks_the_real_address_and_rules_out_the_rest(db_session, role):
    company, _ = role
    rows = [_row(db_session, company, f"{x}@{DOMAIN}") for x in ("p.sharma", "priya.sharma")]
    chosen = verify_candidates(db_session, None, rows, pool=[], resolver=resolver, smtp=FakeSMTP({f"priya.sharma@{DOMAIN}"}))
    assert chosen.email == f"priya.sharma@{DOMAIN}" and chosen.confidence == "verified"
    assert rows[0].confidence == "invalid"
    assert db_session.get(MailDomain, DOMAIN).catch_all is False


def test_catch_all_domain_is_permanently_low_confidence(db_session, role):
    company, _ = role
    smtp = FakeSMTP(catch_all="yes")
    first = _row(db_session, company, f"a.b@{DOMAIN}")
    assert verify_candidates(db_session, None, [first], pool=[], resolver=resolver, smtp=smtp) is None
    assert first.confidence == "low" and db_session.get(MailDomain, DOMAIN).catch_all is True
    second = _row(db_session, company, f"c.d@{DOMAIN}")
    verify_candidates(db_session, None, [second], pool=[], resolver=resolver, smtp=smtp)
    assert second.confidence == "low" and smtp.calls == 1  # no more checks wasted on a catch-all domain


def test_no_checker_available_means_low_confidence_not_verified(db_session, role):
    company, _ = role
    row = _row(db_session, company, f"x.y@{DOMAIN}")
    verify_candidates(db_session, None, [row], pool=[], resolver=resolver, smtp=lambda *a, **k: None)
    assert row.confidence == "low" and get_redis().exists(SMTP_BLOCKED_KEY)


def test_mailbox_history_beats_everything(db_session, role):
    company, _ = role
    row = _row(db_session, company, f"old.friend@{DOMAIN}")
    row.replied_count = 1
    assert verify_candidates(db_session, None, [row], pool=[], resolver=resolver, smtp=FakeSMTP()) is row
    assert row.confidence == "proven"


def _results():
    return [
        SearchResult("Priya Sharma - Talent Acquisition - M3 Acme | LinkedIn", "https://www.linkedin.com/in/priya-s-1",
                     "Talent Acquisition at M3 Acme · Bengaluru, India"),
        SearchResult("Priya Sharma - Talent Acquisition Partner - M3 Acme | LinkedIn", "https://www.linkedin.com/in/priya-s-2",
                     "Recruiting for M3 Acme analytics · Hyderabad, India"),
        SearchResult("Rahul Verma - Senior Recruiter - M3 Acme | LinkedIn", "https://www.linkedin.com/in/rahul-verma",
                     "Senior Recruiter, M3 Acme · Bengaluru"),
        SearchResult("Anil Kumar - Recruiter - Globex | LinkedIn", "https://www.linkedin.com/in/anil-k",
                     "Recruiter at Globex, ex-M3 Acme · Pune"),
    ]


def test_bench_fill_freezes_namesakes_and_builds_verified_addresses(db_session, role, monkeypatch):
    company, job = role
    queries = []
    monkeypatch.setattr(P, "web_search", lambda http, q, **k: queries.append(q) or _results())
    row = pursued_roles(db_session, [job.cluster_id])[0]
    smtp = FakeSMTP({f"rahul.verma@{DOMAIN}"})
    channel = decide_channel(db_session, None, None, *row, TickBudget(4), pool=[], resolver=resolver, smtp=smtp)

    assert channel.mode == "bench" and len(queries) == 1 and 'site:linkedin.com/in "M3 Acme"' in queries[0]
    ready = usable_members(db_session, db_session.get(Bench, channel.bench_id))
    assert [(c.full_name, e.email, e.confidence) for _, c, e in ready] == [("Rahul Verma", f"rahul.verma@{DOMAIN}", "verified")]
    priyas = db_session.scalars(select(Contact).where(Contact.company_id == company.id, Contact.name_key == "priya sharma")).all()
    assert len(priyas) == 2 and all(p.status == "frozen" for p in priyas)      # never guessed at
    assert not db_session.scalars(select(Contact).where(Contact.linkedin_url.like("%anil-k"))).all()  # left the company
    assert db_session.get(MailDomain, DOMAIN).pattern == "{first}.{last}"     # learned from the confirmed guess


def test_designated_address_is_the_only_channel(db_session, role, monkeypatch):
    company, job = role
    job.description = "Great team. Interested? Send your CV to jobs@m3acme-test.io with the role in the subject."
    monkeypatch.setattr(P, "web_search", lambda *a, **k: pytest.fail("must not search for people"))
    row = pursued_roles(db_session, [job.cluster_id])[0]
    channel = decide_channel(db_session, None, None, *row, TickBudget(4), pool=[], resolver=resolver, smtp=FakeSMTP())
    assert channel.mode == "designated" and channel.designated_email == "jobs@m3acme-test.io"
    assert channel.bench_id is None


def test_learned_pattern_is_used_without_guessing(db_session, role):
    company, _ = role
    db_session.add(MailDomain(domain=DOMAIN, mx_hosts=["mx.test"], has_mx=True, pattern="{f}{last}",
                              mx_checked_at=datetime.now(timezone.utc)))
    c = Contact(company_id=company.id, full_name="Meera Iyer", name_key="meera iyer", first_name="meera",
                last_name="iyer", kind="recruiting", source="search")
    db_session.add(c)
    db_session.flush()
    smtp = FakeSMTP({f"miyer@{DOMAIN}"})
    addr = addresses.find_address(db_session, None, c, company, allow_enrich=False, startup=False, pool=[],
                                  resolver=resolver, smtp=smtp)
    assert addr.email == f"miyer@{DOMAIN}" and addr.confidence == "verified" and addr.pattern == "{f}{last}"


def test_other_formats_are_tried_when_the_domain_pattern_is_refused(db_session, role):
    company, _ = role
    db_session.add(MailDomain(domain=DOMAIN, mx_hosts=["mx.test"], has_mx=True, pattern="{first}.{last}",
                              mx_checked_at=datetime.now(timezone.utc)))
    c = Contact(company_id=company.id, full_name="Elizabeth Holt", name_key="elizabeth holt", first_name="elizabeth",
                last_name="holt", kind="recruiting", source="search")
    db_session.add(c)
    db_session.flush()
    addr = addresses.find_address(db_session, None, c, company, allow_enrich=False, startup=False, pool=[],
                                  resolver=resolver, smtp=FakeSMTP({f"eholt@{DOMAIN}"}))
    assert addr.email == f"eholt@{DOMAIN}" and addr.confidence == "verified"
    assert db_session.get(MailDomain, DOMAIN).pattern == "{first}.{last}"   # one exception doesn't relearn the domain
