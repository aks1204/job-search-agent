"""Reading a feed into raw_postings: new jobs appear, changed ones update, missing ones close."""

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config_loader import get_config
from app.core.http import PoliteClient
from app.models.company import Company, CompanySighting
from app.models.feed import CompanyFeed, RawPosting
from app.modules.m1_companies.facts import _INDIA, upsert_fact
from app.modules.m1_companies.watch.readers import READERS, FeedSpec, RawJob, read_adapter, read_workday
from app.settings import redact

log = logging.getLogger(__name__)

AGGREGATOR_SCOUTS = ("adzuna", "jooble", "remotive")  # job sites (also used by the staleness cross-check)
# Every source whose sightings ARE job posts. HN "Who is hiring" comments and Work at a Startup listings
# are often a small startup's only job ad, so they count as leads too.
LEAD_SCOUTS = (*AGGREGATOR_SCOUTS, "waas", "hn")
LEAD_WINDOW_DAYS = {"hn": 45}  # monthly thread: posts stay relevant longer
AGGREGATOR_WINDOW_DAYS = 21


def _hn_role(headline: str) -> tuple[str, str | None]:
    """'Acme | Senior Data Analyst | Remote (India) | Full-time' → ('Senior Data Analyst', 'Remote (India)')."""
    parts = [p.strip() for p in headline.split("|")]
    title = parts[1] if len(parts) > 1 and parts[1] else headline
    return title[:300], (parts[2] if len(parts) > 2 else None)


@dataclass
class SyncResult:
    fetched: int = 0
    new: int = 0
    changed: int = 0
    closed: int = 0
    reopened: int = 0
    error: str | None = None


def _hash(job: RawJob) -> str:
    return hashlib.sha256(f"{job.title}|{job.location}|{job.description or ''}".encode()).hexdigest()[:32]


def aggregator_jobs(session: Session, company_id: int) -> list[RawJob]:
    """The guaranteed floor: job posts seen for this company recently — job sites, Work at a Startup,
    HN 'Who is hiring' comments. Leads only: Module 2 never acts on them without a canonical posting."""
    now = datetime.now(timezone.utc)
    rows = session.scalars(
        select(CompanySighting).where(
            CompanySighting.company_id == company_id,
            CompanySighting.scout.in_(LEAD_SCOUTS),
            CompanySighting.last_seen_at >= now - timedelta(days=max(LEAD_WINDOW_DAYS.values())),
        )
    ).all()
    jobs = []
    for s in rows:
        if s.last_seen_at < now - timedelta(days=LEAD_WINDOW_DAYS.get(s.scout, AGGREGATOR_WINDOW_DAYS)):
            continue
        d = s.data or {}
        if s.scout == "hn":
            title, location = _hn_role(d.get("headline") or "")
            description = d.get("text")
        else:
            title, location, description = d.get("title") or "(untitled)", d.get("location"), d.get("description")
        jobs.append(RawJob(
            external_id=f"{s.scout}:{s.external_id}"[:255], title=title, location=location, url=s.url,
            posted_at=str(d.get("created") or d.get("published") or d.get("posted") or "") or None,
            description=description, payload={"scout": s.scout, "salary": d.get("salary")},
        ))
    return jobs


def fetch_jobs(session: Session, http: PoliteClient, feed: CompanyFeed, company: Company) -> list[RawJob]:
    cfg = get_config()
    if feed.method == "aggregator":
        return aggregator_jobs(session, company.id)
    if feed.method == "adapter":
        adapter = cfg.adapters.get(company.domain or "")
        if adapter is None:
            raise RuntimeError(f"adapter for {company.domain} no longer exists in config/adapters")
        return read_adapter(http, adapter).jobs
    spec = FeedSpec(feed.method, feed.slug, feed.site, feed.host)
    roles = list(cfg.owner.target_roles) if cfg.owner else []
    if feed.method == "workday":
        return read_workday(http, spec, roles, pages_per_role=cfg.watch.workday_pages_per_role).jobs
    return READERS[feed.method](http, spec, roles).jobs


def apply_jobs(session: Session, feed: CompanyFeed, jobs: list[RawJob], now: datetime) -> SyncResult:
    result = SyncResult(fetched=len(jobs))
    existing = {p.external_id: p for p in session.scalars(select(RawPosting).where(RawPosting.feed_id == feed.id))}
    seen = set()
    for job in jobs:
        if job.external_id in seen:
            continue
        seen.add(job.external_id)
        digest = _hash(job)
        row = existing.get(job.external_id)
        if row is None:
            session.add(RawPosting(
                feed_id=feed.id, company_id=feed.company_id, external_id=job.external_id, title=job.title,
                location=job.location, url=job.url, department=job.department,
                employment_type=job.employment_type, posted_at=job.posted_at, description=job.description,
                payload=job.payload, content_hash=digest, lead_only=feed.method == "aggregator",
                first_seen_at=now, last_seen_at=now,
            ))
            result.new += 1
            continue
        row.last_seen_at = now
        if row.closed_at is not None:
            row.closed_at = None
            result.reopened += 1
        if row.content_hash != digest:
            row.title, row.location, row.url, row.description = job.title, job.location, job.url, job.description
            row.department, row.employment_type, row.posted_at = job.department, job.employment_type, job.posted_at
            row.payload, row.content_hash, row.changed_at = job.payload, digest, now
            result.changed += 1
    for ext, row in existing.items():
        if ext not in seen and row.closed_at is None:
            row.closed_at = now
            result.closed += 1
    return result


def sync_feed(session: Session, http: PoliteClient, feed: CompanyFeed) -> SyncResult:
    cfg = get_config().watch
    company = session.get(Company, feed.company_id)
    now = datetime.now(timezone.utc)
    feed.last_sync_at = now
    try:
        jobs = fetch_jobs(session, http, feed, company)
    except Exception as e:  # loud break: back off, and after N failures gardening re-activates
        feed.consecutive_failures += 1
        feed.health = "failing"
        feed.last_error = redact(f"{type(e).__name__}: {e}")[:1000]
        feed.next_sync_at = now + timedelta(hours=min(2 ** feed.consecutive_failures, 48))
        if feed.consecutive_failures >= cfg.reactivate_after_failures:
            feed.needs_reactivation = True
        log.warning("sync failed for %s (%s): %s", company.name, feed.method, feed.last_error)
        return SyncResult(error=feed.last_error)

    result = apply_jobs(session, feed, jobs, now)
    feed.open_jobs = len({j.external_id for j in jobs})
    feed.consecutive_failures = 0
    feed.last_error = None
    feed.last_success_at = now
    if feed.health in ("pending", "failing"):
        feed.health = "ok"
    if result.new or result.closed or result.changed or result.reopened:
        feed.last_change_at = now
    interval = timedelta(hours=cfg.active_sync_hours) if feed.cadence == "active" else timedelta(days=cfg.dormant_sync_days)
    feed.next_sync_at = now + interval

    # Established companies get their India-presence check from the careers feed (spec §1.3).
    if any(j.location and _INDIA.search(j.location) for j in jobs) and feed.method != "aggregator":
        upsert_fact(session, company.id, "india_presence", "yes", "verified", "careers")
    return result
