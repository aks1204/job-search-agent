"""Part 6b against the real database: the buttons do what they say, ask first when they should, can be undone,
and can't be pressed from another website. The Sending switch and "Run now" are faked here (they live in
Redis / the worker queue, which are your real ones), so a test never turns your sending on or starts a job."""

import re

import pytest
from sqlalchemy import select

from app.models import Company, JobFeedback, JobScore, MemoryEntry, Notification
from tests.test_m6_web_integration import client, roles  # noqa: F401  (same fixtures)

pytestmark = pytest.mark.integration


def token_of(client) -> str:
    page = client.get("/jobs").text
    return re.search(r'name="token" value="([^"]+)"', page).group(1)


def press(client, url: str, **fields):
    return client.post(url, data={"token": token_of(client), "next": "/jobs", **fields}, follow_redirects=False)


# --- the safety guards -------------------------------------------------------------------------------

def test_a_button_pressed_from_another_site_is_refused(client, roles):
    _, data, _ = roles
    token = token_of(client)
    r = client.post(f"/do/feedback/{data}", data={"token": token, "action": "skip"},
                    headers={"Origin": "https://evil.example"}, follow_redirects=False)
    assert r.status_code == 403
    r = client.post(f"/do/feedback/{data}", data={"token": token, "action": "skip"},
                    headers={"Referer": "https://evil.example/page"}, follow_redirects=False)
    assert r.status_code == 403
    r = client.post("/api/scouts/nope/run", headers={"Origin": "https://evil.example"})
    assert r.status_code == 403                                     # the API too (a 404 would mean it got through)


def test_only_this_laptop_is_answered(client):
    assert client.get("/jobs", headers={"Host": "evil.example"}).status_code == 400   # DNS rebinding
    assert client.get("/jobs", headers={"Host": "localhost:8000"}).status_code == 200


def test_a_press_without_the_pages_code_is_refused(client, db_session, roles):
    _, data, _ = roles
    token_of(client)                                                # the browser has its cookie…
    for data_ in ({"action": "skip"}, {"token": "guessed", "action": "skip"}):
        r = client.post(f"/do/feedback/{data}", data=data_, follow_redirects=False)
        assert r.status_code == 303                                 # back to the page, told why
    assert "this page was old" in client.get("/jobs").text
    score = db_session.scalars(select(JobScore).where(JobScore.cluster_id == data)).one()
    assert score.owner_action is None                               # …and nothing changed


