"""Loads config/*.yaml once at startup. Editing rules = edit file + restart, by design.

Each file gets a content hash of its *parsed* data (comment-only edits don't
change it). Verdicts and scores record these hashes so they can be re-run when
the rules they depended on change.
"""

import dataclasses
import hashlib
import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, TypeVar

import yaml
from pydantic import BaseModel, ValidationError

from app.config_schemas import (
    AppConfig, ApplyConfig, CompanyRules, JobRules, LLMConfig, OutreachConfig, OwnerStub, ScoutsConfig, SeedList, SeedPack,
    WatchConfig,
)
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
    owner_file: OwnerStub | None  # config/owner_stub.yaml (None until it exists) — the fallback before Module 5
    rules: CompanyRules
    job_rules: JobRules
    llm: LLMConfig
    watch: WatchConfig
    outreach: OutreachConfig
    apply: ApplyConfig
    adapters: dict[str, dict]  # giant-company adapters from config/adapters/*.yaml, keyed by domain
    seed_packs: dict[str, SeedPack]  # curated optional lists from config/seed_packs/*.yaml, keyed by file stem
    profile_from_db: bool = False  # the running app: your confirmed profile (Module 5) wins over the file

    @property
    def owner(self) -> OwnerStub | None:
        """Who the agent works for. Your confirmed profile (Module 5a) once there is one; until then
        config/owner_stub.yaml. Every module reads this, so switching over needs no other change."""
        if self.profile_from_db:
            from app.modules.m5_profile.store import current_profile  # late: needs the database

            profile = current_profile()
            if profile is not None:
                return profile
        return self.owner_file

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
        owner_file=_validate(files, "owner_stub", OwnerStub),
        rules=_validate(files, "company_rules", CompanyRules) or CompanyRules(),
        job_rules=_validate(files, "job_rules", JobRules) or JobRules(),
        llm=_validate(files, "llm", LLMConfig) or LLMConfig(),
        watch=_validate(files, "watch", WatchConfig) or WatchConfig(),
        outreach=_validate(files, "outreach", OutreachConfig) or OutreachConfig(),
        apply=_validate(files, "apply", ApplyConfig) or ApplyConfig(),
        adapters=_load_adapters(config_dir / "adapters"),
        seed_packs=_load_seed_packs(config_dir / "seed_packs"),
    )


def _load_seed_packs(folder: Path) -> dict[str, SeedPack]:
    packs: dict[str, SeedPack] = {}
    if not folder.is_dir():
        return packs
    for path in sorted(folder.glob("*.yaml")):
        try:
            packs[path.stem] = SeedPack.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")) or {})
        except (yaml.YAMLError, ValidationError) as e:
            raise ConfigError(f"seed_packs/{path.name}: {e}") from e
    return packs


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
    return dataclasses.replace(load_config(get_settings().config_dir), profile_from_db=True)
