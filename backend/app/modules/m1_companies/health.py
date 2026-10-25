"""Per-scout health, shown in the dashboard and `python -m app.cli stats`."""

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config_loader import get_config
from app.models.scout import ScoutRun
from app.modules.m1_companies.scouts import SCOUTS

RECENT_RUNS = 10


def scout_health(session: Session) -> list[dict]:
    cfg = get_config()
    report = []
    for name, cls in SCOUTS.items():
        scout = cls(cfg)
        runs = session.scalars(
            select(ScoutRun).where(ScoutRun.scout == name).order_by(ScoutRun.started_at.desc()).limit(RECENT_RUNS)
        ).all()
        finished = [r for r in runs if r.status != "running"]
        failures = 0
        for r in finished:
            if r.status != "failed":
                break
            failures += 1

        reason = scout.not_ready_reason()
        if not scout.settings.enabled:
            health = "disabled"
        elif reason:
            health = "not_configured"
        elif not finished:
            health = "never_run"
        elif failures:
            health = "failing"
        else:
            health = "ok"

        last = finished[0] if finished else None
        successful = session.scalar(select(func.count()).select_from(ScoutRun).where(
            ScoutRun.scout == name, ScoutRun.status == "ok")) or 0
        report.append({
            "scout": name,
            "health": health,
            "reason": reason if health == "not_configured" else None,
            "consecutive_failures": failures,
            "successful_runs": successful,
            "last_run": None if last is None else {
                "status": last.status,
                "started_at": last.started_at,
                "finished_at": last.finished_at,
                "counts": last.counts,
                "error": last.error,
            },
        })
    return report
