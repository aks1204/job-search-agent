"""Finds the website — the identity — of companies seen only by name (mostly aggregator results).

Part 1a uses web search when a Brave key is configured. Without one these
companies wait as `unresolved`; Part 1b adds an LLM guess-and-verify rung.
Search-found domains are tagged `domain_source = "search"` (inferred, not verified).
"""

from collections import Counter
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.config_loader import get_config
from app.core import audit
from app.core.hosts import is_junk, normalize_domain
from app.core.http import PoliteClient
from app.core.search import search_available, web_search
from app.db import session_scope
from app.models.company import STATUS_UNRESOLVED, Company
from app.modules.m1_companies.identity import assign_domain, name_matches_domain, normalize_name

BACKOFF_DAYS = (1, 3, 7, 14)


def pick_domain(name: str, results) -> str | None:
    """First non-junk result whose domain or title clearly names the company."""
    wanted = normalize_name(name)
    for r in results:
        domain = normalize_domain(r.url)
        if not domain or is_junk(r.url):
            continue
        if name_matches_domain(name, domain) or (wanted and wanted in normalize_name(r.title)):
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


def run_resolver() -> dict:
    with PoliteClient() as http, session_scope() as session:
        return dict(resolve_batch(session, http))
