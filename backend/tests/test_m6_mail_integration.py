"""Part 6d against the real database with a fake Gmail: the summary mail and the two alerts. Nothing is really
sent, and the switches are faked (they live in your real Redis), so a test never turns your mail on."""

from datetime import datetime, timedelta, timezone

import pytest

from app.models import MailAccount, MailLog, Notification
from app.modules.m6_mail import alerts as A
from app.modules.m6_mail import summary as M
from app.modules.m6_mail import switches
from tests.test_m6_reopening_integration import _rejected
from tests.test_m6_web_integration import EVIL_TITLE, NOW, client, roles  # noqa: F401  (same fixtures)

pytestmark = pytest.mark.integration
NOON_IST = datetime(2026, 1, 5, 6, 30, tzinfo=timezone.utc)
NIGHT_IST = datetime(2026, 1, 5, 18, 0, tzinfo=timezone.utc)      # 23:30 in India: quiet hours


class FakeGmail:
    def __init__(self):
        self.sent = []

    def __call__(self, session, msg):
        self.sent.append(msg)
        return "m1", "t1"

    def html(self, i=-1) -> str:
        return self.sent[i].get_body(preferencelist=("html",)).get_content()

    def text(self, i=-1) -> str:
        return self.sent[i].get_body(preferencelist=("plain",)).get_content()


@pytest.fixture
def gmail_account(db_session):
    db_session.add(MailAccount(email="me@gmail.com", refresh_token_enc="not-used-by-the-fake", scopes="x", status="ok"))
    db_session.flush()


@pytest.fixture
def switched(monkeypatch):
    """Which switches are on, and since when (instead of your real Redis)."""
    on: dict[str, datetime] = {}
    monkeypatch.setattr(switches, "on_since", lambda name: on.get(name))
    return on


def test_the_summary_leads_with_new_fast_lane_roles_and_carries_no_pay(db_session, roles):
    _company, data, manager = roles
    db_session.add(Notification(kind="needs_you", section="needs_you", title="Is your pay still right?",
                                detail="Your pay was about 45% of your current pay"))
    db_session.flush()
    s = M.build(db_session, now=NOW + timedelta(seconds=1), since=NOW - timedelta(seconds=1))
    text, html = M.render(s)
    news = next(sec for sec in s.sections if sec.key == "new")
    titles = [c.title for c in news.cards]
    assert any(t.startswith("Analytics Manager · Web Co") for t in titles)
    assert any(c.chip.endswith("fast lane") and c.buttons[0][0] == "Open role →" for c in news.cards)  # javascript: link dropped
    assert "<script>" not in html and "&lt;script&gt;" in html                                       # escaped
    assert "45%" not in html and "45%" not in text                                                  # pay stays on Profile
    assert "new match" in s.subject and not s.quiet
    assert s.sections[0].key == "needs_you"                                                           # needs you first


def test_a_role_hidden_by_a_wait_is_not_in_the_mail(db_session, roles):
    company, data, manager = roles
    _rejected(db_session, company, data, NOW - timedelta(days=1))           # turned down for a similar role
    s = M.build(db_session, now=NOW + timedelta(seconds=1), since=NOW - timedelta(seconds=1))
    _, html = M.render(s)
    assert "Analytics Manager · Web Co" not in html                         # hidden until the wait ends
    assert "Data Analyst &lt;script&gt;" not in html                         # applied to and turned down: ended, not "new"


def test_a_quiet_day_sends_nothing_but_a_preview_always_goes(db_session, roles, gmail_account):
    fake = FakeGmail()
    later = NOW + timedelta(hours=1)
    db_session.add(MailLog(kind="summary", subject="earlier", counts={}, sent_at=NOW + timedelta(minutes=30)))
    db_session.flush()
    result = M.send_summary(db_session, now=later, sender=fake)
    if result["sent"]:                       # your real database may have news from the last half hour
        pytest.skip("real activity in the last half hour")
    assert fake.sent == [] and result["why"] == "nothing new since the last summary"
    result = M.send_summary(db_session, preview=True, now=later, sender=fake)
    assert result["sent"] and fake.sent[0]["Subject"].startswith("Preview · Job Agent:")
    assert fake.sent[0]["To"] == "me@gmail.com" and "Settings" in fake.html()
    assert M.last_summary(db_session) == NOW + timedelta(minutes=30)        # a preview doesn't move the start


