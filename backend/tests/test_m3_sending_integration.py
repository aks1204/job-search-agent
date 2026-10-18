"""Part 3c against the real database with a fake Gmail: review → waves → send → reply hold → bounce breaker.
Nothing is really sent."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.config_loader import get_config
from app.models import (
    Bench, BenchMember, Company, CompanyFeed, Contact, ContactEmail, DraftEdit, JobPosting, MailAccount,
    OutreachDraft, RawPosting, RoleChannel,
)
from app.modules.m1_companies.candidate import Candidate
from app.modules.m1_companies.identity import intake as company_intake
from app.modules.m2_jobs.pipeline import intake
from app.modules.m3_outreach import campaigns as C
from app.modules.m3_outreach import sender as S
from app.modules.m3_outreach.drafting import style_examples

pytestmark = pytest.mark.integration
DOMAIN = "m3send-test.io"
NOON_IST = datetime(2026, 10, 19, 6, 30, tzinfo=timezone.utc)


@pytest.fixture
def campaign(db_session):
    if get_config().owner is None:
        pytest.skip("needs config/owner_stub.yaml")
    company_intake(db_session, Candidate(scout="seeds", external_id=DOMAIN, name="Send Co", domain=DOMAIN,
                                         type_hint="established"))
    company = db_session.scalars(select(Company).where(Company.domain == DOMAIN)).one()
    feed = CompanyFeed(company_id=company.id, method="greenhouse", slug="m3s", cadence="active", health="ok",
                       detected_vendors=[])
    db_session.add(feed)
    db_session.flush()
    now = datetime.now(timezone.utc)
    db_session.add(RawPosting(feed_id=feed.id, company_id=company.id, external_id="1", title="Senior Data Analyst",
                              location="Bengaluru, India", posted_at=now.isoformat(), payload={},
                              description="Analytics. " * 40, content_hash="s1", first_seen_at=now, last_seen_at=now))
    db_session.flush()
    intake(db_session, company_ids=[company.id])
    job = db_session.scalars(select(JobPosting).where(JobPosting.company_id == company.id)).one()
    bench = Bench(company_id=company.id, key="analytics/india", shape="mnc", searched={})
    db_session.add(bench)
    db_session.flush()
    people = [("Asha Rao", "verified"), ("Bala Iyer", "verified"), ("Chitra Nair", "low"), ("Dev Shah", "verified")]
    for rank, (name, conf) in enumerate(people, 1):
        first, last = name.lower().split()
        c = Contact(company_id=company.id, full_name=name, name_key=f"{first} {last}", first_name=first, last_name=last,
                    kind="recruiting", source="search", geo="india", location="Bengaluru")
        db_session.add(c)
        db_session.flush()
        db_session.add(ContactEmail(company_id=company.id, contact_id=c.id, email=f"{first}.{last}@{DOMAIN}",
                                    source="constructed", confidence=conf, verified_at=now))
        db_session.add(BenchMember(bench_id=bench.id, contact_id=c.id, slot_kind="recruiting", rank=rank, co_suspects=[]))
    db_session.add(RoleChannel(cluster_id=job.cluster_id, company_id=company.id, mode="bench", bench_id=bench.id))
    db_session.add(OutreachDraft(cluster_id=job.cluster_id, version=1, scenario="fresh", role_name="Senior Data Analyst",
                                 subject="Application for Senior Data Analyst in Send Co (3 YoE)",
                                 body="{greeting}\n\nI came across the opening.\n\nBest regards,\nMe", intro="i",
                                 points=[], req_ids=[], attach_resume=False, status="ready", truth={},
                                 inputs_version="x"))
    db_session.add(MailAccount(email="me@gmail.com", refresh_token_enc="not-used-by-the-fake", scopes="x", status="ok"))
    db_session.flush()
    C.prepare_campaigns(db_session, [job.cluster_id])
    return db_session.scalars(select(C.OutreachCampaign).where(C.OutreachCampaign.cluster_id == job.cluster_id)).one()


class FakeGmail:
    def __init__(self):
        self.sent = []

    def __call__(self, session, msg):
        self.sent.append(msg)
        return f"m{len(self.sent)}", f"t{len(self.sent)}"


def test_review_is_per_email_and_low_confidence_needs_your_go_ahead(db_session, campaign):
    es = C.emails(db_session, campaign)
    assert [e.to_name for e in es] == ["Asha Rao", "Bala Iyer", "Chitra Nair", "Dev Shah"]
    assert campaign.status == "pending_review"
    with pytest.raises(C.ReviewError):
        C.decide(db_session, campaign, es[2], "approve")                 # low-confidence: not without --allow-low
    C.decide(db_session, campaign, es[0], "approve")
    C.decide(db_session, campaign, es[1], "approve")
    C.decide(db_session, campaign, es[2], "skip")
    assert campaign.status == "pending_review"                            # one still waiting
    C.decide(db_session, campaign, es[3], "approve")
    assert campaign.status == "scheduled" and [e.wave for e in es if e.decision == "approved"] == [0, 0, 1]


def test_your_edit_resets_approvals_and_becomes_a_style_example(db_session, campaign):
    es = C.emails(db_session, campaign)
    C.decide(db_session, campaign, es[0], "approve")
    C.edit(db_session, campaign, campaign.subject, campaign.body.replace("I came across", "I recently saw"))
    assert es[0].decision == "pending" and campaign.edited
    assert db_session.scalars(select(DraftEdit).where(DraftEdit.cluster_id == campaign.cluster_id)).one()
    examples = style_examples(db_session)
    assert "-I came across the opening." in examples and "+I recently saw the opening." in examples
    with pytest.raises(C.ReviewError):
        C.edit(db_session, campaign, campaign.subject, "no greeting line")


def _approve_all(db_session, campaign):
    for e in C.emails(db_session, campaign):
        C.decide(db_session, campaign, e, "approve", allow_low=True)


def test_waves_send_day_zero_first_and_a_reply_holds_the_company(db_session, campaign):
    _approve_all(db_session, campaign)
    campaign.armed_at = NOON_IST
    due = S.next_candidates(db_session, NOON_IST)
    assert [e.to_name for e, _ in due] == ["Asha Rao", "Bala Iyer"]       # day 1 waits for tomorrow
    fake = FakeGmail()
    e, c = due[0]
    assert S.send_one(db_session, e, c, sender=fake) == "sent"
    assert fake.sent[0]["To"] == f"asha.rao@{DOMAIN}" and fake.sent[0].get_content().startswith("Hi Asha,")
    assert e.sent_body.startswith("Hi Asha,") and c.status == "running"

    reply = [{"id": "r1", "from": f"Asha Rao <asha.rao@{DOMAIN}>", "subject": "Re: Application",
              "date": "", "snippet": "Thanks, let's talk", "sent_by_me": False}]
    assert S.check_thread(db_session, e, reader=lambda s, t: reply) == "reply"
    assert c.status == "paused" and S.next_candidates(db_session, NOON_IST) == []   # nothing more goes out
    assert db_session.get(ContactEmail, e.address_id).confidence == "proven"
    C.resume_company(db_session, c.company_id)
    assert c.status == "running"


def test_bounces_trip_the_breaker_and_distrust_the_guessed_pattern(db_session, campaign):
    from app.models import MailDomain

    _approve_all(db_session, campaign)
    campaign.armed_at = NOON_IST - timedelta(days=3)
    db_session.add(MailDomain(domain=DOMAIN, mx_hosts=[], has_mx=True, pattern="{first}.{last}"))
    b = S.breaker(db_session)
    b.state, b.reset_at, b.history = "ok", datetime.now(timezone.utc) - timedelta(seconds=1), []
    fake = FakeGmail()
    es = [e for e in C.emails(db_session, campaign)]
    for e in es[:2]:
        S.send_one(db_session, e, campaign, sender=fake)
    for e in es[2:]:  # keep the cleanup away from real DNS: the rest of the queue is already proven
        db_session.get(ContactEmail, e.address_id).confidence = "proven"
    bounce = [{"id": "b", "from": "Mail Delivery Subsystem <mailer-daemon@googlemail.com>",
               "subject": "Delivery Status Notification (Failure)", "date": "", "snippet": "Address not found",
               "sent_by_me": False}]
    assert S.check_thread(db_session, es[0], reader=lambda s, t: bounce) == "bounce"
    assert b.state == "ok"                                                    # one bounce isn't enough
    assert S.check_thread(db_session, es[1], reader=lambda s, t: bounce) == "bounce"
    assert b.state == "tripped" and db_session.get(MailDomain, DOMAIN).pattern_distrusted
    assert db_session.get(ContactEmail, es[0].address_id).confidence == "invalid"
