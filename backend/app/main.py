from fastapi import FastAPI

from app.api import config as config_api
from app.api import health
from app.config_loader import get_config


def create_app() -> FastAPI:
    get_config()  # fail fast on broken YAML rather than at first request

    app = FastAPI(title="Job Agent", version="0.1.0")
    app.include_router(health.router)
    app.include_router(config_api.router, prefix="/api")
    return app


app = create_app()
