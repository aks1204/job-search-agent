"""Monthly free-tier budgets, counted in Redis (feeds the Settings screen's quota bars)."""

from datetime import datetime, timezone

from app.core.redis_client import get_redis


def _key(service: str) -> str:
    return f"quota:{service}:{datetime.now(timezone.utc):%Y-%m}"


def try_consume(service: str, monthly_limit: int) -> bool:
    """Take one unit of this month's budget; False (and nothing taken) when exhausted."""
    r = get_redis()
    key = _key(service)
    used = r.incr(key)
    if used == 1:
        r.expire(key, 40 * 86400)
    if used > monthly_limit:
        r.decr(key)
        return False
    return True


def used_this_month(service: str) -> int:
    return int(get_redis().get(_key(service)) or 0)
