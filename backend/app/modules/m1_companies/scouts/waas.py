"""YC's Work at a Startup — public, logged-out category pages only.

OWNER DECISION (2026-10-12): relaxes spec §7.2, which listed Work at a Startup as
never-crawl. Scope is deliberately narrow: robots.txt allows it; only the public
category pages are read (each embeds its newest ~25-30 jobs as page data); never
logged in (applying needs the owner's own YC account, which matches the manual-apply
design). Disable in scouts.yaml to return to the spec's original rule.
"""

import html
import json
import re
from collections.abc import Iterator

from app.modules.m1_companies.candidate import Candidate
from app.modules.m1_companies.scouts.base import Scout, ScoutContext

BASE = "https://www.workatastartup.com"
_DATA_PAGE = re.compile(r'data-page="([^"]+)"')


def page_jobs(raw_html: str) -> list[dict]:
    m = _DATA_PAGE.search(raw_html)
    if not m:
        return []
    props = json.loads(html.unescape(m.group(1))).get("props") or {}
    return props.get("jobs") or []


class WaaSScout(Scout):
    name = "waas"

    def discover(self, ctx: ScoutContext) -> Iterator[Candidate]:
        for category in self.settings.categories:
            resp = ctx.http.get(f"{BASE}/jobs/l/{category}", scrape=True, headers={"Accept": "text/html"})
            resp.raise_for_status()
            for job in page_jobs(resp.text):
                company = (job.get("companyName") or "").strip()
                if self.is_placeholder(company) or not job.get("id"):
                    continue
                yield Candidate(
                    scout=self.name,
                    external_id=str(job["id"]),
                    name=company,
                    url=f"{BASE}/jobs/{job['id']}",
                    type_hint="startup",
                    data={
                        "category": category,
                        "title": job.get("title"),
                        "location": job.get("location"),
                        "job_type": job.get("jobType"),
                        "role_type": job.get("roleType"),
                        "salary": job.get("salary"),
                        "batch": job.get("companyBatch"),
                        "yc_slug": job.get("companySlug"),
                        "one_liner": job.get("companyOneLiner"),
                    },
                )