def test_alerts_are_off_until_switched_on_and_wait_out_quiet_hours(db_session, roles, gmail_account, switched):
    fake = FakeGmail()
    assert A.send_alerts(db_session, now=NOON_IST, sender=fake)["why"] == "alerts are off"
    switched["fast"] = NOW - timedelta(minutes=1)
    assert "quiet hours" in A.send_alerts(db_session, now=NIGHT_IST, sender=fake)["why"]
    assert fake.sent == []


def test_fast_lane_finds_and_replies_come_in_one_alert_and_only_once(db_session, roles, gmail_account, switched):
    _company, data, manager = roles
    switched["fast"] = switched["reply"] = NOW - timedelta(minutes=1)
    reply = Notification(kind="ping", section="happened", title="Web Co: Asha Rao replied — interested",
                         detail="Keen to talk.", link="https://mail.google.com/mail/#all/t9")
    db_session.add(reply)
    db_session.flush()
    fake = FakeGmail()
    result = A.send_alerts(db_session, now=NOON_IST, sender=fake)
    assert result["sent"] and result["replies"] >= 1 and result["fast"] >= 2
    html = fake.html()
    assert "Analytics Manager · Web Co" in html and "Open in Gmail" in html and "Keen to talk." in html
    assert reply.delivered_at == NOON_IST
    again = A.send_alerts(db_session, now=NOON_IST + timedelta(minutes=3), sender=fake)
    assert len(fake.sent) == 1 and not again["sent"]                        # each told once


def test_settings_shows_the_switches_off_and_turns_one_on(client, monkeypatch):
    import re

    flipped = []
    monkeypatch.setattr(switches, "on_since", lambda name: None)
    monkeypatch.setattr(switches, "set_on", lambda name, on: flipped.append((name, on)))
    page = client.get("/settings").text
    assert "Summary mail (twice a day)" in page and "Send me the summary now" in page
    assert "Next summary mail off (Settings)" in page
    token = re.search(r'name="token" value="([^"]+)"', page).group(1)
    r = client.post("/do/mail-switch", data={"token": token, "next": "/settings", "name": "summary", "on": "1"},
                    follow_redirects=False)
    assert r.status_code == 303 and flipped == [("summary", True)]


def test_the_summary_and_alerts_are_scheduled():
    from app.celery_app import build_beat_schedule
    from app.config_loader import get_config

    schedule = build_beat_schedule(get_config())
    times = [t for t in get_config().app.summary_mail_times]
    assert sum(1 for k, v in schedule.items() if v["task"] == "m6.summary") == len(times)
    assert schedule["m6-alerts"]["task"] == "m6.alerts"


def test_a_reply_that_needs_you_is_under_needs_you(db_session, roles):
    db_session.add(Notification(kind="ping", section="needs_you", title="Web Co: a reply from Asha Rao needs your eyes",
                                detail="Unsure what they meant."))
    db_session.flush()
    s = M.build(db_session, now=NOW + timedelta(seconds=1), since=NOW - timedelta(seconds=1))
    needs = next(sec for sec in s.sections if sec.key == "needs_you")
    everything = [c.title for c in needs.cards] + [needs.note]
    assert any("needs your eyes" in t for t in everything) or needs.more > 0     # (your real items may come first)


def test_a_subject_never_carries_a_line_break():
    msg = M.message("me@gmail.com", "New fast-lane role: Data\nAnalyst · Web Co", "text", "<p>html</p>")
    assert msg["Subject"] == "New fast-lane role: Data Analyst · Web Co"


