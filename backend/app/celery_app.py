"""Celery worker + beat. Beat is what makes the agent run while nobody has the app open."""

from celery import Celery
from celery.schedules import crontab

from app.config_loader import LoadedConfig, get_config
from app.config_schemas import SCOUT_NAMES
from app.settings import get_settings


def build_beat_schedule(cfg: LoadedConfig) -> dict:
    schedule: dict = {
        "system-heartbeat": {
            "task": "system.heartbeat",
            "schedule": float(cfg.app.heartbeat_interval_seconds),
        },
        "m1-resolve-domains": {
            "task": "m1.resolve_domains",
            "schedule": cfg.scouts.resolver.interval_minutes * 60.0,
        },
    }
    for name in SCOUT_NAMES:
        scout = getattr(cfg.scouts, name)
        if not scout.enabled:
            continue
        s = scout.schedule
        schedule[f"m1-scout-{name}"] = {
            "task": "m1.run_scout",
            "schedule": crontab(minute=s.minute, hour=s.hour, day_of_month=s.day_of_month),
            "args": [name],
        }
    return schedule


_settings = get_settings()
_config = get_config()

celery_app = Celery(
    "job_agent",
    broker=_settings.redis_url,
    backend=_settings.redis_url,
    include=["app.tasks.system", "app.modules.m1_companies.tasks"],
)

celery_app.conf.update(
    timezone=_config.app.timezone,
    enable_utc=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    result_expires=3600,
    broker_connection_retry_on_startup=True,
    beat_schedule=build_beat_schedule(_config),
)
