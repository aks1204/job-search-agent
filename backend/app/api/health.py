from datetime import datetime, timezone

from fastapi import APIRouter, Response
from sqlalchemy import text

from app.config_loader import get_config
from app.constants import HEARTBEAT_KEY
from app.core.redis_client import get_redis
from app.db import get_engine
from app.settings import get_settings

router = APIRouter()


def check_database() -> dict:
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
            pgvector = conn.execute(
                text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
            ).scalar()
            migration = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
    except Exception as e:
        return {"ok": False, "error": (str(e) or type(e).__name__).splitlines()[0]}
    return {"ok": pgvector is not None, "pgvector": pgvector, "migration": migration}


def check_redis() -> dict:
    try:
        get_redis().ping()
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True}


def check_scheduler() -> dict:
    """Beat schedules a heartbeat, a worker executes it. A fresh heartbeat proves both."""
    try:
        raw = get_redis().get(HEARTBEAT_KEY)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    if raw is None:
        return {"ok": False, "error": "no heartbeat yet (worker or beat not running?)"}
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(raw)).total_seconds()
    limit = get_config().app.heartbeat_interval_seconds * 3
    return {"ok": age <= limit, "last_heartbeat": raw, "age_seconds": round(age, 1)}


def check_config() -> dict:
    cfg = get_config()
    return {
        "ok": True,
        "version": cfg.version,
        "files": {name: f.version for name, f in cfg.files.items()},
    }


@router.get("/health")
def health(response: Response) -> dict:
    checks = {
        "database": check_database(),
        "redis": check_redis(),
        "scheduler": check_scheduler(),
        "config": check_config(),
        "anthropic_key": {"ok": bool(get_settings().anthropic_api_key)},
    }
    if not checks["database"]["ok"]:
        status = "down"
        response.status_code = 503
    elif all(c["ok"] for c in checks.values()):
        status = "ok"
    else:
        status = "degraded"
    return {"status": status, "checks": checks}
