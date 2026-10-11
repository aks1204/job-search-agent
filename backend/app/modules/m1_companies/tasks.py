from celery.signals import worker_ready

from app.celery_app import celery_app
from app.modules.m1_companies.resolver import run_resolver
from app.modules.m1_companies.runner import run_scout


@celery_app.task(name="m1.run_scout")
def run_scout_task(name: str) -> dict:
    return run_scout(name)


@celery_app.task(name="m1.resolve_domains")
def resolve_domains_task() -> dict:
    return run_resolver()


@worker_ready.connect
def _load_seeds_on_start(**_) -> None:
    """Spec §1.2: the seed list loads on startup (a config change needs a restart anyway)."""
    run_scout_task.delay("seeds")
