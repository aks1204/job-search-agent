"""Module 5 background job (daily): notice a new resume, remind about stale pay / notice period, and re-work
your career direction after your profile changed (its suggestions wait for your yes / no)."""

import logging

from app.celery_app import celery_app
from app.core.llm import LLM
from app.db import session_scope
from app.modules.m5_profile import direction
from app.modules.m5_profile.profile import check

log = logging.getLogger(__name__)


@celery_app.task(name="m5.check_profile")
def check_profile_task() -> dict:
    llm = LLM() if LLM.available() else None
    with session_scope() as s:
        result = check(s, llm)
    if llm is not None:
        with session_scope() as s:
            if direction.needs_refresh(s):
                result["direction"] = direction.refresh(s, llm)
    log.info("m5.check_profile: %s", result)
    return result
