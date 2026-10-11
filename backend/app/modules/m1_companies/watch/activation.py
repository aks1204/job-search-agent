"""Activation (spec §1.5): kept company → careers page → reading method → ownership check → tracking record → first sync.

Idempotent: re-running it on a company with a feed updates that feed in place,
which is also how gardening heals broken feeds and ATS migrations.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config_loader import get_config
from app.core import audit
from app.core.http import PoliteClient
from app.core.hosts import ats_hint, domain_label
from app.models.company import STATUS_WATCHING, Company
from app.models.feed import CompanyFeed
from app.modules.m1_companies.facts import fact_card, value
from app.modules.m1_companies.identity import normalize_name
from app.modules.m1_companies.watch.careers import Discovery, discover
from app.modules.m1_companies.watch.ownership import verify_ownership
from app.modules.m1_companies.watch.readers import (
    READERS, FeedNotFound, FeedSpec, FetchResult, find_workday_host, read_adapter, read_workday,
)
from app.modules.m1_companies.watch.sync import SyncResult, aggregator_jobs, sync_feed

log = logging.getLogger(__name__)

# The ladder: public ATS endpoints → Workday → per-company adapters → aggregator floor.
LADDER = ("greenhouse", "lever", "ashby", "smartrecruiters", "workable", "recruitee", "workday", "adapter")
GUESSABLE = ("greenhouse", "lever", "ashby", "smartrecruiters")


@dataclass
class FeedCandidate:
    spec: FeedSpec
    linked_from_site: bool
    how: str
    source_rank: int = 0  # 0 linked from site · 1 adapter · 2 AI-suggested · 3 blind slug guess


class BoardSuggestions(BaseModel):
    urls: list[str] = Field(description=(
        "Job-board URLs this company publishes its openings on — Greenhouse, Lever, Ashby, SmartRecruiters, "
        "Workable, Recruitee, or Workday (*.myworkdayjobs.com/…). Only boards you are confident about; "
        "an empty list if unsure."
    ))


SUGGEST_SYSTEM = """You help a job-search assistant find where a company lists its open jobs.
Large companies often run a JavaScript careers site backed by an applicant-tracking system such as Workday.
Give the board URLs you are confident about for THIS company (match the name and domain). Every URL will be
checked live, so never invent one. For Workday, the tenant and site name matter most
(https://TENANT.wdN.myworkdayjobs.com/SITE); the server number N is corrected automatically if it has changed.
Answer in the requested JSON format."""


def _label(hint: dict) -> str:
    """'lever/acme', or for Workday the full address that matters: 'workday/lilly/LLY @ lilly.wd5'."""
    if hint["vendor"] == "workday":
        return f"workday/{hint['slug']}/{hint.get('site')} @ {hint.get('host')}"
    return f"{hint['vendor']}/{hint['slug']}"


def slug_guesses(company: Company) -> list[str]:
    words = normalize_name(company.name).split()
    guesses = [domain_label(company.domain or ""), "".join(words), "-".join(words)]
    return [g for g in dict.fromkeys(guesses) if g and len(g) >= 2][:3]


def _ai_suggestions(company: Company, llm, session, notes: list[str]) -> list[dict]:
    if llm is None:
        notes.append("AI suggestions skipped (no API key)")
        return []
    try:
        urls = llm.structured(
            "fast", system=SUGGEST_SYSTEM, schema=BoardSuggestions, purpose="m1.suggest_boards",
            user=f"Company: {company.name}\nDomain: {company.domain}", session=session,
            subject=("company", str(company.id)),
        ).parsed.urls
    except Exception as e:
        notes.append(f"AI suggestions failed: {type(e).__name__}")
        return []
    hints = [h for u in urls[:5] if (h := ats_hint(u))]
    unusable = [u for u in urls[:5] if not ats_hint(u)]
    notes.append(f"AI suggested {len(urls)} board(s)" + (f", unusable: {', '.join(unusable)[:160]}" if unusable else ""))
    return hints


def _candidates(company: Company, discovery: Discovery | None, *, llm=None, session=None,
                notes: list[str] | None = None, previous: CompanyFeed | None = None) -> list[FeedCandidate]:
    notes = notes if notes is not None else []
    found: list[FeedCandidate] = []
    if previous is not None and previous.method not in ("aggregator", "adapter"):
        # Re-activation: the board we used before is the first thing to re-check (and re-verify).
        found.append(FeedCandidate(FeedSpec(previous.method, previous.slug, previous.site, previous.host), False,
                                   f"previous {previous.method}/{previous.slug}", source_rank=1))
    if discovery:
        for h in discovery.scan.ats_hints:
            found.append(FeedCandidate(FeedSpec(h["vendor"], h["slug"], h.get("site"), h.get("host")),
                                       discovery.on_company_site, f"{discovery.how}: {_label(h)}"))
    if company.domain in get_config().adapters:
        found.append(FeedCandidate(FeedSpec("adapter"), True, "community adapter", source_rank=1))
    if not any(c.linked_from_site for c in found):  # only pay for AI suggestions when the site gave us nothing
        for h in _ai_suggestions(company, llm, session, notes):
            found.append(FeedCandidate(FeedSpec(h["vendor"], h["slug"], h.get("site"), h.get("host")), False,
                                       f"AI-suggested {_label(h)}", source_rank=2))
    for vendor in GUESSABLE:  # last resort; skipped once a better candidate is confirmed
        for slug in slug_guesses(company):
            found.append(FeedCandidate(FeedSpec(vendor, slug), False, f"guessed {vendor}/{slug}", source_rank=3))
    unique = {(c.spec.method, (c.spec.slug or "").lower(), c.spec.site): c for c in reversed(found)}
    return sorted(unique.values(), key=lambda c: (c.source_rank, LADDER.index(c.spec.method)))


def _try(http: PoliteClient, company: Company, cand: FeedCandidate) -> tuple[FetchResult | None, str]:
    """(result, what happened) — the note is kept so a fallback can explain itself."""
    cfg = get_config()
    roles = list(cfg.owner.target_roles) if cfg.owner else []
    try:
        if cand.spec.method == "adapter":
            result = read_adapter(http, cfg.adapters[company.domain])
        elif cand.spec.method == "workday":
            try:
                result = read_workday(http, cand.spec, roles, pages_per_role=1)
            except (FeedNotFound, httpx.HTTPStatusError) as first_error:
                # Right tenant/site, wrong server number? Find the live one.
                probes: list[str] = []
                host = find_workday_host(http, cand.spec.slug, cand.spec.site, skip=cand.spec.host, outcomes=probes)
                if host is None:
                    first = (f"HTTP {first_error.response.status_code}"
                             if isinstance(first_error, httpx.HTTPStatusError) else "not found")
                    return None, f"{first}; {'; '.join(probes)}"
                old = cand.spec.host
                cand.spec.host = host
                cand.how += f" (server {old} → {host})"
                result = read_workday(http, cand.spec, roles, pages_per_role=1)
        else:
            result = READERS[cand.spec.method](http, cand.spec, roles)
    except FeedNotFound:
        return None, "not found"
    except httpx.HTTPStatusError as e:
        return None, f"HTTP {e.response.status_code}"
    except Exception as e:
        log.info("candidate %s failed for %s: %s", cand.how, company.name, e)
        return None, f"{type(e).__name__}: {str(e)[:80]}"
    return result, f"{len(result.jobs)} jobs"


def activate(session: Session, company: Company, http: PoliteClient, *, llm=None) -> tuple[CompanyFeed, SyncResult]:
    now = datetime.now(timezone.utc)
    discovery = discover(http, company, llm=llm, session=session) if company.domain else None

    chosen: tuple[FeedCandidate, str] | None = None
    best_jobs = -1
    attempts: list[str] = []  # every board tried and what happened — shown when we fall back
    size = value(fact_card(session, company.id), "employee_estimate")
    existing = session.scalar(select(CompanyFeed).where(CompanyFeed.company_id == company.id))
    for cand in _candidates(company, discovery, llm=llm, session=session, notes=attempts, previous=existing):
        if chosen and not cand.linked_from_site:
            break  # never let a guess displace a board the company itself links to
        result, note = _try(http, company, cand)
        if result is None:
            attempts.append(f"{cand.how} → {note}")
            continue
        ok, evidence = verify_ownership(company, result, linked_from_site=cand.linked_from_site,
                                        careers_url=discovery.careers_url if discovery else None,
                                        method=cand.spec.method, slug=cand.spec.slug, employee_estimate=size)
        if not ok:
            attempts.append(f"{cand.how} → {note}, rejected: {evidence}")
            continue
        # A company's site can link several boards (e.g. a subsidiary's small one next to the main
        # Workday site). Among verified boards, keep the biggest; the ladder order breaks ties.
        if len(result.jobs) > best_jobs:
            chosen, best_jobs = (cand, f"{evidence}; {len(result.jobs)} jobs"), len(result.jobs)

    feed = existing
    previous_method = feed.method if feed else None
    if feed is None:
        feed = CompanyFeed(company_id=company.id, activated_at=now)
        session.add(feed)

    if chosen:
        cand, evidence = chosen
        feed.method, feed.slug, feed.site, feed.host = cand.spec.method, cand.spec.slug, cand.spec.site, cand.spec.host
        feed.ownership_evidence = f"{cand.how} — {evidence}"
    else:
        feed.method, feed.slug, feed.site, feed.host = "aggregator", None, None, None
        n = len(aggregator_jobs(session, company.id))
        note = "custom careers page (browser reader not built yet)" if discovery else "no careers page found"
        feed.ownership_evidence = f"aggregator floor: {note}; {n} recent job-site listings"
        if attempts:
            feed.ownership_evidence += " | tried: " + "; ".join(attempts[:10])
    feed.careers_url = discovery.careers_url if discovery else feed.careers_url
    feed.verified_at = now
    feed.needs_reactivation = False
    feed.consecutive_failures = 0
    feed.health = "pending"
    feed.cadence = feed.cadence or "active"
    session.flush()

    company.status = STATUS_WATCHING
    verb = "Re-activated" if previous_method else "Watching"
    change = f" (was {previous_method})" if previous_method and previous_method != feed.method else ""
    audit.record(
        session, "m1", "feed_activated",
        f"{verb} {company.name} via {feed.method}{change}",
        subject_type="company", subject_id=str(company.id), is_judgment=feed.method != "aggregator",
        detail={"feed_id": feed.id, "method": feed.method, "slug": feed.slug, "careers_url": feed.careers_url,
                "attempts": attempts,
                "evidence": feed.ownership_evidence, "discovery": discovery.how if discovery else None},
    )
    first = sync_feed(session, http, feed)  # first sync immediately
    return feed, first
