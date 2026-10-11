from celery.signals import worker_ready

from app.celery_app import celery_app
from app.db import session_scope
from app.modules.m1_companies.queue import run_batch
from app.modules.m1_companies.resolver import run_resolver
from app.modules.m1_companies.revival import check_rules_changes
from app.modules.m1_companies.runner import run_scout


@celery_app.task(name="m1.run_scout")
def run_scout_task(name: str) -> dict:
    return run_scout(name)


@celery_app.task(name="m1.resolve_domains")
def resolve_domains_task() -> dict:
    return run_resolver()


@celery_app.task(name="m1.evaluate_batch")
def evaluate_batch_task() -> dict:
    return run_batch()


@celery_app.task(name="m1.evaluate_batch_ids")
def evaluate_ids_task(company_ids: list[int]) -> dict:
    return run_batch(company_ids=company_ids)


@celery_app.task(name="m1.activate_batch")
def activate_batch_task(company_ids: list[int] | None = None) -> dict:
    from app.modules.m1_companies.watch.queue import run_activation_batch

    return run_activation_batch(company_ids=company_ids)


@celery_app.task(name="m1.sync_batch")
def sync_batch_task() -> dict:
    from app.modules.m1_companies.watch.queue import run_sync_batch

    return run_sync_batch()


@celery_app.task(name="m1.garden")
def garden_task() -> dict:
    from app.modules.m1_companies.watch.queue import run_gardening

    return run_gardening()


@celery_app.task(name="m1.check_rules_changes")
def check_rules_changes_task() -> dict:
    with session_scope() as s:
        return dict(check_rules_changes(s))


@worker_ready.connect
def _on_worker_start(**_) -> None:
    """Spec §1.2: seeds load on startup. A rules edit needs a restart, so check for one here too."""
    run_scout_task.delay("seeds")
    check_rules_changes_task.delay()
