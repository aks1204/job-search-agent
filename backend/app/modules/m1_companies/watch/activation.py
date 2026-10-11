"""Activation (spec §1.5): kept company → careers page → reading method → ownership check → tracking record → first sync.

Idempotent: re-running it on a company with a feed updates that feed in place,
which is also how gardening heals broken feeds and ATS migrations.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config_loader import get_config
from app.core import audit
from app.core.http import PoliteClient
from app.core.hosts import domain_label
from app.models.company import STATUS_WATCHING, Company
from app.models.feed import CompanyFeed
from app.modules.m1_companies.identity import normalize_name
from app.modules.m1_companies.watch.careers import Discovery, discover
from app.modules.m1_companies.watch.ownership import verify_ownership
from app.modules.m1_companies.watch.readers import READERS, FeedNotFound, FeedSpec, FetchResult, read_adapter, read_workday
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


def slug_guesses(company: Company) -> list[str]:
    words = normalize_name(company.name).split()
    guesses = [domain_label(company.domain or ""), "".join(words), "-".join(words)]
    return [g for g in dict.fromkeys(guesses) if g and len(g) >= 2][:3]


def _candidates(company: Company, discovery: Discovery | None) -> list[FeedCandidate]:
    found: list[FeedCandidate] = []
    if discovery:
        for h in discovery.scan.ats_hints:
            found.append(FeedCandidate(FeedSpec(h["vendor"], h["slug"], h.get("site"), h.get("host")),
                                   discovery.on_company_site, f"{discovery.how}: {h['vendor']}/{h['slug']}"))
    if company.domain in get_config().adapters:
        found.append(FeedCandidate(FeedSpec("adapter"), True, "community adapter"))
    if not found:
        for vendor in GUESSABLE:
            for slug in slug_guesses(company):
                found.append(FeedCandidate(FeedSpec(vendor, slug), False, f"guessed {vendor}/{slug}"))
    return sorted(found, key=lambda c: LADDER.index(c.spec.method))


def _try(http: PoliteClient, company: Company, cand: FeedCandidate) -> FetchResult | None:
    cfg = get_config()
    roles = list(cfg.owner.target_roles) if cfg.owner else []
    try:
        if cand.spec.method == "adapter":
            return read_adapter(http, cfg.adapters[company.domain])
        if cand.spec.method == "workday":
            return read_workday(http, cand.spec, roles, pages_per_role=1)
        return READERS[cand.spec.method](http, cand.spec, roles)
    except FeedNotFound:
        return None
    except Exception as e:
        log.info("candidate %s failed for %s: %s", cand.how, company.name, e)
        return None


def activate(session: Session, company: Company, http: PoliteClient, *, llm=None) -> tuple[CompanyFeed, SyncResult]:
    now = datetime.now(timezone.utc)
    discovery = discover(http, company, llm=llm, session=session) if company.domain else None

    chosen: tuple[FeedCandidate, str] | None = None
    best_jobs = -1
    rejected_evidence: list[str] = []
    for cand in _candidates(company, discovery):
        result = _try(http, company, cand)
        if result is None:
            continue
        ok, evidence = verify_ownership(company, result, linked_from_site=cand.linked_from_site,
                                        careers_url=discovery.careers_url if discovery else None)
        if not ok:
            rejected_evidence.append(f"{cand.how}: {evidence}")
            continue
        # A company's site can link several boards (e.g. a subsidiary's small one next to the main
        # Workday site). Among verified boards, keep the biggest; the ladder order breaks ties.
        if len(result.jobs) > best_jobs:
            chosen, best_jobs = (cand, f"{evidence}; {len(result.jobs)} jobs"), len(result.jobs)
        if not cand.linked_from_site:
            break  # guessed boards: first verified one is enough

    feed = session.scalar(select(CompanyFeed).where(CompanyFeed.company_id == company.id))
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
        if rejected_evidence:
            feed.ownership_evidence += " | rejected: " + "; ".join(rejected_evidence[:3])
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
                "evidence": feed.ownership_evidence, "discovery": discovery.how if discovery else None},
    )
    first = sync_feed(session, http, feed)  # first sync immediately
    return feed, first
