"""Scheduler entry points for the watchlist: activation, sync, gardening. One transaction per company."""

import logging
from collections import Counter
from datetime import datetime, timezone

from sqlalchemy import or_, select

from app.config_loader import get_config
from app.core import audit
from app.core.http import PoliteClient
from app.core.llm import LLM
from app.db import session_scope
from app.models.company import STATUS_DORMANT, STATUS_PASSED, STATUS_WATCHING, Company
from app.models.feed import CompanyFeed
from app.modules.m1_companies.watch.activation import activate
from app.modules.m1_companies.watch.gardening import garden
from app.modules.m1_companies.watch.sync import sync_feed
from app.settings import redact

log = logging.getLogger(__name__)


def _llm():
    return LLM() if LLM.available() else None


def run_activation_batch(*, limit: int | None = None, company_ids: list[int] | None = None) -> dict:
    """Kept companies without a feed, plus feeds gardening flagged for re-activation."""
    n = limit or get_config().watch.activation_batch
    with session_scope() as s:
        if company_ids:
            ids = company_ids
        else:
            no_feed = select(Company.id).outerjoin(CompanyFeed, CompanyFeed.company_id == Company.id).where(
                Company.status == STATUS_PASSED, CompanyFeed.id.is_(None)
            ).order_by(Company.updated_at)
            healing = select(CompanyFeed.company_id).join(Company, Company.id == CompanyFeed.company_id).where(
                CompanyFeed.needs_reactivation.is_(True),
                Company.status.in_((STATUS_PASSED, STATUS_WATCHING, STATUS_DORMANT)),
            )
            ids = list(s.scalars(no_feed.limit(n))) + list(s.scalars(healing.limit(n)))
            ids = list(dict.fromkeys(ids))[:n]

    counts: Counter = Counter()
    llm = _llm()
    with PoliteClient() as http:
        for company_id in ids:
            try:
                with session_scope() as s:
                    company = s.get(Company, company_id)
                    if company is None or not company.domain:
                        continue
                    feed, first = activate(s, company, http, llm=llm)
                    counts[f"via_{feed.method}"] += 1
                    counts["jobs_found"] += first.new
            except Exception as e:
                counts["errors"] += 1
                log.warning("activation of company %s failed: %s", company_id, e)
                with session_scope() as s:
                    audit.record(s, "m1", "activation_failed", f"Couldn't set up a feed: {redact(str(e))[:300]}",
                                 subject_type="company", subject_id=str(company_id))
    return dict(counts)


def run_sync_batch(*, limit: int | None = None, company_ids: list[int] | None = None) -> dict:
    n = limit or get_config().watch.sync_batch
    now = datetime.now(timezone.utc)
    with session_scope() as s:
        q = select(CompanyFeed.id).join(Company, Company.id == CompanyFeed.company_id)
        if company_ids:
            q = q.where(CompanyFeed.company_id.in_(company_ids))
        else:
            q = q.where(
                Company.status.in_((STATUS_WATCHING, STATUS_DORMANT)),
                CompanyFeed.needs_reactivation.is_(False),
                or_(CompanyFeed.next_sync_at.is_(None), CompanyFeed.next_sync_at <= now),
            ).order_by(CompanyFeed.next_sync_at.asc().nulls_first()).limit(n)
        feed_ids = list(s.scalars(q))

    counts: Counter = Counter()
    with PoliteClient() as http:
        for feed_id in feed_ids:
            with session_scope() as s:
                result = sync_feed(s, http, s.get(CompanyFeed, feed_id))
                if result.error:
                    counts["failed"] += 1
                else:
                    counts["synced"] += 1
                    counts["new_jobs"] += result.new
                    counts["closed_jobs"] += result.closed
    return dict(counts)


def run_gardening() -> dict:
    with PoliteClient() as http, session_scope() as s:
        return garden(s, http, _llm())
