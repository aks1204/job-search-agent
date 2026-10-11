from collections.abc import Iterator

from app.core.hosts import normalize_domain
from app.modules.m1_companies.candidate import Candidate
from app.modules.m1_companies.scouts.base import Scout, ScoutContext


class SeedScout(Scout):
    """config/seed_companies.yaml. Seeds enter the same pipe and get judged like everyone else."""

    name = "seeds"

    def discover(self, ctx: ScoutContext) -> Iterator[Candidate]:
        for seed in self.config.seeds.companies:
            domain = normalize_domain(seed.domain)
            if domain is None:
                continue
            yield Candidate(
                scout=self.name,
                external_id=domain,
                name=seed.name,
                domain=domain,
                type_hint=seed.type,
                data={"trusted": seed.trusted, "notes": seed.notes} if (seed.trusted or seed.notes) else {},
            )
