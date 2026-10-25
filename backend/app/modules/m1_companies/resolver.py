"""Finds the website — the identity — of companies seen only by name (mostly aggregator results).

Rungs: web search (when a Brave key is configured) → AI guess, trusted only when
the guessed site actually names the company. Domains found either way are
tagged `domain_source = "search"` / `"llm"` (inferred, not verified).
"""

import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.config_loader import get_config
from app.core import audit
from app.core.hosts import is_junk, is_profile_site_for_other, normalize_domain
from app.core.http import PoliteClient
from app.core.llm import LLMError
from app.core.search import search_available, web_search
from app.db import session_scope
from app.models.company import STATUS_UNRESOLVED, Company, CompanySighting
from app.modules.m1_companies.enrich import fetch_site_text
from app.modules.m1_companies.identity import assign_domain, name_matches_domain, normalize_name, site_mentions_name

BACKOFF_DAYS = (1, 3, 7, 14)


_TITLE_PARTS = re.compile(r"\s*[|–—:·]\s+|\s*\|\s*|\s+-\s+")


def _title_names(title: str, name: str) -> bool:
    """The page title is FOR this company: its first part is the name ('Acme — Data tools' for Acme). A name
    that merely appears in a title proves nothing: ZS's title 'ZS | Strategy & Transformation Consulting'
    once made zs.com the website of a German firm called Strategy & Transformation Consulting."""
    wanted = normalize_name(name)
    first = normalize_name(_TITLE_PARTS.split(title or "", maxsplit=1)[0])
    return bool(wanted) and (first == wanted or first.startswith(wanted + " "))   # "Plimsoll Analytics | …"


def pick_domain(name: str, results) -> str | None:
    """First non-junk result whose domain clearly fits the company's name, or whose page title leads with it."""
    for r in results:
        domain = normalize_domain(r.url)
        if not domain or is_junk(r.url) or is_profile_site_for_other(domain, name):
            continue                        # e.g. "Innerscore — Overview | ZoomInfo" is ZoomInfo's page, not theirs
        if name_matches_domain(name, domain) or _title_names(r.title, name):
            return domain
    return None


def resolve_batch(session: Session, http: PoliteClient, now: datetime | None = None) -> Counter:
    cfg = get_config().scouts.resolver
    counts: Counter = Counter()
    if not search_available():
        counts["skipped_no_search_key"] = 1
        return counts

    now = now or datetime.now(timezone.utc)
    pending = session.scalars(
        select(Company)
        .where(
            Company.status == STATUS_UNRESOLVED,
            Company.resolution_attempts < cfg.max_attempts,
            or_(Company.next_resolution_at.is_(None), Company.next_resolution_at <= now),
        )
        .order_by(Company.first_seen_at)
        .limit(cfg.batch_size)
    ).all()

    for company in pending:
        results = web_search(
            http, f"{company.name} company official website",
            purpose="resolver", monthly_budget=cfg.brave_monthly_budget,
        )
        if results is None:
            counts["budget_exhausted"] += 1
            break
        domain = pick_domain(company.name, results)
        if domain:
            name = company.name
            kept = assign_domain(session, company, domain, source="search")
            audit.record(
                session, "m1", "domain_resolved",
                f"Identified {name} as {domain} (from web search — inferred)",
                subject_type="company", subject_id=str(kept.id), is_judgment=True,
                detail={"query_name": name, "domain": domain, "candidates": [r.url for r in results[:5]]},
            )
            counts["resolved"] += 1
        else:
            company.resolution_attempts += 1
            idx = min(company.resolution_attempts, len(BACKOFF_DAYS)) - 1
            company.next_resolution_at = now + timedelta(days=BACKOFF_DAYS[idx])
            counts["not_found"] += 1
        session.flush()
    return counts


class DomainGuess(BaseModel):
    domain: str = Field(description="The company's own registrable domain, e.g. 'acme.com' — or 'unknown'.")
    confidence: Literal["high", "medium", "low"]


GUESS_SYSTEM = """You identify a company's official website domain for a job-search assistant.
You get the company name and where it was seen (job titles, locations, snippets).
Answer only if you are confident which company this is; otherwise answer "unknown".
Never answer a job board, LinkedIn, or an applicant-tracking-system domain."""


def _context(session: Session, company: Company) -> str:
    sightings = session.scalars(
        select(CompanySighting).where(CompanySighting.company_id == company.id).limit(3)
    ).all()
    lines = []
    for s in sightings:
        d = s.data or {}
        bits = [d.get("title"), d.get("location"), (d.get("description") or d.get("headline") or "")[:300]]
        lines.append(f"- seen on {s.scout}: " + " | ".join(b for b in bits if b))
    return "\n".join(lines) or "- (no extra context)"


def llm_resolve_batch(session: Session, http: PoliteClient, llm, limit: int = 10) -> Counter:
    """Last rung: the AI guesses a domain, and we only trust it if that site names the company."""
    cfg = get_config().scouts.resolver
    counts: Counter = Counter()
    eligible = [Company.status == STATUS_UNRESOLVED, Company.llm_resolution_tried_at.is_(None)]
    if search_available():
        eligible.append(Company.resolution_attempts >= cfg.max_attempts)  # search had its turn first
    pending = session.scalars(select(Company).where(*eligible).order_by(Company.first_seen_at).limit(limit)).all()

    for company in pending:
        company.llm_resolution_tried_at = datetime.now(timezone.utc)
        try:
            guess = llm.structured(
                "fast", system=GUESS_SYSTEM, schema=DomainGuess, purpose="m1.guess_domain",
                user=f"Company name: {company.name}\nSeen as:\n{_context(session, company)}",
                session=session, subject=("company", str(company.id)),
            ).parsed
        except LLMError:
            counts["llm_error"] += 1
            continue
        domain = normalize_domain(guess.domain)
        if guess.confidence == "low" or not domain or is_junk(domain) or is_profile_site_for_other(domain, company.name):
            counts["no_guess"] += 1
            continue
        site = fetch_site_text(http, domain)
        if not site or not site_mentions_name(site[0], company.name):
            counts["guess_not_confirmed"] += 1
            continue
        name = company.name
        kept = assign_domain(session, company, domain, source="llm")
        audit.record(
            session, "m1", "domain_resolved",
            f"Identified {name} as {domain} (AI guess, confirmed on the site — inferred)",
            subject_type="company", subject_id=str(kept.id), is_judgment=True,
            detail={"domain": domain, "confidence": guess.confidence, "method": "llm+site_check"},
        )
        counts["resolved_by_llm"] += 1
        session.flush()
    return counts


def run_resolver() -> dict:
    from app.core.llm import LLM

    counts: Counter = Counter()
    with PoliteClient() as http:
        with session_scope() as session:
            counts.update(resolve_batch(session, http))
        if LLM.available():
            with session_scope() as session:
                counts.update(llm_resolve_batch(session, http, LLM()))
    return dict(counts)
