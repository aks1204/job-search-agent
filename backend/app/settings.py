"""Environment settings (secrets, URLs). Behavioural rules live in config/*.yaml."""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    database_url: str
    redis_url: str = "redis://redis:6379/0"
    config_dir: Path = Path("/config")

    anthropic_api_key: str = ""
    llm_fast_model: str = "claude-haiku-4-5-20251001"
    llm_judge_model: str = "claude-sonnet-5-5"

    embedding_model: str = "BAAI/bge-small-en-v1.5"
    embedding_cache_dir: str = "/opt/models"

    brave_api_key: str = ""
    adzuna_app_id: str = ""
    adzuna_app_key: str = ""
    jooble_api_key: str = ""


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