def test_a_role_with_a_draft_and_an_apply_link_is_one_card(db_session, roles):
    from app.models import Application, OutreachCampaign, OutreachDraft

    company, _data, manager = roles
    d = OutreachDraft(cluster_id=manager, version=1, scenario="fresh", role_name="Analytics Manager", subject="Hello",
                      body="{greeting}\n\nHi.", intro="i", points=[], req_ids=[], attach_resume=False, status="ready",
                      truth={}, inputs_version="x")
    db_session.add(d)
    db_session.flush()
    db_session.add(OutreachCampaign(cluster_id=manager, company_id=company.id, draft_id=d.id, draft_version=1,
                                    subject="Hello", body=d.body, status="pending_review"))
    db_session.add(Application(cluster_id=manager, company_id=company.id, status="queued", on_list=True,
                               queued_at=NOW))
    db_session.flush()
    cards, _older, _born = M._needs_you(db_session, NOW)          # all of them (the mail shows the top 5)
    mine = [c for c in cards if c.title.startswith("Analytics Manager · Web Co")]
    assert len(mine) == 1 and mine[0].chip == "to review · to apply"
    assert [label for label, _ in mine[0].buttons] == ["Review drafts →", "Open apply link →"]


def test_each_role_is_on_one_card_in_the_whole_mail(db_session, roles):
    from app.models import Application, JobScore, OutreachCampaign, OutreachDraft
    from sqlalchemy import select

    company, _data, manager = roles
    d = OutreachDraft(cluster_id=manager, version=1, scenario="fresh", role_name="Analytics Manager", subject="Hello",
                      body="{greeting}\n\nHi.", intro="i", points=[], req_ids=[], attach_resume=False, status="ready",
                      truth={}, inputs_version="x")
    db_session.add(d)
    db_session.flush()
    db_session.add(OutreachCampaign(cluster_id=manager, company_id=company.id, draft_id=d.id, draft_version=1,
                                    subject="Hello", body=d.body, status="pending_review"))
    db_session.add(Application(cluster_id=manager, company_id=company.id, status="queued", on_list=True, queued_at=NOW))
    db_session.add(Notification(kind="update", section="happened", title="Web Co: a new opening for this role",
                                subject_type="job_cluster", subject_id=str(manager)))
    db_session.scalars(select(JobScore).where(JobScore.cluster_id == manager)).one().owner_action = "act"
    db_session.flush()
    s = M.build(db_session, now=NOW + timedelta(seconds=1), since=NOW - timedelta(seconds=1))
    needs = next(sec for sec in s.sections if sec.key == "needs_you")
    mine = [c for c in needs.cards if c.role == manager]
    if not mine:
        pytest.skip("your real items fill the top of Needs you")
    _, html = M.render(s)
    assert html.count("Analytics Manager · Web Co") == 1                     # not again in New matches / Pursuing
    assert ("Web Co: a new opening for this role", None) in mine[0].lines    # its news is on that card
    pursuing = next((sec for sec in s.sections if sec.key == "pursuing"), None)
    assert pursuing is None or all(c.role != manager for c in pursuing.cards)  # (left out when nothing else is)
    assert s.counts["pursuing"] >= 1 and "pursuing" in s.subject


def test_one_set_of_words_for_where_a_role_stands():
    from types import SimpleNamespace as App

    from app.web.labels import in_play, role_chips

    assert role_chips(None, None, True) == [("to apply", "red")]
    assert role_chips(None, "pending_review", True) == [("to apply", "red"), ("to review", "red")]
    assert role_chips(App(status="applied"), "running", False) == [("outreach on", "green"), ("applied", "blue")]
    assert role_chips(App(status="interviewing"), None, False) == [("interviewing", "green")]
    ended = role_chips(App(status="silent"), "done", False)
    assert ended == [("ended · no reply", "gray")] and not in_play(ended)


def test_pursuing_tab_uses_the_same_chips(client, db_session, roles):
    from sqlalchemy import select

    from app.models import JobScore

    _company, data, _manager = roles
    db_session.scalars(select(JobScore).where(JobScore.cluster_id == data)).one().owner_action = "act"
    db_session.flush()
    page = client.get("/jobs?tab=pursuing").text
    card = page[page.index(f'id="c{data}"'):]
    assert '<span class="chip ch-red">to apply</span>' in card[:1500]
    assert '<span class="chip ch-red">to apply</span>' in client.get(f"/roles/{data}").text
