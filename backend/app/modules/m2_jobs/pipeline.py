"""Part 2a pipeline: raw postings → canonical job postings → sibling clusters → Kind-1 filters → descriptions.

Idempotent: re-syncs only move last_seen; a material content change (new hash) re-normalises the
posting, marks it changed (so Part 2c re-scores it once) and re-runs the filters.
"""

import logging
from collections import Counter
from datetime import datetime, timezone

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from app.config_loader import get_config
from app.core.http import PoliteClient
from app.db import session_scope
from app.models.company import Company
from app.models.feed import CompanyFeed, RawPosting
from app.models.job import JobCluster, JobPosting
from app.modules.m2_jobs import normalize as N
from app.modules.m2_jobs.details import FETCHERS, fetch_description
from app.modules.m2_jobs.filters import TitleNet, kind1
from app.settings import redact

log = logging.getLogger(__name__)

INTAKE_BATCH = 2000
MIN_DESCRIPTION = 200  # shorter than this isn't a real description (aggregator snippets, titles)


def _apply_raw(job: JobPosting, raw: RawPosting, method: str) -> None:
    job.method = method
    job.external_id = raw.external_id
    job.title = raw.title
    job.title_norm = N.normalize_title(raw.title)
    job.location = raw.location
    job.is_india, job.is_remote = N.location_flags(raw.location)
    job.employment_type = N.employment_type(raw.employment_type, raw.title)
    job.posted_at = N.parse_posted(raw.posted_at, raw.first_seen_at)
    job.salary = N.salary_from(raw.payload)
    job.url = raw.url
    job.lead_only = raw.lead_only
    job.fingerprint = N.fingerprint(raw.title, raw.location)
    job.content_hash = raw.content_hash
    if raw.description and len(raw.description) >= MIN_DESCRIPTION:
        job.description, job.description_status = raw.description, "present"
    elif method in FETCHERS:
        job.description, job.description_status = raw.description, "pending"
    else:
        job.description, job.description_status = raw.description, "unavailable"


def _cluster_for(session: Session, job: JobPosting, cache: dict) -> JobCluster:
    key = (job.company_id, job.fingerprint)
    if key not in cache:
        cluster = session.scalar(select(JobCluster).where(
            JobCluster.company_id == job.company_id, JobCluster.fingerprint == job.fingerprint))
        if cluster is None:
            cluster = JobCluster(company_id=job.company_id, fingerprint=job.fingerprint, title=job.title)
            session.add(cluster)
            session.flush()
        cache[key] = cluster
    return cache[key]


def _refresh_clusters(session: Session, cluster_ids: set[int]) -> None:
    """Size = open postings; canonical = oldest open non-lead posting (a lead only if nothing better)."""
    for cid in cluster_ids:
        members = session.scalars(
            select(JobPosting).where(JobPosting.cluster_id == cid, JobPosting.status == "open")
            .order_by(JobPosting.lead_only, JobPosting.first_seen_at, JobPosting.id)
        ).all()
        cluster = session.get(JobCluster, cid)
        cluster.size = len(members)
        cluster.canonical_job_id = members[0].id if members else None
        if members:
            cluster.title = members[0].title


def intake(session: Session, now: datetime | None = None, company_ids: list[int] | None = None) -> Counter:
    now = now or datetime.now(timezone.utc)
    cfg = get_config()
    net = TitleNet.build(cfg.owner, cfg.job_rules)
    counts: Counter = Counter()
    scope = [RawPosting.company_id.in_(company_ids)] if company_ids else []

    # Re-seen and unchanged: only last_seen moves (spec: "re-syncs update last_seen only").
    session.execute(
        update(JobPosting).where(JobPosting.raw_posting_id == RawPosting.id,
                                 JobPosting.last_seen_at < RawPosting.last_seen_at, *scope)
        .values(last_seen_at=RawPosting.last_seen_at).execution_options(synchronize_session=False)
    )

    rows = session.execute(
        select(RawPosting, CompanyFeed.method, JobPosting)
        .join(CompanyFeed, CompanyFeed.id == RawPosting.feed_id)
        .outerjoin(JobPosting, JobPosting.raw_posting_id == RawPosting.id)
        .where(or_(
            JobPosting.id.is_(None),
            JobPosting.content_hash != RawPosting.content_hash,
            RawPosting.closed_at.is_(None) != (JobPosting.status == "open"),
        ), *scope)
        .limit(INTAKE_BATCH)
    ).all()

    clusters: dict = {}
    touched: set[int] = set()
    for raw, method, job in rows:
        if job is None:
            job = JobPosting(raw_posting_id=raw.id, company_id=raw.company_id,
                             first_seen_at=raw.first_seen_at, last_seen_at=raw.last_seen_at)
            _apply_raw(job, raw, method)
            session.add(job)
            counts["new"] += 1
            refilter = True
        elif job.content_hash != raw.content_hash:
            old_cluster = job.cluster_id
            _apply_raw(job, raw, method)
            job.changed_at = now  # Part 2c re-scores once on material change
            counts["changed"] += 1
            refilter = True
            if old_cluster:
                touched.add(old_cluster)
        else:
            refilter = False

        if raw.closed_at is not None and job.status == "open":
            job.status, job.closed_at = "closed", raw.closed_at
            counts["closed"] += 1
        elif raw.closed_at is None and job.status == "closed":
            job.status, job.closed_at = "open", None
            counts["reopened"] += 1

        cluster = _cluster_for(session, job, clusters)
        job.cluster_id = cluster.id
        touched.add(cluster.id)

        if refilter:
            reasons = kind1(job, net, cfg.job_rules, now)
            job.filter_status, job.filter_reasons = ("failed", reasons) if reasons else ("passed", [])
            counts["passed" if not reasons else "filtered_out"] += 1

    session.flush()
    _refresh_clusters(session, touched)
    return counts


