"""Module 4 background job: apply list, link checks, the narrow inbox scan and silence, every 30 minutes."""

import logging

from app.celery_app import celery_app
from app.modules.m4_apply.tracker import run_tracking

log = logging.getLogger(__name__)


@celery_app.task(name="m4.track")
def track_task() -> dict:
    result = run_tracking()
    log.info("m4.track: %s", result)
    return result
