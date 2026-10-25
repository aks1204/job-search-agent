"""Weekly gardening (spec §1.5): cadence, health/self-healing, staleness, pruning, fact refreshes."""

import logging
from collections import Counter
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config_loader import get_config
from app.core import audit
from app.core.http import PoliteClient
from app.models.company import (
    STATUS_ARCHIVED,
    STATUS_DORMANT,
    STATUS_PASSED,
    STATUS_REJECTED,
    STATUS_WATCHING,
    Company,
    CompanySighting,
)
from app.models.feed import CompanyFeed, RawPosting
from app.modules.m1_companies.enrich import enrich
from app.modules.m1_companies.identity import normalize_name
from app.modules.m1_companies.revival import on_facts_changed
from app.modules.m1_companies.watch.sync import AGGREGATOR_SCOUTS

log = logging.getLogger(__name__)


def _quiet_since(feed: CompanyFeed) -> datetime:
    return feed.last_change_at or feed.activated_at


def tune_cadence(session: Session, now: datetime) -> Counter:
    """Active (6-hourly) ↔ dormant (weekly). Promote on any sign of life."""
    cfg, counts = get_config().watch, Counter()
    week_ago = now - timedelta(days=7)
    for feed in session.scalars(select(CompanyFeed)).all():
        recent_signal = session.scalar(
            select(CompanySighting.id).where(CompanySighting.company_id == feed.company_id,
                                             CompanySighting.last_seen_at >= week_ago).limit(1)
        )
        quiet = now - _quiet_since(feed) > timedelta(days=cfg.dormant_after_quiet_days)
        if feed.cadence == "active" and quiet and not recent_signal:
            feed.cadence = "dormant"
            counts["to_dormant"] += 1
        elif feed.cadence == "dormant" and (not quiet or recent_signal):
            feed.cadence = "active"
            feed.next_sync_at = now  # read soon
            counts["to_active"] += 1
    return counts


def _titles(rows) -> set[str]:
    return {normalize_name(t) for t in rows if t}


def check_staleness(session: Session, now: datetime) -> Counter:
    """Quiet breakage: job sites show this company's jobs that our feed doesn't."""
    cfg, counts = get_config().watch, Counter()
    since = now - timedelta(days=14)
    feeds = session.scalars(select(CompanyFeed).where(CompanyFeed.method != "aggregator",
                                                      CompanyFeed.needs_reactivation.is_(False))).all()
    for feed in feeds:
        ours = _titles(session.scalars(select(RawPosting.title).where(   # the careers page's own jobs only
            RawPosting.feed_id == feed.id, RawPosting.closed_at.is_(None), ~RawPosting.external_id.like("hn:%"))))
        theirs = _titles((s.data or {}).get("title") for s in session.scalars(select(CompanySighting).where(
            CompanySighting.company_id == feed.company_id, CompanySighting.scout.in_(AGGREGATOR_SCOUTS),
            CompanySighting.last_seen_at >= since)))
        missing = [t for t in theirs if not any(t in o or o in t for o in ours)]
        if len(missing) >= cfg.stale_aggregator_jobs:
            feed.health, feed.needs_reactivation = "stale", True
            counts["stale"] += 1
            audit.record(session, "m1", "feed_stale",
                         f"Feed looks stale: job sites list {len(missing)} roles our {feed.method} feed doesn't",
                         subject_type="company", subject_id=str(feed.company_id), detail={"missing": missing[:10]})
    return counts


def flag_reverification(session: Session, now: datetime) -> Counter:
    cfg = get_config().watch
    cutoff = now - timedelta(days=cfg.reverify_days)
    counts = Counter()
    for feed in session.scalars(select(CompanyFeed).where(CompanyFeed.verified_at < cutoff,
                                                          CompanyFeed.needs_reactivation.is_(False))).all():
        feed.needs_reactivation = True
        counts["reverify"] += 1
    return counts


def prune(session: Session, now: datetime) -> Counter:
    """90 days silent → dormant; a further 90 → archived (no more reads). New signals revive."""
    cfg, counts = get_config().watch, Counter()
    rows = session.execute(
        select(Company, CompanyFeed).join(CompanyFeed, CompanyFeed.company_id == Company.id)
        .where(Company.status.in_((STATUS_WATCHING, STATUS_DORMANT, STATUS_ARCHIVED)))
    ).all()
    for company, feed in rows:
        silent = now - _quiet_since(feed)
        alive = feed.cadence == "active"  # tune_cadence (run first) sets this on recent jobs or sightings
        if company.status == STATUS_WATCHING and not alive and silent > timedelta(days=cfg.demote_after_silent_days):
            company.status = STATUS_DORMANT
            counts["demoted"] += 1
        elif company.status == STATUS_DORMANT and not alive and silent > timedelta(days=cfg.archive_after_silent_days):
            company.status = STATUS_ARCHIVED
            counts["archived"] += 1
        elif company.status in (STATUS_DORMANT, STATUS_ARCHIVED) and alive:
            company.status = STATUS_WATCHING
            counts["revived"] += 1
    return counts


def refresh_facts(session: Session, http: PoliteClient, llm, now: datetime) -> Counter:
    """Startup cards monthly, MNC quarterly, rejects in a slow lane (may revive them)."""
    cfg, counts = get_config().watch, Counter()
    watched = (STATUS_PASSED, STATUS_WATCHING, STATUS_DORMANT)
    due = []
    for company in session.scalars(select(Company).where(Company.status.in_((*watched, STATUS_REJECTED)),
                                                         Company.enriched_at.is_not(None))).all():
        if company.status == STATUS_REJECTED:
            days = cfg.refresh_rejected_days
        elif company.type == "established":
            days = cfg.refresh_established_days
        else:
            days = cfg.refresh_startup_days
        if now - company.enriched_at > timedelta(days=days):
            due.append(company)
    for company in sorted(due, key=lambda c: c.enriched_at)[: cfg.refresh_per_run]:
        try:
            changed = enrich(session, company, llm=llm, http=http, profile=company.type if company.type != "unknown" else "startup")
        except Exception as e:
            log.info("refresh failed for %s: %s", company.name, e)
            counts["refresh_errors"] += 1
            continue
        counts["refreshed"] += 1
        if company.status == STATUS_REJECTED and on_facts_changed(session, company, changed):
            counts["revived_rejects"] += 1
    return counts


def garden(session: Session, http: PoliteClient, llm=None) -> dict:
    now = datetime.now(timezone.utc)
    counts: Counter = Counter()
    for step in (tune_cadence, check_staleness, flag_reverification, prune):
        counts.update(step(session, now))
    if llm is not None:
        counts.update(refresh_facts(session, http, llm, now))
    return dict(counts)
