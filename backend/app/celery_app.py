"""Celery worker + beat. Beat is what makes the agent run while nobody has the app open."""

from celery import Celery

from app.config_loader import get_config
from app.settings import get_settings

_settings = get_settings()
_config = get_config()

celery_app = Celery(
    "job_agent",
    broker=_settings.redis_url,
    backend=_settings.redis_url,
    include=["app.tasks.system"],
)

celery_app.conf.update(
    timezone=_config.app.timezone,
    enable_utc=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    result_expires=3600,
    broker_connection_retry_on_startup=True,
    beat_schedule={
        "system-heartbeat": {
            "task": "system.heartbeat",
            "schedule": float(_config.app.heartbeat_interval_seconds),
        },
    },
)
