"""Coverage report: how kept companies' jobs are being read, and which reader to build next.

Gaps are grouped by job-system VENDOR, not by company: one new reader fixes every
company on that vendor at their next re-activation.
"""

from collections import Counter, defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.company import STATUS_DORMANT, STATUS_PASSED, STATUS_WATCHING, Company
from app.models.feed import CompanyFeed
from app.modules.m1_companies.watch.readers import READERS

KEPT = (STATUS_PASSED, STATUS_WATCHING, STATUS_DORMANT)
READABLE = set(READERS) | {"adapter"}
EXAMPLES = 5


def coverage(session: Session) -> dict:
    rows = session.execute(
        select(Company, CompanyFeed).outerjoin(CompanyFeed, CompanyFeed.company_id == Company.id)
        .where(Company.status.in_(KEPT)).order_by(Company.name)
    ).all()

    direct: Counter = Counter()
    direct_empty: list[str] = []
    gaps: dict[str, list[str]] = defaultdict(list)
    awaiting_setup: list[str] = []

    for company, feed in rows:
        label = f"{company.name} ({company.domain})"
        if feed is None:
            awaiting_setup.append(label)
        elif feed.method != "aggregator":
            direct[feed.method] += 1
            if feed.open_jobs == 0:
                direct_empty.append(label)  # readable board, just no matching openings right now
        else:
            detected = set(feed.detected_vendors or []) - READABLE
            # Phenom is a careers-site platform that usually fronts another ATS; count it only when
            # nothing else was found behind it (BCG: Phenom search, no reachable ATS).
            unreadable = sorted(detected - {"phenom"}) or sorted(detected)
            if unreadable:
                for vendor in unreadable:
                    gaps[f"vendor: {vendor}"].append(label)
            elif feed.careers_url:
                gaps["custom careers page (no known vendor)"].append(label)
            else:
                gaps["no careers page found"].append(label)

    fallback = sum(1 for _, f in rows if f is not None and f.method == "aggregator")
    ranked = sorted(gaps.items(), key=lambda kv: -len(kv[1]))
    return {
        "kept_companies": len(rows),
        "read_directly": sum(direct.values()),
        "on_job_site_fallback": fallback,
        "awaiting_setup": len(awaiting_setup),
        "direct_by_method": dict(direct.most_common()),
        "direct_but_no_matching_openings": {"count": len(direct_empty), "examples": direct_empty[:EXAMPLES]},
        "gaps": [{"reason": reason, "companies": len(names), "examples": names[:EXAMPLES]} for reason, names in ranked],
        "build_next": [reason.removeprefix("vendor: ") for reason, _ in ranked if reason.startswith("vendor: ")][:3],
    }
