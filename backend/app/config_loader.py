"""Loads config/*.yaml once at startup. Editing rules = edit file + restart, by design.

Each file gets a content hash of its *parsed* data (comment-only edits don't
change it). Verdicts and scores record these hashes so they can be re-run when
the rules they depended on change.
"""

import hashlib
import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, TypeVar

import yaml
from pydantic import BaseModel, ValidationError

from app.config_schemas import AppConfig, CompanyRules, LLMConfig, OwnerStub, ScoutsConfig, SeedList, WatchConfig
from app.settings import get_settings

M = TypeVar("M", bound=BaseModel)


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class ConfigFile:
    name: str
    version: str
    data: Any


@dataclass(frozen=True)
class LoadedConfig:
    files: dict[str, ConfigFile]
    version: str
    app: AppConfig
    scouts: ScoutsConfig
    seeds: SeedList
    owner: OwnerStub | None  # None until config/owner_stub.yaml exists
    rules: CompanyRules
    llm: LLMConfig
    watch: WatchConfig
    adapters: dict[str, dict]  # giant-company adapters from config/adapters/*.yaml, keyed by domain

    def file_version(self, name: str) -> str | None:
        f = self.files.get(name)
        return f.version if f else None

    def rules_section_version(self, section: str) -> str:
        """Hash of one company_rules section (defaults included), for targeted re-evaluation."""
        return _hash(self.rules.model_dump(mode="json")[section])


def _hash(data: Any) -> str:
    canonical = json.dumps(data, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def _validate(files: dict[str, ConfigFile], name: str, model: type[M]) -> M | None:
    if name not in files:
        return None
    try:
        return model.model_validate(files[name].data)
    except ValidationError as e:
        raise ConfigError(f"{name}.yaml: {e}") from e


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

    return LoadedConfig(
        files=files,
        version=_hash({name: f.version for name, f in files.items()}),
        app=_validate(files, "app", AppConfig) or AppConfig(),
        scouts=_validate(files, "scouts", ScoutsConfig) or ScoutsConfig(),
        seeds=_validate(files, "seed_companies", SeedList) or SeedList(),
        owner=_validate(files, "owner_stub", OwnerStub),
        rules=_validate(files, "company_rules", CompanyRules) or CompanyRules(),
        llm=_validate(files, "llm", LLMConfig) or LLMConfig(),
        watch=_validate(files, "watch", WatchConfig) or WatchConfig(),
        adapters=_load_adapters(config_dir / "adapters"),
    )


def _load_adapters(folder: Path) -> dict[str, dict]:
    """Community-maintained per-company readers (see config/adapters/README.md)."""
    from app.config_schemas import Adapter

    adapters: dict[str, dict] = {}
    if not folder.is_dir():
        return adapters
    for path in sorted(folder.glob("*.yaml")):
        if path.name.endswith(".example.yaml"):
            continue
        try:
            adapter = Adapter.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")) or {})
        except (yaml.YAMLError, ValidationError) as e:
            raise ConfigError(f"adapters/{path.name}: {e}") from e
        adapters[adapter.domain.lower()] = adapter.model_dump()
    return adapters


@lru_cache(maxsize=1)
def get_config() -> LoadedConfig:
    return load_config(get_settings().config_dir)
