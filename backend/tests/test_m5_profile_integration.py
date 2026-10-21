"""Module 5a against the real database, with a fake AI reading a fake resume (rolled back afterwards)."""

import pytest
from sqlalchemy import select

from app.core.llm import LLMResult
from app.models import Notification, OwnerProfile
from app.modules.m5_profile import profile as P
from app.modules.m5_profile import store

pytestmark = pytest.mark.integration


class FakeLLM:
    def __init__(self):
        self.calls = 0

    def structured(self, tier, *, system, user, schema, purpose, session=None, subject=None):
        self.calls += 1
        assert purpose == "m5.read_resume" and "RESUME TEXT" in user
        parsed = P.ResumeRead(name="Test Person", location="Pune, India", mobile="", linkedin_url="",
                              career_field="analytics", total_experience_years=4.0, skills=["SQL"],
                              jobs=[P.ResumeJob(company="Testco", title="Senior Analyst", start="2022-05", end="")])
        return LLMResult(parsed=parsed, model="fake", cost_usd=0.02, input_tokens=1, output_tokens=1)


@pytest.fixture(autouse=True)
def fake_resume(monkeypatch):
    monkeypatch.setattr(P, "resume_text", lambda: "RESUME TEXT")
    yield
    store.invalidate()


def test_read_then_confirm_then_everything_uses_it(db_session):
    llm = FakeLLM()
    draft = P.make_draft(db_session, llm)
    assert draft.status == "draft" and draft.data["current_role"]["company"] == "Testco" and llm.calls == 1
    assert store.load_confirmed(db_session) is None or store.load_confirmed(db_session).id != draft.id  # not yet used
    data = dict(draft.data)
    data.setdefault("level_ladder", ["Analyst", "Senior Analyst", "Manager"])
    data.setdefault("current_level", "Senior Analyst")
    data["notice_period_days"] = 30
    row = P.confirm(db_session, data)
    assert row.status == "confirmed" and row.id == draft.id
    assert store.load_confirmed(db_session).id == row.id
    assert store.as_owner(row).current_role.company == "Testco"
    assert "notice_period_days" in row.confirmed_fields                          # you just confirmed it
    assert db_session.scalars(select(OwnerProfile).where(OwnerProfile.status == "confirmed")).all() == [row]


def test_a_draft_is_never_used_until_you_confirm_and_invalid_data_is_refused(db_session):
    P.make_draft(db_session, FakeLLM())
    with pytest.raises(Exception):
        P.confirm(db_session, {"name": "Only a name"})                           # missing required fields
    assert store.latest_draft(db_session) is not None


def test_daily_check_asks_once_for_a_new_resume_and_reminds_about_stale_pay(db_session, monkeypatch):
    monkeypatch.setattr(P, "file_hash", lambda path: "hash-new")
    out = P.check(db_session, FakeLLM())
    assert "draft" in out
    assert P.check(db_session, FakeLLM()).get("draft") is None                   # waiting for you: not re-read
    ask = db_session.scalars(select(Notification).where(Notification.subject_type == "profile",
                                                        Notification.subject_id == "draft")).all()
    assert len(ask) >= 1 and ask[0].kind == "needs_you"


def test_an_unreadable_resume_is_reported_once_not_failed_daily(db_session, monkeypatch):
    monkeypatch.setattr(P, "file_hash", lambda path: "hash-scanned")
    monkeypatch.setattr(P, "resume_text", lambda: "")              # a scanned PDF: no text
    assert "unreadable" in P.check(db_session, FakeLLM())
    P.check(db_session, FakeLLM())
    told = db_session.scalars(select(Notification).where(Notification.subject_type == "profile",
                                                         Notification.subject_id == "unreadable")).all()
    assert len(told) == 1


def test_store_keeps_the_last_good_profile_through_a_database_blip(monkeypatch):
    from app.config_schemas import OwnerStub

    good = OwnerStub.model_validate({"name": "X", "career_field": "a",
                                     "current_role": {"title": "t", "company": "c", "started": "2023-01"},
                                     "employment_history": [{"company": "c", "title": "t", "start": "2023-01"}]})
    store._cache.update(at=0.0, value=good)
    import app.db

    def boom():
        raise RuntimeError("database down")

    monkeypatch.setattr(app.db, "session_scope", boom)
    assert store.current_profile() is good
