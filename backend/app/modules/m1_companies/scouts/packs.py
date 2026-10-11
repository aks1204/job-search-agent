from collections.abc import Iterator

from app.core.hosts import normalize_domain
from app.modules.m1_companies.candidate import Candidate
from app.modules.m1_companies.scouts.base import Scout, ScoutContext


class PacksScout(Scout):
    """Curated seed packs (config/seed_packs). Judged like everyone else; your own seed list outranks them."""

    name = "packs"

    def not_ready_reason(self) -> str | None:
        missing = [p for p in self.settings.enabled_packs if p not in self.config.seed_packs]
        if missing:
            return f"unknown seed pack(s) in scouts.yaml: {', '.join(missing)}"
        return None

    def discover(self, ctx: ScoutContext) -> Iterator[Candidate]:
        for pack_name in self.settings.enabled_packs:
            for seed in self.config.seed_packs[pack_name].companies:
                domain = normalize_domain(seed.domain)
                if domain is None:
                    continue
                yield Candidate(
                    scout=self.name, external_id=domain, name=seed.name, domain=domain,
                    type_hint=seed.type, data={"pack": pack_name},
                )
