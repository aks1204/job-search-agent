from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass
from typing import ClassVar

from app.config_loader import LoadedConfig
from app.config_schemas import ScoutBase
from app.core.http import PoliteClient
from app.modules.m1_companies.candidate import Candidate


@dataclass
class ScoutContext:
    http: PoliteClient
    config: LoadedConfig
    state: dict  # persisted between runs; mutate freely


class Scout(ABC):
    name: ClassVar[str]

    def __init__(self, config: LoadedConfig):
        self.config = config

    @property
    def settings(self) -> ScoutBase:
        return getattr(self.config.scouts, self.name)

    def not_ready_reason(self) -> str | None:
        """Why this scout can't run right now (missing key, no target roles…), or None."""
        return None

    @abstractmethod
    def discover(self, ctx: ScoutContext) -> Iterator[Candidate]: ...

    def is_placeholder(self, company_name: str | None) -> bool:
        if not company_name or not company_name.strip():
            return True
        lowered = company_name.strip().lower()
        return any(word in lowered for word in self.config.scouts.company_name_skiplist)

    def target_roles(self) -> list[str]:
        return list(self.config.owner.target_roles) if self.config.owner else []

    def roles_reason(self) -> str | None:
        if self.config.owner is None:
            return "config/owner_stub.yaml is missing"
        if not self.config.owner.target_roles:
            return "no target_roles in config/owner_stub.yaml"
        return None
