"""Module 4, no database: status rules, link checks, title matching words."""

from datetime import datetime, timezone
from types import SimpleNamespace

from app.modules.m4_apply import inbox as I
from app.modules.m4_apply import tracker as T


class FakeSession:
    def __init__(self):
        self.added = []

    def add(self, obj):
        self.added.append(obj)


def app(status):
    return SimpleNamespace(id=1, status=status, applied_at=None, applied_how=None, status_at=None, last_heard_at=None)


def test_statuses_only_move_forward():
    s, a = FakeSession(), app("queued")
    assert T.set_status(s, a, "applied", source="you") and a.applied_how == "you"
    assert T.set_status(s, a, "confirmed", source="email")
    assert not T.set_status(s, a, "applied", source="you")          # never backwards
    assert T.set_status(s, a, "interviewing", source="email")
    assert T.set_status(s, a, "rejected", source="email")
    assert not T.set_status(s, a, "interviewing", source="email")   # rejected is final
    assert [e.kind for e in s.added] == ["applied", "confirmed", "interviewing", "rejected"]


def test_a_receipt_on_a_queued_role_means_you_applied():
    s, a = FakeSession(), app("queued")
    at = datetime(2026, 11, 4, 10, tzinfo=timezone.utc)
    assert T.set_status(s, a, "confirmed", source="email", at=at)
    assert a.applied_at == at and a.applied_how == "email" and [e.kind for e in s.added] == ["applied", "confirmed"]


def test_closing_only_affects_roles_not_yet_applied_and_silence_can_be_revived():
    s = FakeSession()
    applied = app("applied")
    assert not T.set_status(s, applied, "closed", source="agent")
    queued = app("queued")
    assert T.set_status(s, queued, "closed", source="agent")
    silent = app("silent")
    assert T.set_status(s, silent, "interviewing", source="email")  # news arrived after all


class FakeHTTP:
    def __init__(self, code, text=""):
        self.code, self.text = code, text

    def get(self, url, **kw):
        return SimpleNamespace(status_code=self.code, text=self.text)


def test_link_check():
    assert T.check_link(FakeHTTP(200, "<h1>Data Analyst</h1> Apply now"), "u") == (True, "live")
    assert T.check_link(FakeHTTP(404), "u")[0] is False
    assert T.check_link(FakeHTTP(200, "Sorry, this job is no longer accepting applications"), "u")[0] is False
    assert T.check_link(FakeHTTP(403), "u")[0] is None                 # can't tell: still shown


def test_title_words():
    assert I._tokens("Senior Data Analyst – Commercial") == {"senior", "data", "analyst", "commercial"}


def test_any_news_counts_against_silence_even_without_a_status_change():
    s, a = FakeSession(), app("in_review")
    later = datetime(2026, 12, 1, tzinfo=timezone.utc)
    assert not T.set_status(s, a, "in_review", source="email", at=later)    # an assessment email: same status
    assert a.last_heard_at == later


def test_applying_after_the_posting_closed_or_after_skipping_still_counts():
    s = FakeSession()
    closed = app("closed")
    assert T.set_status(s, closed, "applied", source="you")
    skipped = app("skipped")
    assert T.set_status(s, skipped, "confirmed", source="email")            # the receipt shows you applied
    assert [e.kind for e in s.added][-2:] == ["applied", "confirmed"]
    assert not T.set_status(s, app("applied"), "closed", source="agent")   # but closing never undoes applying
