"""Module 3 background jobs: contacts + drafts every watch tick, freshness sweep weekly. Nothing sends."""

import logging

from app.celery_app import celery_app
from app.modules.m3_outreach.drafting import run_drafts
from app.modules.m3_outreach.pipeline import run_contacts, sweep

log = logging.getLogger(__name__)


@celery_app.task(name="m3.contacts")
def contacts_task() -> dict:
    result = {f"contacts_{k}": v for k, v in run_contacts().items()}
    try:
        result.update({f"drafts_{k}": v for k, v in run_drafts().items()})
    except Exception as e:  # e.g. a broken template file: contacts still count, drafts wait for the fix
        log.warning("m3 drafts skipped: %r", e)
        result["drafts_error"] = str(e)[:200]
    log.info("m3.contacts: %s", result)
    return result


@celery_app.task(name="m3.sweep")
def sweep_task() -> dict:
    result = sweep()
    log.info("m3.sweep: %s", result)
    return result
