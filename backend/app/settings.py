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
    llm_fast_model: str = "claude-haiku-5-5"
    llm_judge_model: str = "claude-sonnet-5-5"

    embedding_model: str = "BAAI/bge-small-en-v1.5"
    embedding_cache_dir: str = "/opt/models"

    brave_api_key: str = ""
    adzuna_app_id: str = ""
    adzuna_app_key: str = ""
    jooble_api_key: str = ""


    def secrets(self) -> list[str]:
        values = [
            self.anthropic_api_key, self.brave_api_key, self.adzuna_app_id,
            self.adzuna_app_key, self.jooble_api_key,
        ]
        return [v for v in values if v and len(v) >= 4]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def redact(text: str) -> str:
    """Strip configured secrets from text that will be stored or shown (errors, logs)."""
    for secret in get_settings().secrets():
        text = text.replace(secret, "***")
    return text
