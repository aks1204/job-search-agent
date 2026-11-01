"""Activation (spec §1.5): kept company → careers page → reading method → ownership check → tracking record → first sync.

Idempotent: re-running it on a company with a feed updates that feed in place,
which is also how gardening heals broken feeds and ATS migrations.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config_loader import get_config
from app.core import audit
from app.core.http import PoliteClient
from app.core.hosts import PLATFORMS, ats_hint, domain_label
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

# The ladder: public ATS endpoints → Workday / Oracle → per-company adapters → aggregator floor.
LADDER = ("greenhouse", "lever", "ashby", "smartrecruiters", "workable", "recruitee", "teamtailor", "pinpoint", "jazzhr",
          "workday", "oracle", "eightfold", "jibe", "phenom", "talentbrew", "adapter")
GUESSABLE = ("greenhouse", "lever", "ashby", "smartrecruiters")
KEEP_IF_WORKED = timedelta(days=7)   # an unreachable previous board is kept only if it worked this recently


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


def _spec(hint: dict, company: Company) -> FeedSpec:
    site = hint.get("site")
    if hint["vendor"] == "eightfold":
        site = company.domain  # Eightfold's search API is keyed by the employer's domain
    return FeedSpec(hint["vendor"], hint["slug"], site, hint.get("host"))


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
    hn = (company.source_data or {}).get("hn") or {}
    for url in [hn.get("careers_url"), *(hn.get("board_links") or [])]:
        if h := ats_hint(url):   # the company's own HN post links this board: as good as a link on their site
            found.append(FeedCandidate(_spec(h, company), True, f"linked in their Hacker News post: {_label(h)}"))
    if discovery:
        for h in discovery.scan.ats_hints:
            # A careers-site platform (Phenom, Jibe, TalentBrew) is only the front: a real job system behind it
            # (e.g. Workday) is tried first and wins.
            found.append(FeedCandidate(_spec(h, company), discovery.on_company_site, f"{discovery.how}: {_label(h)}",
                                       source_rank=1 if h["vendor"] in PLATFORMS else 0))
    if company.domain in get_config().adapters:
        found.append(FeedCandidate(FeedSpec("adapter"), True, "community adapter", source_rank=1))
    if not company.domain:
        pass  # name-only (from a Hacker News post): only boards the post links — no guesses to check ownership by
    elif not any(c.linked_from_site for c in found):  # only pay for AI suggestions when the site gave us nothing
        for h in _ai_suggestions(company, llm, session, notes):
            found.append(FeedCandidate(_spec(h, company), False,
                                       f"AI-suggested {_label(h)}", source_rank=2))
    for vendor in GUESSABLE if company.domain else ():  # last resort; skipped once a better one is confirmed
        for slug in slug_guesses(company):
            found.append(FeedCandidate(FeedSpec(vendor, slug), False, f"guessed {vendor}/{slug}", source_rank=3))
    unique = {(c.spec.method, (c.spec.slug or "").lower(), c.spec.site): c for c in reversed(found)}
    return sorted(unique.values(), key=lambda c: (c.source_rank, LADDER.index(c.spec.method)))


def _transient(e: Exception) -> bool:
    """A failure that's only temporary — no connection, a timeout, the server busy — not "this board is gone"."""
    if isinstance(e, httpx.TransportError):
        return True
    return isinstance(e, httpx.HTTPStatusError) and (e.response.status_code >= 500 or e.response.status_code == 429)


def _try(http: PoliteClient, company: Company, cand: FeedCandidate) -> tuple[FetchResult | None, str, bool]:
    """(result, what happened, whether a failure was only temporary) — the note is kept so a fallback can
    explain itself."""
    cfg = get_config()
    roles = list(cfg.owner.target_roles) if cfg.owner else []
    try:
        if cand.spec.method == "adapter":
            result = read_adapter(http, cfg.adapters[company.domain], roles)
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
                    return None, f"{first}; {'; '.join(probes)}", _transient(first_error)
                old = cand.spec.host
                cand.spec.host = host
                cand.how += f" (server {old} → {host})"
                result = read_workday(http, cand.spec, roles, pages_per_role=1)
        else:
            result = READERS[cand.spec.method](http, cand.spec, roles)
    except FeedNotFound:
        return None, "not found", False
    except httpx.HTTPStatusError as e:
        return None, f"HTTP {e.response.status_code}", _transient(e)
    except Exception as e:
        log.info("candidate %s failed for %s: %s", cand.how, company.name, e)
        return None, f"{type(e).__name__}: {str(e)[:80]}", _transient(e)
    return result, f"{len(result.jobs)} jobs", False


