"""Part 6b: "Run now" buttons. Every job here also runs on its schedule; the button just does it now, in the
background (the worker), and leaves a ping when it's done. One run of each job at a time."""

import logging

from app.celery_app import celery_app
from app.config_schemas import SCOUT_NAMES

log = logging.getLogger(__name__)

LOCK = "web:running:{}"
LOCK_SECONDS = 4 * 3600          # a run that died without cleaning up frees itself after this


def _scout(name):
    from app.modules.m1_companies.tasks import run_scout_task
    return run_scout_task(name)


def _resolve(_):
    from app.modules.m1_companies.tasks import resolve_domains_task
    return resolve_domains_task()


def _evaluate(company_id):
    from app.modules.m1_companies.queue import run_batch
    if company_id:                                    # "Judge again" on one company's page
        return run_batch(company_ids=[int(company_id)], force=True)
    return run_batch()


def _activate(company_id):
    from app.modules.m1_companies.watch.queue import run_activation_batch
    return run_activation_batch(company_ids=[int(company_id)] if company_id else None)


def _recheck(_):
    from app.db import session_scope
    from app.modules.m1_companies.watch.queue import recheck_unread
    with session_scope() as s:
        return {"flagged_for_a_new_look": len(recheck_unread(s, apply=True))}


def _sync(_):
    from app.modules.m1_companies.tasks import sync_batch_task
    return sync_batch_task()


def _process(_):
    from app.modules.m2_jobs.tasks import process_task
    return process_task()


def _assess(_):
    from app.modules.m2_jobs.assess import run_assessments
    return run_assessments()


def _score(_):
    from app.modules.m2_jobs.scoring import run_scoring
    return run_scoring()


def _contacts(_):
    from app.modules.m3_outreach.tasks import contacts_task
    return contacts_task()


def _drafts(cluster_id):
    from app.db import session_scope
    from app.modules.m3_outreach.campaigns import prepare_campaigns
    from app.modules.m3_outreach.drafting import run_drafts
    ids = [int(cluster_id)] if cluster_id else None
    result = dict(run_drafts(cluster_ids=ids))
    with session_scope() as s:
        result.update({f"campaigns_{k}": v for k, v in prepare_campaigns(s, ids).items()})
    return result


def _sweep(_):
    from app.modules.m3_outreach.tasks import sweep_task
    return sweep_task()


def _mail(_):
    from app.modules.m3_outreach.tasks import mail_check_task
    return mail_check_task()


def _track(_):
    from app.modules.m4_apply.tasks import track_task
    return track_task()


def _garden(_):
    from app.modules.m1_companies.tasks import garden_task
    return garden_task()


def _rates(_):
    from app.modules.m2_jobs.tasks import refresh_rates_task
    return refresh_rates_task()


def _profile(_):
    from app.modules.m5_profile.tasks import check_profile_task
    return check_profile_task()


def _direction(_):
    from app.core.llm import LLM
    from app.db import session_scope
    from app.modules.m5_profile import direction
    if not LLM.available():
        raise RuntimeError("needs ANTHROPIC_API_KEY in .env")
    with session_scope() as s:
        return direction.refresh(s, LLM())


def _resume(_):
    """Read your résumé again → a draft of your profile for you to check (review/profile.yaml)."""
    from app.core.llm import LLM
    from app.db import session_scope
    from app.modules.m5_profile import profile as P
    from app.modules.m5_profile import store
    with session_scope() as s:                       # saved first: a file problem never wastes the AI read
        draft = store.latest_draft(s)
        if draft is None or draft.resume_hash != P.file_hash(P.resume_path()):   # same résumé: no second AI read
            if not LLM.available():
                raise RuntimeError("needs ANTHROPIC_API_KEY in .env")
            draft = P.make_draft(s, LLM())
    P.export(draft)
    return {"draft": draft.id, "still_to_fill_in": len((draft.notes or {}).get("gaps") or [])}


def _sources(_):
    from app.modules.maintenance.tasks import source_check_task
    return source_check_task()


