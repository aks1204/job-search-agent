"""Module 5 background job: notice a new resume, remind about stale pay / notice period (daily)."""

import logging

from app.celery_app import celery_app
from app.core.llm import LLM
from app.db import session_scope
from app.modules.m5_profile.profile import check

log = logging.getLogger(__name__)


@celery_app.task(name="m5.check_profile")
def check_profile_task() -> dict:
    with session_scope() as s:
        result = check(s, LLM() if LLM.available() else None)
    log.info("m5.check_profile: %s", result)
    return result