def fetch_descriptions(session: Session, http: PoliteClient, limit: int | None = None) -> Counter:
    """Fetch full text for jobs that passed Kind 1 and arrived without one. Re-checks Kind 1 after
    (the detail page can reveal e.g. a contract role)."""
    cfg = get_config()
    net = TitleNet.build(cfg.owner, cfg.job_rules)
    limit = limit or cfg.job_rules.descriptions.per_run
    counts: Counter = Counter()
    rows = session.execute(
        select(JobPosting, CompanyFeed)
        .join(RawPosting, RawPosting.id == JobPosting.raw_posting_id)
        .join(CompanyFeed, CompanyFeed.id == RawPosting.feed_id)
        .join(Company, Company.id == JobPosting.company_id)
        .where(JobPosting.filter_status == "passed", JobPosting.status == "open",
               JobPosting.description_status == "pending", Company.status != "blocked")
        .order_by(JobPosting.first_seen_at.desc()).limit(limit)
    ).all()
    cache: dict = {}
    for job, feed in rows:
        try:
            text, extra = fetch_description(http, feed, job, cache)
        except Exception as e:
            job.description_status = "failed"
            counts["failed"] += 1
            log.info("description fetch failed for job %s: %s", job.id, redact(str(e))[:200])
            continue
        if text and len(text) >= MIN_DESCRIPTION:
            job.description, job.description_status = text, "fetched"
            counts["fetched"] += 1
        else:
            job.description_status = "unavailable"
            counts["empty"] += 1
        if job.employment_type == "unknown" and extra.get("employment"):
            job.employment_type = N.employment_type(extra["employment"], job.title)
        if job.posted_at is None and extra.get("posted"):
            job.posted_at = N.parse_posted(extra["posted"], job.first_seen_at)
        if reasons := kind1(job, net, cfg.job_rules):
            job.filter_status, job.filter_reasons = "failed", reasons
            counts["filtered_out_after_details"] += 1
    return counts


def refilter_all(session: Session) -> Counter:
    """Re-apply Kind 1 to every open posting (after owner_stub / job_rules edits + restart)."""
    cfg = get_config()
    net = TitleNet.build(cfg.owner, cfg.job_rules)
    counts: Counter = Counter()
    for job in session.scalars(select(JobPosting).where(JobPosting.status == "open")).all():
        reasons = kind1(job, net, cfg.job_rules)
        new = ("failed", reasons) if reasons else ("passed", [])
        if (job.filter_status, job.filter_reasons) != new:
            job.filter_status, job.filter_reasons = new
            counts["changed"] += 1
    return counts


def run_pipeline(*, assess: bool = True) -> dict:
    from app.modules.m2_jobs.assess import run_assessments

    counts: dict = {}
    with session_scope() as s:
        counts.update({f"intake_{k}": v for k, v in intake(s).items()})
    with PoliteClient() as http, session_scope() as s:
        counts.update({f"descriptions_{k}": v for k, v in fetch_descriptions(s, http).items()})
    if assess:
        from app.modules.m2_jobs.scoring import run_scoring

        counts.update({f"ai_read_{k}": v for k, v in run_assessments().items()})
        counts.update({f"score_{k}": v for k, v in run_scoring().items()})
    return counts


def funnel(session: Session) -> dict:
    """Where postings stand, and the most common reasons they were filtered out."""
    open_jobs = select(JobPosting).where(JobPosting.status == "open").subquery()
    by_filter = dict(session.execute(select(open_jobs.c.filter_status, func.count()).group_by(open_jobs.c.filter_status)).all())
    passed = select(JobPosting).where(JobPosting.status == "open", JobPosting.filter_status == "passed").subquery()
    by_desc = dict(session.execute(select(passed.c.description_status, func.count()).group_by(passed.c.description_status)).all())
    reasons: Counter = Counter()
    for (rs,) in session.execute(select(JobPosting.filter_reasons).where(
            JobPosting.status == "open", JobPosting.filter_status == "failed")).all():
        for r in rs or []:
            reasons["posted too long ago" if r.startswith("posted ") else r] += 1
    clusters = session.scalar(select(func.count()).select_from(JobCluster).where(JobCluster.size > 1)) or 0
    leads = session.scalar(select(func.count()).select_from(JobPosting).where(
        JobPosting.status == "open", JobPosting.lead_only.is_(True))) or 0

    from app.models.assessment import JobAssessment

    by_outcome = dict(session.execute(select(JobAssessment.outcome, func.count()).group_by(JobAssessment.outcome)).all())
    ai_reasons: Counter = Counter()
    for (rs,) in session.execute(select(JobAssessment.reasons).where(JobAssessment.outcome == "rejected")).all():
        for r in rs or []:
            ai_reasons[r.split(" (")[0].split(":")[0]] += 1  # "family: …", "lateral (…)", "not doable … (“Location: …”)"
    return {
        "open_postings": sum(by_filter.values()),
        "by_filter_status": by_filter,
        "passed_by_description": by_desc,
        "top_filter_reasons": reasons.most_common(8),
        "sibling_clusters": clusters,
        "open_leads": leads,
        "ai_read_by_outcome": by_outcome,
        "top_ai_reject_reasons": ai_reasons.most_common(8),
    }
