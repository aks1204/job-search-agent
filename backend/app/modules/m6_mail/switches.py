"""The on/off switches for the summary mail and each alert (owner 2026-11-04: all off until you turn them on).

Kept in Redis like the Sending switch; the value is when it was turned on, so an alert never brings up what
happened before you switched it on. If Redis can't be reached, everything counts as off (nothing is sent)."""

from datetime import datetime, timezone

from app.core.redis_client import get_redis

SWITCHES = {"summary": "Summary mail (twice a day)", "fast": "Email alert: a fast-lane find",
            "reply": "Email alert: a reply to your outreach"}
KEY = "mail:on:{}"


def on_since(name: str) -> datetime | None:
    """When the switch was turned on; None when it's off (or Redis is down)."""
    try:
        value = get_redis().get(KEY.format(name))
    except Exception:
        return None
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return datetime.now(timezone.utc)


def is_on(name: str) -> bool:
    return on_since(name) is not None


def set_on(name: str, on: bool) -> None:
    if name not in SWITCHES:
        raise ValueError(f"no such switch {name!r}")
    if on:
        get_redis().set(KEY.format(name), datetime.now(timezone.utc).isoformat(), nx=True)   # already on: kept
    else:
        get_redis().delete(KEY.format(name))
