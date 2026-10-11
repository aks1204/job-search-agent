from fastapi import FastAPI

from app.api import audit, companies, health, scouts, verdicts
from app.api import config as config_api
from app.config_loader import get_config


def create_app() -> FastAPI:
    get_config()  # fail fast on broken YAML rather than at first request

    app = FastAPI(title="Job Agent", version="0.3.0")
    app.include_router(health.router)
    for router in (config_api.router, companies.router, scouts.router, audit.router, verdicts.router):
        app.include_router(router, prefix="/api")
    return app


app = create_app()
