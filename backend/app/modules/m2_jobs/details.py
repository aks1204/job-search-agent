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


_LD_JSON = re.compile(r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>', re.I | re.S)


def _ld_job_description(page: str) -> str | None:
    """The schema.org JobPosting most careers sites embed for search engines."""
    return (_ld_job(page) or {}).get("description")


def _ld_address(place) -> str | None:
    address = place.get("address") if isinstance(place, dict) else None
    if not isinstance(address, dict):
        return None
    country = address.get("addressCountry")
    country = country.get("name") if isinstance(country, dict) else country
    parts = [address.get("addressLocality"), address.get("addressRegion"), country]
    return ", ".join(dict.fromkeys(p.strip() for p in parts if isinstance(p, str) and p.strip())) or None


def _ld_place(job: dict) -> str | None:
    """Every place a JobPosting lists ('Dallas, Texas, US; Hyderabad, Telangana, India'), plus 'Remote' when it
    says so — a job open in several cities must not be judged by the first one alone."""
    places = job.get("jobLocation")
    places = places if isinstance(places, list) else [places]
    found = [a for p in places if (a := _ld_address(p))]
    kind = job.get("jobLocationType")
    if "TELECOMMUTE" in (kind if isinstance(kind, list) else [kind]):
        found.append("Remote")
    return "; ".join(dict.fromkeys(found)) or None


def _ld_job(page: str) -> dict | None:
    import json

    for block in _LD_JSON.findall(page):
        try:
            data = json.loads(block.strip())
        except ValueError:
            continue
        items = data if isinstance(data, list) else [data]
        items += [g for d in items if isinstance(d, dict) and isinstance(d.get("@graph"), list) for g in d["@graph"]]
        for item in items:
            if not isinstance(item, dict):
                continue
            kind = item.get("@type")
            kinds = kind if isinstance(kind, list) else [kind]
            if "JobPosting" in kinds and isinstance(item.get("description"), str) and item["description"]:
                return item
    return None


def _job_page(http: PoliteClient, feed: CompanyFeed, job: JobPosting, cache: dict) -> tuple[str | None, dict]:
    """Phenom, TalentBrew and JazzHR lists give titles only: read the role's own public job page."""
    if not job.url:
        raise ValueError("no job page")
    resp = http.get(job.url, scrape=True)
    resp.raise_for_status()
    page = resp.text
    if ld := _ld_job(page):
        extra = {}
        if feed.method == "talentbrew":   # read from the sitemap, the title and city came from the address
            title = ld.get("title")
            extra = {"title": html.unescape(title).strip() if isinstance(title, str) else None,
                     "location": _ld_place(ld)}
        return _text(ld["description"]), {k: v for k, v in extra.items() if v}
    if feed.method == "phenom":                       # the job page's embedded phApp.ddo.jobDetail
        from app.modules.m1_companies.watch.readers import _PHENOM_DDO

        m = _PHENOM_DDO.search(page)
        if m:
            import json

            try:
                ddo, _ = json.JSONDecoder().raw_decode(page, m.end())
                desc = (((ddo.get("jobDetail") or {}).get("data") or {}).get("job") or {}).get("description")
                if desc:
                    return _text(desc), {}
            except ValueError:
                pass
    return _text(page), {}                            # last resort: the page's text


FETCHERS = {"workday": _workday, "oracle": _oracle, "eightfold": _eightfold, "smartrecruiters": _smartrecruiters,
            "phenom": _job_page, "talentbrew": _job_page, "jazzhr": _job_page}


def fetch_description(http: PoliteClient, feed: CompanyFeed, job: JobPosting, cache: dict) -> tuple[str | None, dict]:
    fetcher = FETCHERS.get(feed.method)
    if fetcher is None:
        raise LookupError(f"no description endpoint for {feed.method}")
    return fetcher(http, feed, job, cache)
