"""Catch-up for sources whose scheduled run was missed (owner request, 2026-10-26).

Each source runs at a fixed time (YC 05:30, Adzuna 06:00, Hacker News on the 2nd and 16th at 07:00…). On a
laptop that's off at that moment the run is simply skipped — nothing catches up — so a source could go weeks
without running. Every 15 minutes this checks each switched-on source: if it has had no successful run since
its last scheduled time, it runs it once now. Only when the internet is reachable (a laptop just waking up
isn't online yet), and a failed attempt isn't retried for 2 hours.
"""

import logging
import socket
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config_loader import get_config
from app.models.scout import ScoutRun

log = logging.getLogger(__name__)

RETRY_AFTER = timedelta(hours=2)
GRACE = timedelta(minutes=10)         # after the scheduled time, before catching up
STUCK_AFTER = timedelta(hours=3)      # a run still "running" this long ago was cut off (laptop shut)
LOOKBACK_DAYS = 40            # enough for a twice-monthly source


def _values(field, low: int, high: int) -> list[int]:
    """Cron field ('5', '2,16', '*', 7) → the values it allows."""
    text = str(field).strip()
    if text == "*":
        return list(range(low, high + 1))
    return sorted({int(v) for v in text.split(",") if v.strip().isdigit()})


def last_scheduled(schedule, now: datetime, tz: ZoneInfo) -> datetime | None:
    """The most recent time (UTC) the schedule should have fired at or before `now`."""
    local_now = now.astimezone(tz)
    hours, minutes = _values(schedule.hour, 0, 23), _values(schedule.minute, 0, 59)
    days = _values(schedule.day_of_month, 1, 31)
    for back in range(LOOKBACK_DAYS + 1):
        day: date = local_now.date() - timedelta(days=back)
        if day.day not in days:
            continue
        for h in reversed(hours):
            for m in reversed(minutes):
                at = datetime.combine(day, time(h, m), tzinfo=tz)
                if at <= local_now:
                    return at.astimezone(timezone.utc)
    return None


def online(host: str = "hacker-news.firebaseio.com") -> bool:
    try:
        socket.getaddrinfo(host, 443)
        return True
    except OSError:
        return False


def due(session: Session, now: datetime | None = None) -> list[str]:
    """Switched-on, set-up sources that have missed their last scheduled run."""
    from app.modules.m1_companies.scouts import SCOUTS

    cfg = get_config()
    now = now or datetime.now(timezone.utc)
    tz = ZoneInfo(cfg.app.timezone)
    out = []
    for name, cls in SCOUTS.items():
        scout = cls(cfg)
        if not scout.settings.enabled or scout.not_ready_reason():
            continue
        fired = last_scheduled(scout.settings.schedule, now, tz)
        if fired is None or now - fired < GRACE:
            continue                                  # (the normal scheduled run gets its chance first)
        runs = session.scalars(select(ScoutRun).where(ScoutRun.scout == name, ScoutRun.started_at >= fired)
                               .order_by(ScoutRun.started_at.desc())).all()
        if any(r.status == "ok" or (r.status == "running" and now - r.started_at < STUCK_AFTER) for r in runs):
            continue                                  # done (or under way) since its scheduled time
        if runs and now - runs[0].started_at < RETRY_AFTER:
            continue                                  # tried recently and failed: not again yet
        out.append(name)
    return out


def run_catchup() -> dict:
    from app.db import session_scope
    from app.modules.m1_companies.tasks import run_scout_task

    with session_scope() as s:
        names = due(s)
    if not names:
        return {"due": []}
    if not online():
        log.info("scout catch-up: %s due, but offline — next check", names)
        return {"due": names, "offline": True}
    for name in names:
        run_scout_task.delay(name)
    log.info("scout catch-up: running %s", names)
    return {"due": names, "started": names}
