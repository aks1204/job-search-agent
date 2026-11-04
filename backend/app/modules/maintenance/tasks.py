"""Weekly source self-check (Mondays): a note for each new warning — saved for `pings` now, and shown in the
summary mail's Housekeeping line once Module 6 is built."""

import logging

from app.celery_app import celery_app
from app.db import session_scope
from app.modules.maintenance.sources import weekly_check

log = logging.getLogger(__name__)


@celery_app.task(name="maintenance.source_check")
def source_check_task() -> dict:
    with session_scope() as s:
        result = weekly_check(s)
    log.info("maintenance.source_check: %s", result)
    return result


@celery_app.task(name="maintenance.audit_compact")
def audit_compact_task() -> dict:
    """Part 6e: Audit log entries over 12 months old → one line per kind per month."""
    from app.modules.maintenance.audit_compact import compact

    with session_scope() as s:
        result = compact(s)
    log.info("maintenance.audit_compact: %s", result)
    return result
