from celery.signals import worker_ready
from sqlalchemy import func, select

from app.celery_app import celery_app
from app.core.http import PoliteClient
from app.db import session_scope
from app.models.assessment import ExchangeFactor
from app.modules.m2_jobs.pipeline import run_pipeline
from app.modules.m2_jobs.rates import refresh_from_world_bank


@celery_app.task(name="m2.process")
def process_task() -> dict:
    """Normalise new/changed raw postings, cluster siblings, Kind-1 filters, descriptions, AI reads."""
    return run_pipeline()


@celery_app.task(name="m2.refresh_rates")
def refresh_rates_task() -> dict:
    """World Bank PPP + exchange rates (yearly; also once at start-up if none are stored yet)."""
    with PoliteClient() as http, session_scope() as s:
        return refresh_from_world_bank(s, http)


@worker_ready.connect
def _rates_on_start(**_) -> None:
    with session_scope() as s:
        if not s.scalar(select(func.count()).select_from(ExchangeFactor)):
            refresh_rates_task.delay()
