from app.celery_app import celery_app
from app.modules.m2_jobs.pipeline import run_pipeline


@celery_app.task(name="m2.process")
def process_task() -> dict:
    """Normalise new/changed raw postings, cluster siblings, apply Kind-1 filters, fetch descriptions."""
    return run_pipeline()
