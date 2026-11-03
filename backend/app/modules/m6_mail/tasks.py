"""Module 6d background jobs: the summary at each of your mail times, the alerts every few minutes. Both do
nothing while their switch is off (Settings). A Gmail problem is logged, never retried in a loop, and what
Gmail said about your sign-in (e.g. "connect again") is kept so Settings shows it."""

import logging

from app.celery_app import celery_app
from app.db import session_scope
from app.modules.m6_mail import switches

log = logging.getLogger(__name__)
ALERTS_LOCK = "mail:alerts:lock"


def _run(what: str, send) -> dict:
    from app.modules.m3_outreach import gmail

    with session_scope() as s:
        step = s.begin_nested()
        try:
            result = send(s)
            step.commit()
        except Exception as e:              # not connected, Google said no, no internet: told in the log
            acct = gmail.account(s)
            status = acct.status if acct else None
            step.rollback()
            if acct is not None:            # e.g. "reconnect": your sign-in was revoked — that stays
                acct.status = status
            result = {"sent": False, "why": f"{type(e).__name__}: {str(e)[:200]}"}
            log.warning("%s not sent: %s", what, result["why"])
    return result


@celery_app.task(name="m6.summary")
def summary_task() -> dict:
    from app.modules.m6_mail.summary import send_summary

    if not switches.is_on("summary"):
        return {"sent": False, "why": "the summary mail is off"}
    result = _run("summary mail", send_summary)
    log.info("m6.summary: %s", result)
    return result


@celery_app.task(name="m6.alerts")
def alerts_task() -> dict:
    from app.core.redis_client import get_redis
    from app.modules.m6_mail.alerts import send_alerts

    if not (switches.is_on("fast") or switches.is_on("reply")):
        return {"sent": False, "why": "alerts are off"}
    try:
        if not get_redis().set(ALERTS_LOCK, "1", nx=True, ex=600):
            return {"sent": False, "why": "the previous check is still running"}
    except Exception:
        return {"sent": False, "why": "Redis isn't reachable"}
    try:
        result = _run("alert", send_alerts)
    finally:
        get_redis().delete(ALERTS_LOCK)
    if result.get("sent"):
        log.info("m6.alerts: %s", result)
    return result
