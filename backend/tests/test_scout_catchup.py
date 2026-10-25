"""Catch-up for sources whose scheduled run was missed while the laptop was off."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.models.scout import ScoutRun
from app.modules.m1_companies.catchup import due, last_scheduled

IST = ZoneInfo("Asia/Kolkata")


def _at(y, mo, d, h, mi) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=IST).astimezone(timezone.utc)


def test_the_last_scheduled_time_daily_and_twice_a_month():
    daily = SimpleNamespace(minute="30", hour="5", day_of_month="*")
    assert last_scheduled(daily, _at(2026, 10, 26, 9, 0), IST) == _at(2026, 10, 26, 5, 30)    # this morning
    assert last_scheduled(daily, _at(2026, 10, 26, 5, 0), IST) == _at(2026, 10, 25, 5, 30)    # not yet: yesterday
    hn = SimpleNamespace(minute="0", hour="7", day_of_month="2,16")
    assert last_scheduled(hn, _at(2026, 10, 26, 9, 0), IST) == _at(2026, 10, 16, 7, 0)
    assert last_scheduled(hn, _at(2026, 11, 1, 9, 0), IST) == _at(2026, 10, 16, 7, 0)


@pytest.mark.integration
def test_a_source_that_missed_its_run_is_due_until_it_has_run(db_session):
    # A fixed day far in the future, so your real runs can't count as "since the last scheduled time".
    now = _at(2028, 10, 26, 9, 0)                                 # last scheduled HN run: 16 Oct 2028, 07:00
    assert "hn" in due(db_session, now)
    from app.config_loader import get_config

    fired = last_scheduled(get_config().scouts.hn.schedule, now, IST)
    db_session.add(ScoutRun(scout="hn", status="failed", started_at=now - timedelta(minutes=30)))
    db_session.flush()
    assert "hn" not in due(db_session, now)                       # failed just now: not retried for 2 hours
    db_session.add(ScoutRun(scout="hn", status="ok", started_at=fired + timedelta(minutes=1)))
    db_session.flush()
    assert "hn" not in due(db_session, now)
    assert "hn" not in due(db_session, now + timedelta(hours=3))  # ran since its scheduled time
