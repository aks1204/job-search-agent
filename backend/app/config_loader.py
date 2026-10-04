"""Loads config/*.yaml once at startup. Editing rules = edit file + restart, by design.

Each file gets a content hash of its *parsed* data (comment-only edits don't
change it). Verdicts and scores record these hashes so they can be re-run when
the rules they depended on change.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import time
from functools import lru_cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.settings import get_settings


class ConfigError(RuntimeError):
    pass


class QuietHours(BaseModel):
    start: time
    end: time


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timezone: str = "Asia/Kolkata"
    quiet_hours: QuietHours = QuietHours(start=time(22), end=time(8))
    summary_mail_times: list[time] = [time(8), time(20)]
    heartbeat_interval_seconds: int = Field(60, ge=10, le=3600)

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as e:
            raise ValueError(f"unknown timezone {v!r}") from e
        return v


@dataclass(frozen=True)
class ConfigFile:
    name: str
    version: str
    data: Any


@dataclass(frozen=True)
class LoadedConfig:
    files: dict[str, ConfigFile]
    app: AppConfig
    version: str

    def file_version(self, name: str) -> str | None:
        f = self.files.get(name)
        return f.version if f else None


def _hash(data: Any) -> str:
    canonical = json.dumps(data, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def load_config(config_dir: Path) -> LoadedConfig:
    if not config_dir.is_dir():
        raise ConfigError(f"config directory not found: {config_dir}")

    files: dict[str, ConfigFile] = {}
    for path in sorted(config_dir.glob("*.yaml")):
        if path.name.endswith(".example.yaml"):
            continue
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as e:
            raise ConfigError(f"{path.name}: invalid YAML: {e}") from e
        files[path.stem] = ConfigFile(path.stem, _hash(data), data)

    try:
        app_cfg = AppConfig.model_validate(files["app"].data if "app" in files else {})
    except ValidationError as e:
        raise ConfigError(f"app.yaml: {e}") from e

    version = _hash({name: f.version for name, f in files.items()})
    return LoadedConfig(files=files, app=app_cfg, version=version)


@lru_cache(maxsize=1)
def get_config() -> LoadedConfig:
    return load_config(get_settings().config_dir)
