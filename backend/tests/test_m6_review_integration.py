"""Part 6c against the real database: the Review screen shows each email once, as it will arrive, with who it
goes to; Approve / Skip / Undo per person; Edit draft; and only Begin outreach starts the waves (owner
2026-11-04). The Sending switch is faked off here (it lives in your real Redis), and nothing is sent."""

import re

import pytest
from sqlalchemy import select

from app.models import Bench, BenchMember, Contact, ContactEmail, DraftEdit, OutreachDraft, RoleChannel
from app.modules.m3_outreach import campaigns as C
from tests.test_m6_web_integration import DOMAIN, NOW, client, roles  # noqa: F401  (same fixtures)

pytestmark = pytest.mark.integration
BODY = "{greeting}\n\nI came across the Analytics Manager opening.\n\n1. Analytics: built the team's dashboards.\n\nBest regards,\nMe"


@pytest.fixture(autouse=True)
def sending_off(monkeypatch):
    from app.modules.m3_outreach import sender as S

    monkeypatch.setattr(S, "paused", lambda: True)


@pytest.fixture
def campaign(db_session, roles):
    company, _data, manager = roles
    bench = Bench(company_id=company.id, key="analytics/india", shape="mnc", searched={})
    db_session.add(bench)
    db_session.flush()
    for rank, (name, conf) in enumerate((("Asha Rao", "verified"), ("Bala Iyer", "verified"),
                                         ("Chitra Nair", "low")), 1):
        first, last = name.lower().split()
        c = Contact(company_id=company.id, full_name=name, name_key=f"{first} {last}", first_name=first,
                    last_name=last, kind="recruiting", source="search", geo="india", location="Bengaluru")
        db_session.add(c)
        db_session.flush()
        db_session.add(ContactEmail(company_id=company.id, contact_id=c.id, email=f"{first}.{last}@{DOMAIN}",
                                    source="constructed", confidence=conf, verified_at=NOW))
        db_session.add(BenchMember(bench_id=bench.id, contact_id=c.id, slot_kind="recruiting", rank=rank, co_suspects=[]))
    db_session.add(RoleChannel(cluster_id=manager, company_id=company.id, mode="bench", bench_id=bench.id))
    db_session.add(OutreachDraft(cluster_id=manager, version=1, scenario="fresh", role_name="Analytics Manager",
                                 subject="Application for Analytics Manager in Web Co (3 YoE)", body=BODY, intro="i",
                                 points=[], req_ids=[], attach_resume=False, status="ready", truth={},
                                 inputs_version="x"))
    db_session.flush()
    C.prepare_campaigns(db_session, [manager])
    return db_session.scalars(select(C.OutreachCampaign).where(C.OutreachCampaign.cluster_id == manager)).one()


def token_of(client) -> str:
    return re.search(r'name="token" value="([^"]+)"', client.get("/review").text).group(1)


def press(client, url: str, follow: bool = False, **fields):
    return client.post(url, data={"token": token_of(client), "next": "/review", **fields}, follow_redirects=follow)


def card_of(page: str, campaign) -> str:
    """This test's own card: your real database may hold real campaigns too."""
    start = page.index(f'id="c{campaign.id}"')
    end = page.find('<div id="c', start + 1)                 # the next card
    return page[start:end if end > 0 else None]


def _emails(db_session, campaign):
    db_session.expire_all()
    return C.emails(db_session, campaign)


def test_the_email_shows_once_as_it_will_arrive_with_who_it_goes_to(client, campaign):
    whole = client.get("/review").text
    assert "Sending is off" in whole
    page = card_of(whole, campaign)
    assert page.count("I came across the Analytics Manager opening.") == 1          # once, not per person
    assert 'class="greet"' in page and "{greeting}" not in page                      # the greeting is marked
    for who in ("Asha Rao", "Bala Iyer", "Chitra Nair"):
        assert who in page
    assert "address verified" in page and "address not proven" in page
    assert "3 of 3 still to decide" in page and "Day 1" in page and "Day 2" in page
    assert re.search(r'<button type="button" class="primary" disabled>Begin outreach</button>', page)
    assert f'href="/review/{campaign.id}/edit"' in page


def test_approve_then_undo_on_the_card(client, db_session, campaign):
    es = _emails(db_session, campaign)
    r = press(client, f"/do/email/{es[0].id}", action="approve")
    assert r.status_code == 303 and r.headers["location"] == f"/review#c{campaign.id}"
    page = client.get("/review").text
    assert "Approved: Asha Rao. 2 still to decide." in page and f'action="/do/email-undo/{es[0].id}"' in page
    assert _emails(db_session, campaign)[0].decision == "approved"
    press(client, f"/do/email-undo/{es[0].id}")
    assert _emails(db_session, campaign)[0].decision == "pending"


