"""Module 5 background job (daily): notice a new resume, remind about stale pay / notice period, re-work your
career direction after your profile changed, and look after memory (patterns, ageing, clashes with a new
resume). Every change to what the agent looks for still waits for your yes."""

import logging

from app.celery_app import celery_app
from app.core.llm import LLM
from app.db import session_scope
from app.modules.m5_profile import direction, memory
from app.modules.m5_profile.profile import check

log = logging.getLogger(__name__)


@celery_app.task(name="m5.check_profile")
def check_profile_task() -> dict:
    llm = LLM() if LLM.available() else None
    try:  # first, on its own: owner rule — your current / past employers are never watched
        from app.core.http import PoliteClient
        from app.modules.m1_companies import employers

        with PoliteClient() as http, session_scope() as s:
            employer_result = employers.refresh(s, llm, http)
    except Exception as e:
        log.warning("m5 employers failed: %r", e)
        employer_result = {"error": str(e)[:200]}
    with session_scope() as s:
        result = check(s, llm)
    result["employers"] = employer_result
    if llm is not None:
        with session_scope() as s:
            if direction.needs_refresh(s):
                result["direction"] = direction.refresh(s, llm)
        with session_scope() as s:
            result["memory_clashes"] = memory.check_clashes(s, llm)
    for name, step in (("taste", memory.update_taste), ("memory_age", memory.age)):
        try:  # each on its own: one failing never undoes the other
            with session_scope() as s:
                result[name] = step(s)
        except Exception as e:
            log.warning("m5 %s failed: %r", name, e)
            result[name] = {"error": str(e)[:200]}
    log.info("m5.check_profile: %s", result)
    return result