# key → (what the button says, what it does). Order = the Maintenance screen's order.
JOBS: dict[str, tuple[str, object]] = {
    **{f"scout:{n}": (f"Look for companies: {n}", lambda _arg, n=n: _scout(n)) for n in SCOUT_NAMES},
    "resolve": ("Find websites of companies seen by name", _resolve),
    "evaluate": ("Judge new companies", _evaluate),
    "activate": ("Set up job feeds for kept companies", _activate),
    "recheck": ("Re-check companies on the job-site fallback", _recheck),
    "sync": ("Read job feeds", _sync),
    "process": ("Process new postings (filters, AI read, scores)", _process),
    "assess": ("AI-read job descriptions", _assess),
    "score": ("Score roles", _score),
    "contacts": ("Find contacts and write drafts", _contacts),
    "drafts": ("Write outreach drafts", _drafts),
    "sweep": ("Re-check people on contact lists", _sweep),
    "mail": ("Check replies and bounces", _mail),
    "track": ("Check your applications", _track),
    "garden": ("Weekly upkeep", _garden),
    "rates": ("Refresh exchange rates", _rates),
    "profile": ("Daily profile check (résumé, reminders, patterns)", _profile),
    "direction": ("Re-work your career direction (about 3 cents)", _direction),
    "resume": ("Read your résumé again", _resume),
    "sources": ("Check the sources for warning signs", _sources),
}
MAINTENANCE_JOBS = [k for k in JOBS if k not in ("resume",)]
TARGETED = ("evaluate", "activate", "drafts")      # jobs that can be aimed at one company / role


def start(job: str, arg: str | None = None) -> str:
    """Queue a run; returns what to tell you. Raises ValueError for an unknown job."""
    from app.core.redis_client import get_redis

    if job not in JOBS:
        raise ValueError(f"no such job {job!r}")
    if arg is not None and job not in TARGETED:
        raise ValueError("this job can't be aimed at one item")
    label = JOBS[job][0]
    if not get_redis().set(LOCK.format(job), "1", nx=True, ex=LOCK_SECONDS):
        return f"Already running: {label}. You'll get a ping when it's done."
    try:
        celery_app.send_task("web.run_now", args=[job, arg])
    except Exception:
        get_redis().delete(LOCK.format(job))
        raise
    return f"Started: {label}. You'll get a ping when it's done — keep using the app meanwhile."


def summary(result) -> str:
    """'found 3 · kept 1' from a job's counts (zeros and long lists left out)."""
    if not isinstance(result, dict):
        return str(result)[:300] if result else ""
    parts = []
    for k, v in result.items():
        if isinstance(v, dict):
            inner = summary(v)
            if inner:
                parts.append(f"{k.replace('_', ' ')}: {inner}")
        elif isinstance(v, (int, float)) and not isinstance(v, bool):
            if v:
                parts.append(f"{k.replace('_', ' ')} {v}")
        elif isinstance(v, str) and v:
            parts.append(f"{k.replace('_', ' ')}: {v[:80]}")
    return " · ".join(parts)[:600] or "nothing to do"


@celery_app.task(name="web.run_now")
def run_now(job: str, arg: str | None = None) -> dict:
    from app.core.notify import notify
    from app.core.redis_client import get_redis
    from app.db import session_scope

    label, fn = JOBS[job]
    try:
        result = fn(arg)
        title, detail = f"Done: {label}", summary(result)
    except Exception as e:                      # told to you, never silent
        log.warning("web.run_now %s failed: %r", job, e)
        result, title, detail = {"error": str(e)[:200]}, f"Didn't finish: {label}", f"{type(e).__name__}: {str(e)[:200]}"
    finally:
        try:
            get_redis().delete(LOCK.format(job))
        except Exception:
            pass
    with session_scope() as s:
        notify(s, "update", "happened", title, detail=detail, subject=("run", f"{job}:{arg}" if arg else job))
    return result if isinstance(result, dict) else {"result": str(result)}
