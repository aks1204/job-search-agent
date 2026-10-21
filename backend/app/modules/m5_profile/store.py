"""Where the running app gets "who the agent works for": your latest confirmed profile.

`get_config().owner` calls `current_profile()`. It is cached for a short while per process, so a confirm
reaches every worker within `TTL_SECONDS` without a database hit on every use. Anything going wrong (no
database yet, the table not migrated) quietly means "no profile": the app falls back to owner_stub.yaml.
"""

import logging
import time

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config_schemas import OwnerStub
from app.models.profile import OwnerProfile

log = logging.getLogger(__name__)

TTL_SECONDS = 30
_cache: dict = {"at": 0.0, "value": None}


def load_confirmed(session: Session) -> OwnerProfile | None:
    return session.scalar(select(OwnerProfile).where(OwnerProfile.status == "confirmed")
                          .order_by(OwnerProfile.id.desc()))


def latest_draft(session: Session) -> OwnerProfile | None:
    return session.scalar(select(OwnerProfile).where(OwnerProfile.status == "draft").order_by(OwnerProfile.id.desc()))


def as_owner(row: OwnerProfile | None) -> OwnerStub | None:
    if row is None:
        return None
    try:
        return OwnerStub.model_validate(row.data)
    except Exception as e:  # validated at confirm time, so this means a schema change: fall back, say why
        log.warning("confirmed profile %s no longer validates (%s); using owner_stub.yaml", row.id, e)
        return None


def current_profile() -> OwnerStub | None:
    now = time.monotonic()
    if now - _cache["at"] < TTL_SECONDS:
        return _cache["value"]
    value = None
    try:
        from app.db import session_scope

        with session_scope() as s:
            value = as_owner(load_confirmed(s))
    except Exception as e:  # no database / not migrated yet / a blip: keep what we had, don't flip to the file
        log.debug("profile lookup unavailable: %s", e)
        value = _cache["value"]
    _cache.update(at=now, value=value)
    return value


def invalidate() -> None:
    _cache.update(at=0.0, value=None)
