"""'Companies like the ones you keep' — an owner-approved deviation from the spec (2026-10-11).

The spec says the MNC universe comes from a seed list, not discovery. The owner chose
to also grow it from their own taste: for each kept company, the cheap model suggests
similar employers. A suggestion only enters the pipeline if its domain is live and
the homepage names the company (no invented companies, no wrong domains); it is then
judged like everything else.
"""

from collections.abc import Iterator
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy import select

from app.core.hosts import is_junk, normalize_domain
from app.models.company import STATUS_DORMANT, STATUS_PASSED, STATUS_WATCHING, Company
from app.modules.m1_companies.candidate import Candidate
from app.modules.m1_companies.enrich import fetch_site_text
from app.modules.m1_companies.facts import fact_card, value
from app.modules.m1_companies.identity import site_mentions_name
from app.modules.m1_companies.scouts.base import Scout, ScoutContext

KEPT = (STATUS_PASSED, STATUS_WATCHING, STATUS_DORMANT)


class Peer(BaseModel):
    name: str
    domain: str = Field(description="Official website domain, e.g. 'razorpay.com'.")
    type: Literal["startup", "established"]
    why: str = Field(description="One short line on why it's similar.")


class PeerSuggestions(BaseModel):
    peers: list[Peer]


PEERS_SYSTEM = """You widen a job seeker's company watchlist with employers similar to one they liked.
Suggest real companies you are confident exist, with their official website domain. Favour employers that
hire for the job seeker's career field in India (offices there, or remote hiring from India). Do not repeat
the liked company itself, its subsidiaries, staffing agencies or IT-outsourcing firms.
Answer in the requested JSON format."""


class PeersScout(Scout):
    name = "peers"

    def not_ready_reason(self) -> str | None:
        return None if self.config.owner else "config/owner_stub.yaml is missing"

    def discover(self, ctx: ScoutContext) -> Iterator[Candidate]:
        if ctx.llm is None or ctx.session is None:
            ctx.state["last_note"] = "skipped: needs ANTHROPIC_API_KEY"
            return
        expanded: list[int] = ctx.state.setdefault("expanded", [])
        kept = ctx.session.scalars(
            select(Company).where(Company.status.in_(KEPT), Company.id.not_in(expanded or [0]))
            .order_by(Company.updated_at).limit(self.settings.companies_per_run)
        ).all()
        owner = self.config.owner

        for company in kept:
            expanded.append(company.id)
            what = value(fact_card(ctx.session, company.id), "what_they_build")
            try:
                result = ctx.llm.structured(
                    "fast", system=PEERS_SYSTEM, schema=PeerSuggestions, purpose="m1.suggest_peers",
                    user=(f"Job seeker's career field: {owner.career_field}\nLocation: {owner.location or 'India'}\n"
                          f"Liked company: {company.name} ({company.domain}) — {what}\n"
                          f"Suggest up to {self.settings.suggestions_per_company} similar employers."),
                    session=ctx.session, subject=("company", str(company.id)),
                ).parsed
            except Exception:
                continue
            for peer in result.peers[: self.settings.suggestions_per_company]:
                domain = normalize_domain(peer.domain)
                if not domain or is_junk(domain) or domain == company.domain:
                    continue
                site = fetch_site_text(ctx.http, domain)
                if not site or not site_mentions_name(site[0], peer.name):
                    continue  # unverifiable suggestion: never enters the pipeline
                yield Candidate(
                    scout=self.name, external_id=domain, name=peer.name, domain=domain, type_hint=peer.type,
                    data={"suggested_from": company.domain, "why": peer.why},
                )
