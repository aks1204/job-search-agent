"""Module 3 background jobs: contact discovery every watch tick, freshness sweep weekly."""

import logging

from app.celery_app import celery_app
from app.modules.m3_outreach.pipeline import run_contacts, sweep

log = logging.getLogger(__name__)


@celery_app.task(name="m3.contacts")
def contacts_task() -> dict:
    result = run_contacts()
    log.info("m3.contacts: %s", result)
    return result


@celery_app.task(name="m3.sweep")
def sweep_task() -> dict:
    result = sweep()
    log.info("m3.sweep: %s", result)
    return result
