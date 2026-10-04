from functools import lru_cache

import redis

from app.settings import get_settings


@lru_cache(maxsize=1)
def get_redis() -> redis.Redis:
    return redis.Redis.from_url(
        get_settings().redis_url,
        socket_connect_timeout=2,
        socket_timeout=2,
        decode_responses=True,
    )