def test_after_a_button_it_only_goes_back_to_this_app(client, roles):
    _, data, _ = roles
    r = client.post(f"/do/feedback/{data}", data={"token": token_of(client), "action": "thumbs_up",
                                                  "next": "https://evil.example/"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/jobs"


# --- Jobs: skip with reasons, and Undo --------------------------------------------------------------

def test_skip_with_reasons_then_undo(client, db_session, roles):
    _, data, _ = roles
    r = press(client, f"/do/feedback/{data}", action="skip", chips=["too_junior", "location"])
    assert r.status_code == 303
    score = db_session.scalars(select(JobScore).where(JobScore.cluster_id == data)).one()
    assert score.owner_action == "skip" and score.band == "archive"
    fb = db_session.scalars(select(JobFeedback).where(JobFeedback.cluster_id == data)).one()
    assert fb.chips == ["too_junior", "location"]
    page = client.get("/jobs").text
    assert "Skipped this role" in page and f'action="/do/feedback-undo/{fb.id}"' in page
    assert "Skipped this role" not in client.get("/jobs").text      # the line shows once
    press(client, f"/do/feedback-undo/{fb.id}")
    db_session.expire_all()
    score = db_session.scalars(select(JobScore).where(JobScore.cluster_id == data)).one()
    assert score.owner_action is None and score.band != "archive"     # back on your lists
    assert db_session.scalars(select(JobFeedback).where(JobFeedback.cluster_id == data)).all() == []
    precedents = db_session.scalars(select(MemoryEntry).where(
        MemoryEntry.kind == "precedent", MemoryEntry.context["cluster_id"].astext == str(data))).all()
    assert precedents and all(p.status != "active" for p in precedents)   # what memory learned is undone too


def test_undo_only_takes_back_the_latest_decision(client, db_session, roles):
    _, data, _ = roles
    press(client, f"/do/feedback/{data}", action="act")
    press(client, f"/do/feedback/{data}", action="skip")
    first, second = db_session.scalars(select(JobFeedback).where(JobFeedback.cluster_id == data)
                                       .order_by(JobFeedback.id)).all()
    press(client, f"/do/feedback-undo/{first.id}")                   # an older one: refused, said why
    assert "decided on this role again since" in client.get("/jobs").text
    press(client, f"/do/feedback-undo/{second.id}")
    db_session.expire_all()
    score = db_session.scalars(select(JobScore).where(JobScore.cluster_id == data)).one()
    assert score.owner_action == "act"                               # your earlier decision stands again


# --- confirm steps -----------------------------------------------------------------------------------

def test_a_permanent_reject_asks_first(client, db_session, roles):
    company, _, _ = roles
    r = press(client, f"/do/verdict/{company.id}", decision="reject", reason="not for me", permanent="1")
    assert r.status_code == 200 and "Reject Web Co for good?" in r.text and 'name="confirmed" value="1"' in r.text
    db_session.expire_all()
    assert db_session.get(Company, company.id).status != "blocked"   # nothing yet
    press(client, f"/do/verdict/{company.id}", decision="reject", reason="not for me", permanent="1", confirmed="1")
    db_session.expire_all()
    assert db_session.get(Company, company.id).status in ("blocked", "rejected")


def test_turning_sending_on_asks_first_and_off_does_not(client, monkeypatch):
    from app.modules.m3_outreach import sender

    state = {"paused": True}
    monkeypatch.setattr(sender, "paused", lambda: state["paused"])
    monkeypatch.setattr(sender, "set_paused", lambda on: state.update(paused=on))
    r = press(client, "/do/sending", on="1")
    assert r.status_code == 200 and "Turn sending on?" in r.text and state["paused"] is True
    press(client, "/do/sending", on="1", confirmed="1")
    assert state["paused"] is False
    press(client, "/do/sending", on="0")                              # off: at once, no question
    assert state["paused"] is True
    assert "Turn on" in client.get("/settings").text


def test_forgetting_a_memory_asks_first_and_can_be_undone(client, db_session):
    from app.modules.m5_profile.memory import record_answer

    e = record_answer(db_session, "Do you want m6 button tests?", "yes", module="m5.profile")
    db_session.flush()
    assert "Forget this memory?" in press(client, f"/do/memory/{e.id}", action="forget").text
    press(client, f"/do/memory/{e.id}", action="forget", confirmed="1")
    assert e.status == "archived"
    page = client.get("/memory").text
    undo = re.search(r'action="(/do/memory/\d+\?undo=restore)"', page).group(1)
    press(client, undo)
    assert e.status == "active"


# --- background runs ---------------------------------------------------------------------------------

def test_run_now_queues_once_and_pings_when_done(client, db_session, monkeypatch):
    from app.web import runs

    locks, sent = set(), []

    class FakeRedis:
        def set(self, key, value, nx=False, ex=None):
            if nx and key in locks:
                return False
            locks.add(key)
            return True

        def delete(self, key):
            locks.discard(key)

    monkeypatch.setattr("app.core.redis_client.get_redis", lambda: FakeRedis())
    monkeypatch.setattr(runs.celery_app, "send_task", lambda name, args: sent.append((name, args)))
    press(client, "/do/run/sync")
    assert sent == [("web.run_now", ["sync", None])] and "Started: Read job feeds" in client.get("/jobs").text
    press(client, "/do/run/sync")
    assert len(sent) == 1 and "Already running" in client.get("/jobs").text
    assert press(client, "/do/run/nope").status_code == 303 and "Not done" in client.get("/jobs").text

    monkeypatch.setitem(runs.JOBS, "sync", ("Read job feeds", lambda arg: {"read": 3, "failed": 0}))
    monkeypatch.setattr("app.db.session_scope", _scope_of(db_session))
    runs.run_now("sync")
    assert not locks                                                  # free again
    n = db_session.scalars(select(Notification).where(Notification.subject_type == "run")
                           .order_by(Notification.id.desc())).first()
    assert n.title == "Done: Read job feeds" and n.detail == "read 3"
    assert "Done: Read job feeds" in client.get("/pings").text


def _scope_of(session):
    from contextlib import contextmanager

    @contextmanager
    def scope():
        yield session
    return scope


# --- pings, screens, privacy ------------------------------------------------------------------------

def test_pings_can_be_read(client, db_session):
    from app.core.notify import notify

    n = notify(db_session, "update", "happened", "m6 test ping")
    db_session.flush()
    assert "m6 test ping" in client.get("/pings").text
    press(client, f"/do/ping-read/{n.id}")
    assert n.read_at is not None and "m6 test ping" not in client.get("/pings").text


def test_every_screen_has_its_buttons(client, roles):
    company, data, _ = roles
    assert 'action="/do/feedback/' in client.get("/jobs?tab=fast").text
    role = client.get(f"/roles/{data}").text
    assert f'action="/do/applied/{data}"' in role and 'action="/do/run/drafts"' in role
    co = client.get(f"/companies/{company.id}").text
    assert f'action="/do/verdict/{company.id}"' in co and f'action="/do/fix-website/{company.id}"' in co
    assert 'action="/do/run/process"' in client.get("/maintenance").text
    assert client.get("/pings").status_code == 200


def test_the_role_list_never_carries_your_pay(client, db_session, roles):
    from app.models import JobAssessment

    _, data, _ = roles
    a = db_session.scalars(select(JobAssessment).where(JobAssessment.cluster_id == data)).one()
    a.comp = {"verdict": "above", "mid_inr": 4000000, "current_inr": 9876543, "ratio": 1.4}
    db_session.flush()
    rows = client.get("/api/shortlist?band=fast_lane").json()
    mine = [r for r in rows if r["cluster_id"] == data][0]
    assert "current_inr" not in mine["comp"] and "ratio" not in mine["comp"] and mine["comp"]["mid_inr"] == 4000000


def test_each_scout_button_runs_its_own_scout(monkeypatch):
    from app.config_schemas import SCOUT_NAMES
    from app.web import runs

    ran = []
    monkeypatch.setattr(runs, "_scout", lambda name: ran.append(name) or {})
    for name in SCOUT_NAMES:
        runs.JOBS[f"scout:{name}"][1](None)                       # the Run-now button passes no target
    assert ran == list(SCOUT_NAMES)
    with pytest.raises(ValueError):
        runs.start("sync", "5")                                    # only some jobs can be aimed at one item
