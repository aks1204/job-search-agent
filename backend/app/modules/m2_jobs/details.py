"""Full job descriptions for boards whose list API returns titles only.

Only jobs that passed the free Kind-1 filters get fetched, a few per tick, through the
polite client. Each fetcher returns (description, extra facts) or raises.
"""

import html
import re
from urllib.parse import quote, urlsplit

from app.core.http import PoliteClient
from app.models.feed import CompanyFeed
from app.models.job import JobPosting
from app.modules.m1_companies.enrich import html_to_text

LIMIT = 20_000
_SITE_NUMBER = re.compile(r'data-sitenumber="([\w-]+)"', re.I)


def _text(raw: str | None) -> str | None:
    if not raw:
        return None
    return html_to_text(html.unescape(raw), limit=LIMIT) or None


def _workday(http: PoliteClient, feed: CompanyFeed, job: JobPosting, cache: dict) -> tuple[str | None, dict]:
    # Job URL: https://HOST.myworkdayjobs.com/SITE/job/…  →  API: /wday/cxs/TENANT/SITE/job/…
    path = urlsplit(job.url or "").path
    prefix = f"/{feed.site}"
    if not path.startswith(prefix):
        raise ValueError(f"unexpected Workday URL {job.url}")
    resp = http.get(f"https://{feed.host}.myworkdayjobs.com/wday/cxs/{feed.slug}/{feed.site}{path[len(prefix):]}",
                    headers={"Accept": "application/json"})
    resp.raise_for_status()
    info = resp.json().get("jobPostingInfo") or {}
    return _text(info.get("jobDescription")), {"employment": info.get("timeType"), "posted": info.get("startDate")}


def _oracle_site_number(http: PoliteClient, feed: CompanyFeed, cache: dict) -> str:
    if feed.site:
        return feed.site
    key = ("oracle-site", feed.id)
    if key not in cache:
        page = http.get(f"https://{feed.host}/hcmUI/CandidateExperience/en/sites/{feed.slug}/jobs")
        page.raise_for_status()
        m = _SITE_NUMBER.search(page.text)
        if not m:
            raise ValueError("no Oracle site number on the board page")
        cache[key] = m.group(1)
    return cache[key]


def _oracle(http: PoliteClient, feed: CompanyFeed, job: JobPosting, cache: dict) -> tuple[str | None, dict]:
    site_number = _oracle_site_number(http, feed, cache)
    finder = f'ById;Id="{job.external_id}",siteNumber={site_number}'
    resp = http.get(
        f"https://{feed.host}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"
        f"?expand=all&onlyData=true&finder={quote(finder, safe=';,=')}",
        headers={"Accept": "application/json"},
    )
    resp.raise_for_status()
    items = resp.json().get("items") or []
    if not items:
        return None, {}
    d = items[0]
    parts = [d.get(k) for k in ("ExternalDescriptionStr", "ExternalResponsibilitiesStr", "ExternalQualificationsStr")]
    return _text("\n\n".join(p for p in parts if p)), {"employment": d.get("JobSchedule") or d.get("JobType")}


def _eightfold(http: PoliteClient, feed: CompanyFeed, job: JobPosting, cache: dict) -> tuple[str | None, dict]:
    resp = http.get(f"https://{feed.slug}.eightfold.ai/api/pcsx/position_details",
                    params={"position_id": job.external_id, "domain": feed.site, "hl": "en"},
                    headers={"Accept": "application/json"})
    resp.raise_for_status()
    data = resp.json().get("data") or {}
    raw = next((data.get(k) for k in ("jobDescription", "job_description", "description") if data.get(k)), None)
    return _text(raw), {"employment": data.get("employmentType") or data.get("type")}


def _smartrecruiters(http: PoliteClient, feed: CompanyFeed, job: JobPosting, cache: dict) -> tuple[str | None, dict]:
    resp = http.get(f"https://api.smartrecruiters.com/v1/companies/{feed.slug}/postings/{job.external_id}")
    resp.raise_for_status()
    body = resp.json()
    sections = (body.get("jobAd") or {}).get("sections") or {}
    parts = [(sections.get(k) or {}).get("text") for k in ("jobDescription", "qualifications", "additionalInformation")]
    return _text("\n\n".join(p for p in parts if p)), {"employment": (body.get("typeOfEmployment") or {}).get("label")}


FETCHERS = {"workday": _workday, "oracle": _oracle, "eightfold": _eightfold, "smartrecruiters": _smartrecruiters}


def fetch_description(http: PoliteClient, feed: CompanyFeed, job: JobPosting, cache: dict) -> tuple[str | None, dict]:
    fetcher = FETCHERS.get(feed.method)
    if fetcher is None:
        raise LookupError(f"no description endpoint for {feed.method}")
    return fetcher(http, feed, job, cache)
