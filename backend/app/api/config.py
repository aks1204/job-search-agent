"""Read-only view of the loaded rule files (feeds the Settings screen's rule viewers)."""

from fastapi import APIRouter

from app.config_loader import get_config

router = APIRouter()


@router.get("/config")
def read_config() -> dict:
    cfg = get_config()
    return {
        "version": cfg.version,
        "files": {name: {"version": f.version, "content": f.data} for name, f in cfg.files.items()},
    }
