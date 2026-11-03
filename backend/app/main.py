from fastapi import FastAPI

from app.api import (
    applications, audit, companies, contacts, feeds, health, jobs, maintenance, outreach, profile, scouts, verdicts,
)
from app.api import config as config_api
from app.config_loader import get_config


def create_app() -> FastAPI:
    get_config()  # fail fast on broken YAML rather than at first request

    app = FastAPI(title="Job Agent", version="0.4.0")
    app.include_router(health.router)
    for router in (config_api.router, companies.router, scouts.router, audit.router, verdicts.router, feeds.router, jobs.router,
                   contacts.router, outreach.router, applications.router, profile.router, maintenance.router):
        app.include_router(router, prefix="/api")
    # Module 6: the app's screens (http://localhost:8000) and their stylesheet
    from pathlib import Path

    from fastapi.staticfiles import StaticFiles

    from app.web import pages

    app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "web" / "static")), name="static")
    app.include_router(pages.router)
    return app


app = create_app()