def activate(session: Session, company: Company, http: PoliteClient, *, llm=None) -> tuple[CompanyFeed, SyncResult]:
    now = datetime.now(timezone.utc)
    vendors: set[str] = set()
    discovery = discover(http, company, llm=llm, session=session, vendors_seen=vendors) if company.domain else None

    chosen: tuple[FeedCandidate, str] | None = None
    best_jobs = -1
    attempts: list[str] = []  # every board tried and what happened — shown when we fall back
    size = value(fact_card(session, company.id), "employee_estimate")
    existing = session.scalar(select(CompanyFeed).where(CompanyFeed.company_id == company.id))
    previous_unreachable = False
    for cand in _candidates(company, discovery, llm=llm, session=session, notes=attempts, previous=existing):
        if chosen and not cand.linked_from_site:
            break  # never let a guess displace a board the company itself links to
        if chosen and cand.spec.method in PLATFORMS and chosen[0].spec.method not in PLATFORMS:
            continue  # a real job system was found: the careers-site front in front of it isn't needed
        result, note, transient = _try(http, company, cand)
        if result is None:
            attempts.append(f"{cand.how} → {note}")
            if cand.how.startswith("previous") and transient:
                previous_unreachable = True   # the board that worked couldn't be reached right now (not gone)
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

    # The board that worked (within the last week) couldn't be reached just now — laptop waking up, a site
    # hiccup: keep it rather than falling back to job-site listings; the next re-check tries again. A board
    # that keeps failing for over a week is let go as usual.
    keep_previous = bool(not chosen and previous_unreachable and existing is not None
                         and existing.method not in ("aggregator", "adapter")
                         and existing.last_success_at is not None and now - existing.last_success_at < KEEP_IF_WORKED)
    if chosen:
        cand, evidence = chosen
        feed.method, feed.slug, feed.site, feed.host = cand.spec.method, cand.spec.slug, cand.spec.site, cand.spec.host
        feed.ownership_evidence = f"{cand.how} — {evidence}"
    elif keep_previous:
        kept = f"kept the previous {existing.method} board — it couldn't be reached during this re-check"
        feed.ownership_evidence = f"{(existing.ownership_evidence or '').split(' | kept')[0]} | {kept}"[:2000]
        attempts.append(kept)
    else:
        feed.method, feed.slug, feed.site, feed.host = "aggregator", None, None, None
        n = len(aggregator_jobs(session, company.id))
        unreadable = sorted(vendors - set(READERS))
        if unreadable:
            note = f"jobs are on {', '.join(unreadable)} (no reader yet)"
        elif not company.domain:
            note = "no website in their Hacker News post (jobs from the post itself)"
        elif discovery:
            note = "custom careers page (browser reader not built yet)"
        else:
            note = "no careers page found"
        feed.ownership_evidence = f"aggregator floor: {note}; {n} recent job-site listings"
        if attempts:
            feed.ownership_evidence += " | tried: " + "; ".join(attempts[:10])
    feed.careers_url = discovery.careers_url if discovery else feed.careers_url
    feed.detected_vendors = sorted(vendors | ({feed.method} if feed.method != "aggregator" else set()))
    feed.needs_reactivation = False
    if not keep_previous:   # a kept board wasn't re-verified: its failure count, last check and health stand
        feed.verified_at = now
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
    if keep_previous:
        # It just failed to answer: syncing again now would fail, hit the failure limit and queue another full
        # re-check every tick. Wait as a failed sync would; that next sync decides.
        feed.next_sync_at = now + timedelta(hours=min(2 ** feed.consecutive_failures, 48))
        return feed, SyncResult(error="previous board unreachable; kept, next try later")
    first = sync_feed(session, http, feed)  # first sync immediately
    return feed, first
