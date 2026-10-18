"""Module 3 background jobs. Contacts → drafts → campaigns for your review every watch tick; the
sender every 5 minutes (approved emails only); reply/bounce watch every 15 minutes; weekly sweep."""

import logging

from app.celery_app import celery_app
from app.db import session_scope
from app.modules.m3_outreach.campaigns import prepare_campaigns
from app.modules.m3_outreach.drafting import run_drafts
from app.modules.m3_outreach.pipeline import run_contacts, sweep
from app.modules.m3_outreach.sender import mail_check, send_tick

log = logging.getLogger(__name__)


@celery_app.task(name="m3.contacts")
def contacts_task() -> dict:
    result = {f"contacts_{k}": v for k, v in run_contacts().items()}
    try:
        result.update({f"drafts_{k}": v for k, v in run_drafts().items()})
        with session_scope() as s:
            result.update({f"campaigns_{k}": v for k, v in prepare_campaigns(s).items()})
    except Exception as e:  # e.g. a broken template file: contacts still count, drafts wait for the fix
        log.warning("m3 drafts/campaigns skipped: %r", e)
        result["drafts_error"] = str(e)[:200]
    log.info("m3.contacts: %s", result)
    return result


@celery_app.task(name="m3.send")
def send_task() -> dict:
    result = send_tick()
    if "sent" in result or "error" in result:
        log.info("m3.send: %s", result)
    return result


@celery_app.task(name="m3.mail_check")
def mail_check_task() -> dict:
    result = mail_check()
    log.info("m3.mail_check: %s", result)
    return result


@celery_app.task(name="m3.sweep")
def sweep_task() -> dict:
    result = sweep()
    log.info("m3.sweep: %s", result)
    return result
