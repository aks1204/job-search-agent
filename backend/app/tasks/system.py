import socket
from datetime import datetime, timezone

from app.celery_app import celery_app
from app.config_loader import get_config
from app.constants import HEARTBEAT_KEY
from app.core.redis_client import get_redis


@celery_app.task(name="system.heartbeat")
def heartbeat() -> str:
    """Scheduled by beat, executed by a worker: proves the whole unattended chain is alive."""
    now = datetime.now(timezone.utc).isoformat()
    ttl = get_config().app.heartbeat_interval_seconds * 5
    get_redis().set(HEARTBEAT_KEY, now, ex=ttl)
    return now


@celery_app.task(name="system.ping")
def ping() -> str:
    return f"pong from {socket.gethostname()}"