def test_an_unproven_address_needs_approve_anyway(client, db_session, campaign):
    low = _emails(db_session, campaign)[2]
    press(client, f"/do/email/{low.id}", action="approve")
    assert "use Approve anyway if you&#39;re sure" in client.get("/review").text       # (the page escapes the ')
    assert _emails(db_session, campaign)[2].decision == "pending"
    press(client, f"/do/email/{low.id}", action="approve_low")
    e = _emails(db_session, campaign)[2]
    assert e.decision == "approved" and e.allow_low


def test_approve_all_leaves_unproven_ones_and_skip_keeps_your_reason(client, db_session, campaign):
    press(client, f"/do/campaign/{campaign.id}", action="approve_all")
    es = _emails(db_session, campaign)
    assert [e.decision for e in es] == ["approved", "approved", "pending"]
    press(client, f"/do/email/{es[2].id}", action="skip", chips=["address"], reason="bounced before")
    e = _emails(db_session, campaign)[2]
    assert e.decision == "skipped" and e.status_reason == "skipped by you: The address looks wrong; bounced before"


def test_only_begin_outreach_starts_the_waves_and_it_asks_first(client, db_session, campaign):
    for e in _emails(db_session, campaign):
        press(client, f"/do/email/{e.id}", action="skip" if e.confidence == "low" else "approve")
    db_session.expire_all()
    assert db_session.get(C.OutreachCampaign, campaign.id).status == "pending_review"   # the last click didn't start it
    asked = press(client, f"/do/campaign/{campaign.id}", action="begin").text
    assert "Begin outreach: 2 email(s)?" in asked and "Day 1: Asha Rao" in asked and "Sending is off" in asked
    db_session.expire_all()
    assert db_session.get(C.OutreachCampaign, campaign.id).status == "pending_review"   # asking changed nothing
    press(client, f"/do/campaign/{campaign.id}", action="begin", confirmed="1")
    db_session.expire_all()
    c = db_session.get(C.OutreachCampaign, campaign.id)
    assert c.status == "scheduled" and c.armed_at is not None
    whole = client.get("/review").text
    page = card_of(whole, campaign)
    assert "Outreach begun: 2 email(s)" in page and whole.index("UNDER WAY") < whole.index(f'id="c{campaign.id}"')
    assert "/do/email-undo/" not in page                                                 # no taking back now


def test_begin_waits_until_everyone_is_decided(client, db_session, campaign):
    press(client, f"/do/campaign/{campaign.id}", action="begin", confirmed="1")
    db_session.expire_all()
    assert db_session.get(C.OutreachCampaign, campaign.id).status == "pending_review"
    assert "3 still need your Approve or Skip first" in client.get("/review").text


def test_edit_draft_keeps_what_you_typed_on_a_mistake_and_resets_approvals_on_save(client, db_session, campaign):
    es = _emails(db_session, campaign)
    press(client, f"/do/email/{es[0].id}", action="approve")
    form = client.get(f"/review/{campaign.id}/edit").text
    assert "I came across the Analytics Manager opening." in form and "{greeting}" in form
    token = re.search(r'name="token" value="([^"]+)"', form).group(1)
    r = client.post(f"/review/{campaign.id}/edit", data={"token": token, "subject": "New subject",
                                                         "body": "Hello all,\n\nMy new text."})
    assert r.status_code == 200 and "keep the {greeting} line" in r.text and "My new text." in r.text
    r = client.post(f"/review/{campaign.id}/edit", follow_redirects=False,
                    data={"token": token, "subject": "New subject", "body": BODY.replace("came across", "saw")})
    assert r.status_code == 303 and r.headers["location"] == f"/review#c{campaign.id}"
    db_session.expire_all()
    c = db_session.get(C.OutreachCampaign, campaign.id)
    assert c.subject == "New subject" and "I saw the Analytics Manager" in c.body and c.edited
    assert C.emails(db_session, c)[0].decision == "pending"
    assert db_session.scalars(select(DraftEdit).where(DraftEdit.cluster_id == c.cluster_id)).one()
    assert "Approvals were reset" in client.get("/review").text


def test_a_fresh_draft_asks_first_with_its_cost(client, db_session, campaign, monkeypatch):
    from app.core.llm import LLM

    monkeypatch.setattr(LLM, "available", staticmethod(lambda: True))
    asked = press(client, f"/do/campaign/{campaign.id}", action="redraft").text
    assert "Write a fresh draft?" in asked and "cents" in asked
    db_session.expire_all()
    assert db_session.get(C.OutreachCampaign, campaign.id).draft_version == 1        # nothing rewritten yet


def test_a_review_ping_opens_the_campaigns_card(db_session, campaign):
    from app.models import Notification
    from app.web.pages import ping_target

    n = Notification(kind="needs_you", section="needs_you", title="Outreach ready", subject_type="campaign",
                     subject_id=str(campaign.id))
    assert ping_target(db_session, n) == f"/review#c{campaign.id}"
