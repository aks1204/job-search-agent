"""Job-feed readers: public ATS endpoints, Workday's JSON, per-company adapters.

Each reader returns a FetchResult: the jobs, plus the board's own company name
when the vendor reports one (used for the ownership check).
"""

import html
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import quote

from app.core.http import PoliteClient
from app.modules.m1_companies.enrich import html_to_text

DESCRIPTION_LIMIT = 20_000
_TAG = re.compile(r"<[^>]+>")


class FeedNotFound(Exception):
    """The board/slug doesn't exist on this vendor (404 or an empty account)."""


@dataclass
class RawJob:
    external_id: str
    title: str
    location: str | None = None
    url: str | None = None
    department: str | None = None
    employment_type: str | None = None
    posted_at: str | None = None
    description: str | None = None
    payload: dict = field(default_factory=dict)


@dataclass
class FetchResult:
    jobs: list[RawJob]
    board_name: str | None = None


@dataclass
class FeedSpec:
    method: str
    slug: str | None = None
    site: str | None = None
    host: str | None = None


def _text(html_or_escaped: str | None) -> str | None:
    if not html_or_escaped:
        return None
    return html_to_text(html.unescape(html_or_escaped), limit=DESCRIPTION_LIMIT)


def _get_json(http: PoliteClient, url: str, **kw):
    resp = http.get(url, **kw)
    if resp.status_code == 404:
        raise FeedNotFound(url)
    resp.raise_for_status()
    return resp.json()


def read_greenhouse(http: PoliteClient, spec: FeedSpec, roles: list[str]) -> FetchResult:
    base = f"https://boards-api.greenhouse.io/v1/boards/{spec.slug}"
    data = _get_json(http, f"{base}/jobs", params={"content": "true"})
    jobs = [
        RawJob(
            external_id=str(j["id"]), title=j.get("title") or "",
            location=(j.get("location") or {}).get("name"), url=j.get("absolute_url"),
            department=", ".join(d.get("name", "") for d in j.get("departments") or []) or None,
            posted_at=j.get("first_published") or j.get("updated_at"),
            description=_text(j.get("content")),
            payload={"updated_at": j.get("updated_at"), "requisition_id": j.get("requisition_id")},
        )
        for j in data.get("jobs") or []
    ]
    try:
        board_name = _get_json(http, base).get("name")
    except Exception:
        board_name = None
    return FetchResult(jobs, board_name)


def read_lever(http: PoliteClient, spec: FeedSpec, roles: list[str]) -> FetchResult:
    data = _get_json(http, f"https://api.lever.co/v0/postings/{spec.slug}", params={"mode": "json"})
    if not isinstance(data, list):
        raise FeedNotFound(spec.slug)
    jobs = []
    for j in data:
        cats = j.get("categories") or {}
        created = j.get("createdAt")
        jobs.append(RawJob(
            external_id=str(j["id"]), title=j.get("text") or "", location=cats.get("location"),
            url=j.get("hostedUrl"), department=cats.get("team"), employment_type=cats.get("commitment"),
            posted_at=datetime.fromtimestamp(created / 1000, timezone.utc).isoformat() if created else None,
            description="\n\n".join(p for p in (j.get("descriptionPlain"), j.get("additionalPlain")) if p)[:DESCRIPTION_LIMIT] or None,
            payload={"workplaceType": j.get("workplaceType"), "applyUrl": j.get("applyUrl")},
        ))
    return FetchResult(jobs)


def read_ashby(http: PoliteClient, spec: FeedSpec, roles: list[str]) -> FetchResult:
    data = _get_json(http, f"https://api.ashbyhq.com/posting-api/job-board/{spec.slug}",
                     params={"includeCompensation": "true"})
    jobs = [
        RawJob(
            external_id=str(j["id"]), title=j.get("title") or "", location=j.get("location"),
            url=j.get("jobUrl"), department=j.get("department") or j.get("team"),
            employment_type=j.get("employmentType"), posted_at=j.get("publishedAt"),
            description=(j.get("descriptionPlain") or _text(j.get("descriptionHtml")) or "")[:DESCRIPTION_LIMIT] or None,
            payload={"isRemote": j.get("isRemote"), "applyUrl": j.get("applyUrl"),
                     "compensation": j.get("compensation")},
        )
        for j in data.get("jobs") or [] if j.get("isListed", True)
    ]
    return FetchResult(jobs)


def read_smartrecruiters(http: PoliteClient, spec: FeedSpec, roles: list[str]) -> FetchResult:
    jobs, board_name = [], None
    for page in range(5):
        data = _get_json(http, f"https://api.smartrecruiters.com/v1/companies/{spec.slug}/postings",
                         params={"limit": 100, "offset": page * 100})
        content = data.get("content") or []
        for j in content:
            loc = j.get("location") or {}
            board_name = board_name or (j.get("company") or {}).get("name")
            jobs.append(RawJob(
                external_id=str(j["id"]), title=j.get("name") or "",
                location=loc.get("fullLocation") or ", ".join(x for x in (loc.get("city"), loc.get("country")) if x),
                url=f"https://jobs.smartrecruiters.com/{spec.slug}/{j['id']}",
                department=(j.get("department") or {}).get("label"),
                employment_type=(j.get("typeOfEmployment") or {}).get("label"),
                posted_at=j.get("releasedDate"), payload={"remote": loc.get("remote"), "ref": j.get("ref")},
            ))
        if len(content) < 100:
            break
    if not jobs and page == 0:
        raise FeedNotFound(spec.slug)  # unknown companies return an empty list, not a 404
    return FetchResult(jobs, board_name)


def read_workable(http: PoliteClient, spec: FeedSpec, roles: list[str]) -> FetchResult:
    data = _get_json(http, f"https://apply.workable.com/api/v1/widget/accounts/{spec.slug}")
    jobs = [
        RawJob(
            external_id=str(j.get("shortcode") or j.get("code")), title=j.get("title") or "",
            location=", ".join(x for x in (j.get("city"), j.get("country")) if x) or None,
            url=j.get("url") or j.get("shortlink"), department=j.get("department"),
            employment_type=j.get("employment_type"), posted_at=j.get("published_on") or j.get("created_at"),
            payload={"telecommuting": j.get("telecommuting"), "application_url": j.get("application_url")},
        )
        for j in data.get("jobs") or []
    ]
    return FetchResult(jobs, data.get("name"))


def read_recruitee(http: PoliteClient, spec: FeedSpec, roles: list[str]) -> FetchResult:
    data = _get_json(http, f"https://{spec.slug}.recruitee.com/api/offers/")
    offers = data.get("offers") or []
    jobs = [
        RawJob(
            external_id=str(j["id"]), title=j.get("title") or "", location=j.get("location"),
            url=j.get("careers_url"), department=j.get("department"),
            employment_type=j.get("employment_type_code"), posted_at=j.get("published_at"),
            description=_text((j.get("description") or "") + (j.get("requirements") or "")),
            payload={"remote": j.get("remote")},
        )
        for j in offers
    ]
    return FetchResult(jobs, offers[0].get("company_name") if offers else None)


def read_workday(http: PoliteClient, spec: FeedSpec, roles: list[str], *, pages_per_role: int = 2) -> FetchResult:
    """Workday boards can hold thousands of jobs: search with the owner's roles (the site's own filter)."""
    tenant = spec.slug
    endpoint = f"https://{spec.host}.myworkdayjobs.com/wday/cxs/{tenant}/{spec.site}/jobs"
    seen: dict[str, RawJob] = {}
    for query in roles or [""]:
        for page in range(pages_per_role):
            resp = http.post(endpoint, json={"appliedFacets": {}, "limit": 20, "offset": page * 20, "searchText": query},
                             headers={"Accept": "application/json"})
            if resp.status_code == 404:
                raise FeedNotFound(endpoint)
            resp.raise_for_status()
            postings = resp.json().get("jobPostings") or []
            for j in postings:
                path = j.get("externalPath") or ""
                ext = (j.get("bulletFields") or [path])[0] or path
                if ext and ext not in seen:
                    seen[ext] = RawJob(
                        external_id=str(ext)[:255], title=j.get("title") or "", location=j.get("locationsText"),
                        url=f"https://{spec.host}.myworkdayjobs.com/{spec.site}{path}" if path else None,
                        posted_at=j.get("postedOn"), payload={"query": query},
                    )
            if len(postings) < 20:
                break
    return FetchResult(list(seen.values()))


_ORACLE_SITE_NUMBER = re.compile(r'data-sitenumber="([^"]+)"', re.I)


def read_oracle(http: PoliteClient, spec: FeedSpec, roles: list[str], *, pages_per_role: int = 2) -> FetchResult:
    """Oracle Recruiting Cloud (e.g. Uber). The public site name (UberCareers) maps to an internal
    site number (CX_1) printed in the board page; the jobs API is then searched with the owner's roles."""
    host, site = spec.host, spec.slug
    if spec.site:  # vanity-domain boards already told us the site number (e.g. Amex: CX_1)
        site_number = spec.site
    else:
        page = http.get(f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/jobs")
        if page.status_code == 404:
            raise FeedNotFound(site)
        page.raise_for_status()
        m = _ORACLE_SITE_NUMBER.search(page.text)
        if not m:
            raise FeedNotFound(f"{site}: no site number on the board page")
        site_number = m.group(1)

    seen: dict[str, RawJob] = {}
    for query in roles or [""]:
        for p in range(pages_per_role):
            finder = (f'findReqs;siteNumber={site_number},limit=25,offset={p * 25},'
                      f'keyword="{query}",sortBy=POSTING_DATES_DESC')
            url = (f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
                   f"?onlyData=true&expand=requisitionList.secondaryLocations&finder={quote(finder, safe=';,=')}")
            resp = http.get(url, headers={"Accept": "application/json"})
            resp.raise_for_status()
            items = resp.json().get("items") or []
            reqs = (items[0].get("requisitionList") or []) if items else []
            for j in reqs:
                ext = str(j.get("Id") or "")
                if ext and ext not in seen:
                    seen[ext] = RawJob(
                        external_id=ext, title=j.get("Title") or "", location=j.get("PrimaryLocation"),
                        url=f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{ext}",
                        department=j.get("JobFamily") or j.get("JobFunction"),
                        employment_type=j.get("WorkerType") or j.get("JobType"),
                        posted_at=j.get("PostedDate"),
                        description=(j.get("ShortDescriptionStr") or None),
                        payload={"workplace": j.get("WorkplaceTypeCode"), "country": j.get("PrimaryLocationCountry"),
                                 "query": query},
                    )
            if len(reqs) < 25:
                break
    return FetchResult(list(seen.values()))


def read_eightfold(http: PoliteClient, spec: FeedSpec, roles: list[str], *, pages_per_role: int = 2) -> FetchResult:
    """Eightfold careers sites (e.g. Morgan Stanley). Searched per role, filtered to India (the site's own
    filter), plus one unfiltered page per role so remote-from-anywhere roles still show up.
    spec.slug = tenant (morganstanley), spec.site = the company domain the API expects (morganstanley.com)."""
    tenant, domain = spec.slug, spec.site or f"{spec.slug}.com"
    seen: dict[str, RawJob] = {}
    for query in roles or [""]:
        for location, pages in (("India", pages_per_role), ("", 1)):
            for p in range(pages):
                resp = http.get(
                    f"https://{tenant}.eightfold.ai/api/pcsx/search",
                    params={"domain": domain, "query": query, "location": location, "start": p * 10},
                    headers={"Accept": "application/json"},
                )
                if resp.status_code == 404:
                    raise FeedNotFound(f"{tenant}/{domain}")
                resp.raise_for_status()
                body = resp.json()
                if not isinstance(body.get("data"), dict):
                    # e.g. BCG: {"message": "PCSX is not enabled for this user."} — a talent-community
                    # tenant, not the job search. Not a board, rather than a board with 0 jobs.
                    raise FeedNotFound(f"{tenant}: {body.get('message') or 'no job search on this tenant'}")
                positions = body["data"].get("positions") or []
                for j in positions:
                    ext = str(j.get("id") or "")
                    if not ext or ext in seen:
                        continue
                    posted = j.get("postedTs") or j.get("creationTs")
                    seen[ext] = RawJob(
                        external_id=ext, title=j.get("name") or "",
                        location="; ".join((j.get("locations") or [])[:3]) or None,
                        url=f"https://{tenant}.eightfold.ai{j['positionUrl']}" if j.get("positionUrl") else None,
                        department=j.get("department"),
                        posted_at=datetime.fromtimestamp(posted, timezone.utc).isoformat() if posted else None,
                        payload={"ats_job_id": j.get("atsJobId") or j.get("displayJobId"),
                                 "work_location": j.get("workLocationOption"), "query": query},
                    )
                if len(positions) < 10:
                    break
    return FetchResult(list(seen.values()))


# Workday spreads tenants across numbered servers (wd1, wd5, wd115, …) and moves them over time,
# so a remembered "lilly.wd5" can be stale while "lilly.wd115" is live. Common ones first.
WORKDAY_SERVERS = (
    [1, 3, 5, 10, 12, 101, 102, 103, 105, 108, 115, 501, 503]
    + [n for n in range(1, 21) if n not in (1, 3, 5, 10, 12)]
    + [n for n in range(100, 131) if n not in (101, 102, 103, 105, 108, 115)]
)


def probe_workday(http: PoliteClient, host: str, tenant: str, site: str) -> str:
    """One minimal search against tenant/site on one server: 'ok' or what went wrong."""
    try:
        resp = http.post(
            f"https://{host}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs",
            json={"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""},
            headers={"Accept": "application/json"},
        )
    except Exception as e:
        return type(e).__name__
    if resp.status_code == 200 and "jobPostings" in resp.text:
        return "ok"
    return f"HTTP {resp.status_code}"


def find_workday_host(
    http: PoliteClient, tenant: str, site: str, *, skip: str | None = None, outcomes: list[str] | None = None
) -> str | None:
    """Probe Workday servers for the one hosting tenant/site. Returns 'tenant.wdN' or None.

    `outcomes` collects a short tally (e.g. 'HTTP 422×52') so a failure can explain itself.
    """
    tally: dict[str, int] = {}
    for n in WORKDAY_SERVERS:
        host = f"{tenant}.wd{n}"
        if host == skip:
            continue
        outcome = probe_workday(http, host, tenant, site)
        if outcome == "ok":
            return host
        tally[outcome] = tally.get(outcome, 0) + 1
    if outcomes is not None:
        outcomes.append("probed servers: " + ", ".join(f"{k}×{v}" for k, v in sorted(tally.items())))
    return None


def _dig(obj, path: str | None):
    if not path:
        return None
    for part in path.split("."):
        if isinstance(obj, dict):
            obj = obj.get(part)
        elif isinstance(obj, list) and part.isdigit() and int(part) < len(obj):
            obj = obj[int(part)]
        else:
            return None
    return obj


def read_adapter(http: PoliteClient, adapter: dict) -> FetchResult:
    req, fields, pag = adapter["request"], adapter["fields"], adapter.get("pagination")
    jobs: list[RawJob] = []
    for page in range(pag["max_pages"] if pag else 1):
        params, body = dict(req.get("params") or {}), dict(req.get("json_body") or {}) or None
        if pag:
            target = body if req["method"] == "POST" and body is not None else params
            target[pag["param"]] = page * pag["size"]
        resp = http.request(req["method"], req["url"], params=params, json=body)
        resp.raise_for_status()
        items = _dig(resp.json(), adapter["items"]) or []
        for item in items:
            ext = _dig(item, fields.get("external_id"))
            if ext is None:
                continue
            url = _dig(item, fields.get("url"))
            if not url and adapter.get("url_template"):
                url = adapter["url_template"].format(external_id=ext)
            jobs.append(RawJob(
                external_id=str(ext), title=str(_dig(item, fields.get("title")) or ""),
                location=_dig(item, fields.get("location")), url=url,
                department=_dig(item, fields.get("department")),
                posted_at=str(_dig(item, fields.get("posted_at")) or "") or None,
                description=_text(str(_dig(item, fields.get("description")) or "")),
            ))
        if not pag or len(items) < pag["size"]:
            break
    return FetchResult(jobs, adapter.get("name"))


# --- careers-site platforms (2026-11-01: checked live on Booking, Rivian, ICE, BCG, UHG, Axmed, Sun King,
# Paradromics). Each takes only the public, logged-out job list.

def _host(spec: FeedSpec, suffix: str) -> str:
    """'axmed' → 'axmed.teamtailor.com'; a full host (custom careers domain) stays as it is."""
    return spec.slug if "." in (spec.slug or "") else f"{spec.slug}.{suffix}"


def read_jibe(http: PoliteClient, spec: FeedSpec, roles: list[str], *, max_pages: int = 10) -> FetchResult:
    """Jibe careers sites, usually in front of iCIMS (Booking, Rivian, ICE): /api/jobs lists every job with its
    iCIMS apply link. Small boards are read whole; very big ones are searched with the owner's roles."""
    host = spec.slug
    seen: dict[str, RawJob] = {}

    def take(items: list, query: str) -> None:
        for item in items:
            d = item.get("data") or item
            ext = str(d.get("req_id") or d.get("slug") or "")
            if not ext or ext in seen:
                continue
            location = d.get("full_location") or d.get("location_name") or \
                ", ".join(x for x in (d.get("city"), d.get("country")) if x) or None
            seen[ext] = RawJob(
                external_id=ext[:255], title=d.get("title") or "", location=location,
                url=d.get("canonical_url") or (f"https://{host}/jobs/{d['slug']}" if d.get("slug") else d.get("apply_url")),
                department=d.get("department") or ", ".join(c.get("name", "") for c in (d.get("categories") or [])[:2]
                                                            if isinstance(c, dict)) or None,
                employment_type=d.get("employment_type"), posted_at=d.get("posted_date"),
                description=_text(d.get("description")),
                payload={"apply_url": d.get("apply_url"), "ats": d.get("ats_code"), "country": d.get("country"),
                         "location_type": d.get("location_type"), "query": query},
            )

    first = _get_json(http, f"https://{host}/api/jobs", params={"page": 1, "limit": 100})
    if not isinstance(first, dict) or "jobs" not in first:
        raise FeedNotFound(f"{host}: not a Jibe job list")
    total = int(first.get("totalCount") or 0)            # 0 = the site doesn't say: read pages until empty
    if total <= max_pages * 100:                         # read it all
        items = first.get("jobs") or []
        size = len(items)                                # the site's own page size (100, or 10 if it ignores limit)
        take(items, "")
        for page in range(2, max_pages + 1):
            if not size or len(items) < size or (total and len(seen) >= total):
                break
            body = _get_json(http, f"https://{host}/api/jobs", params={"page": page, "limit": 100})
            items = (body.get("jobs") or []) if isinstance(body, dict) else []
            take(items, "")
    else:                                                # too big to read whole: the site's own search
        for query in roles or [""]:
            take(_get_json(http, f"https://{host}/api/jobs",
                           params={"page": 1, "limit": 100, "keywords": query}).get("jobs") or [], query)
    return FetchResult(list(seen.values()))


_PHENOM_DDO = re.compile(r"phApp\.ddo\s*=\s*")


def _phenom_jobs(page_html: str) -> tuple[list[dict], int] | None:
    m = _PHENOM_DDO.search(page_html)
    if not m:
        return None
    try:
        ddo, _ = json.JSONDecoder().raw_decode(page_html, m.end())
    except ValueError:
        return None
    search = (ddo or {}).get("eagerLoadRefineSearch") or {}
    return (search.get("data") or {}).get("jobs") or [], int(search.get("totalHits") or 0)


def read_phenom(http: PoliteClient, spec: FeedSpec, roles: list[str], *, pages_per_role: int = 2) -> FetchResult:
    """Phenom careers sites (BCG): the search page carries its results in an embedded phApp.ddo object.
    Searched with the owner's roles, 10 jobs a page (?keywords=…&from=N)."""
    home = http.get(f"https://{spec.slug}/", scrape=True)                    # → https://careers.bcg.com/global/en/
    if home.status_code == 404:
        raise FeedNotFound(spec.slug)
    home.raise_for_status()
    base = str(home.url).split("?")[0].rstrip("/")
    seen: dict[str, RawJob] = {}
    for query in roles or [""]:
        for p in range(pages_per_role):
            params = {"keywords": query, "from": p * 10} if query else {"from": p * 10, "s": 1}
            resp = http.get(f"{base}/search-results", params=params, scrape=True)
            resp.raise_for_status()
            found = _phenom_jobs(resp.text)
            if found is None:
                raise FeedNotFound(f"{spec.slug}: no Phenom search results on the page")
            jobs, _ = found
            for j in jobs:
                ext = str(j.get("jobId") or j.get("jobSeqNo") or "")
                if not ext or ext in seen:
                    continue
                seen[ext] = RawJob(
                    external_id=ext, title=(j.get("title") or "").replace("​", "").strip(),
                    location=j.get("location") or "; ".join((j.get("multi_location") or [])[:3]) or None,
                    url=j.get("jobUrl") or j.get("applyUrl"), department=j.get("category"),
                    employment_type=j.get("type"), posted_at=j.get("postedDate") or j.get("dateCreated"),
                    # The list's one-line teaser isn't the description: leaving it empty lets the full job
                    # page be fetched later.
                    payload={"apply_url": j.get("applyUrl"), "country": j.get("country"), "query": query,
                             "teaser": j.get("descriptionTeaser")},
                )
            if len(jobs) < 10:
                break
    return FetchResult(list(seen.values()))


_TB_ITEM = re.compile(r"<li\b[^>]*>(.*?)</li>", re.I | re.S)
_TB_LINK = re.compile(r'<a\b[^>]*href="(/job/[^"]+)"[^>]*?data-job-id="(\d+)"', re.I)
_TB_TITLE = re.compile(r"<h2[^>]*>(.*?)</h2>", re.I | re.S)
_TB_LOCATION = re.compile(r'class="job-location[^"]*"[^>]*>(.*?)</span>', re.I | re.S)


def read_talentbrew(http: PoliteClient, spec: FeedSpec, roles: list[str], *, pages_per_role: int = 2) -> FetchResult:
    """TalentBrew (Radancy) careers sites (UnitedHealth Group): the search page's own results service,
    searched with the owner's roles, 50 jobs a page; the job list comes back as an HTML fragment."""
    host = spec.slug
    seen: dict[str, RawJob] = {}
    for query in roles or [""]:
        for page in range(1, pages_per_role + 1):
            resp = http.get(f"https://{host}/search-jobs/results", scrape=True, headers={"X-Requested-With": "XMLHttpRequest"},
                            params={"ActiveFacetID": 0, "CurrentPage": page, "RecordsPerPage": 50, "Distance": 50,
                                    "RadiusUnitType": 0, "Keywords": query, "Location": "", "ShowRadius": "False",
                                    "IsPagination": "False", "FacetType": 0, "SearchResultsModuleName": "Search Results",
                                    "SearchFiltersModuleName": "Search Filters", "SortCriteria": 0, "SortDirection": 0,
                                    "SearchType": 5, "ResultsType": 0})
            if resp.status_code == 404:
                raise FeedNotFound(host)
            resp.raise_for_status()
            try:
                fragment = resp.json().get("results") or ""
            except ValueError:
                raise FeedNotFound(f"{host}: not a TalentBrew results service")
            found = 0
            for item in _TB_ITEM.findall(fragment):
                link = _TB_LINK.search(item)
                if not link:
                    continue
                found += 1
                path, ext = link.groups()
                if ext in seen:
                    continue
                title, loc = _TB_TITLE.search(item), _TB_LOCATION.search(item)
                seen[ext] = RawJob(
                    external_id=ext, title=html.unescape(_TAG.sub("", title.group(1))).strip() if title else "",
                    location=html.unescape(_TAG.sub("", loc.group(1))).strip() if loc else None,
                    url=f"https://{host}{path}", payload={"query": query},
                )
            if found < 50:
                break
    return FetchResult(list(seen.values()))


def read_teamtailor(http: PoliteClient, spec: FeedSpec, roles: list[str]) -> FetchResult:
    """Teamtailor (Axmed): the public JSON Feed at /jobs.json, with a schema.org JobPosting per job."""
    url, seen, name = f"https://{_host(spec, 'teamtailor.com')}/jobs.json", {}, None
    for _ in range(10):                                         # JSON Feed pages via next_url
        data = _get_json(http, url)
        name = name or data.get("title")
        for it in data.get("items") or []:
            jp = it.get("_jobposting") or {}
            ext = str((jp.get("identifier") or {}).get("value") or it.get("id") or "")
            if not ext or ext in seen:
                continue
            places = jp.get("jobLocation") or []
            places = places if isinstance(places, list) else [places]
            parts = [", ".join(x for x in ((p.get("address") or {}).get("addressLocality"),
                                           (p.get("address") or {}).get("addressCountry")) if x)
                     for p in places[:3] if isinstance(p, dict)]
            where = "; ".join(x for x in parts if x) or None
            if jp.get("jobLocationType") == "TELECOMMUTE":
                where = f"Remote ({where})" if where else "Remote"
            seen[ext] = RawJob(
                external_id=ext, title=it.get("title") or jp.get("title") or "", location=where, url=it.get("url"),
                employment_type=jp.get("employmentType") if isinstance(jp.get("employmentType"), str) else None,
                posted_at=it.get("date_published") or jp.get("datePosted"),
                description=_text(it.get("content_html") or jp.get("description")),
                payload={"remote": jp.get("jobLocationType") == "TELECOMMUTE"},
            )
        if not data.get("next_url"):
            break
        url = data["next_url"]
    return FetchResult(list(seen.values()), name)


def read_pinpoint(http: PoliteClient, spec: FeedSpec, roles: list[str]) -> FetchResult:
    """Pinpoint (Sun King): every posting in one public JSON list, /postings.json."""
    data = _get_json(http, f"https://{_host(spec, 'pinpointhq.com')}/postings.json")
    jobs = []
    for j in data.get("data") or []:
        loc = j.get("location") or {}
        where = ", ".join(x for x in (loc.get("city"), loc.get("province"), loc.get("name")) if x) or None
        body = "".join(j.get(k) or "" for k in ("description", "key_responsibilities", "skills_knowledge_expertise"))
        jobs.append(RawJob(
            external_id=str(j.get("id")), title=j.get("title") or "", location=where, url=j.get("url"),
            department=((j.get("job") or {}).get("department") or {}).get("name"),
            employment_type=j.get("employment_type_text") or j.get("employment_type"),
            description=_text(body) if body else None,
            payload={"workplace": j.get("workplace_type"), "deadline": j.get("deadline_at")},
        ))
    return FetchResult([j for j in jobs if j.external_id and j.external_id != "None"])


_JAZZ_ITEM = re.compile(r'<li class="list-group-item"[^>]*>(.*?)</ul>\s*</li>', re.I | re.S)
_JAZZ_LINK = re.compile(r'<a href="(https?://[^"]+/apply/([A-Za-z0-9]{6,})/[^"]*)"[^>]*>(.*?)</a>', re.I | re.S)
_JAZZ_LOCATION = re.compile(r'fa-map-marker"></i>(.*?)</li>', re.I | re.S)
_PAGE_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)


def read_jazzhr(http: PoliteClient, spec: FeedSpec, roles: list[str]) -> FetchResult:
    """JazzHR (Paradromics): the public job list at https://<company>.applytojob.com/apply (no feed)."""
    host = _host(spec, "applytojob.com")
    resp = http.get(f"https://{host}/apply", scrape=True)
    if resp.status_code == 404 or "applytojob.com" not in str(resp.url):    # unknown boards bounce to jazzhr.com
        raise FeedNotFound(host)
    resp.raise_for_status()
    jobs = []
    for item in _JAZZ_ITEM.findall(resp.text):
        link = _JAZZ_LINK.search(item)
        if not link:
            continue
        url, code, title = link.groups()
        loc = _JAZZ_LOCATION.search(item)
        jobs.append(RawJob(external_id=code, title=html.unescape(_TAG.sub("", title)).strip(), url=url,
                           location=html.unescape(_TAG.sub("", loc.group(1))).strip() if loc else None))
    page_title = _PAGE_TITLE.search(resp.text)
    name = html.unescape(page_title.group(1)).split(" - ")[0].strip() if page_title else None
    return FetchResult(jobs, name)


READERS = {
    "greenhouse": read_greenhouse,
    "lever": read_lever,
    "ashby": read_ashby,
    "smartrecruiters": read_smartrecruiters,
    "workable": read_workable,
    "recruitee": read_recruitee,
    "workday": read_workday,
    "oracle": read_oracle,
    "eightfold": read_eightfold,
    "jibe": read_jibe,
    "phenom": read_phenom,
    "talentbrew": read_talentbrew,
    "teamtailor": read_teamtailor,
    "pinpoint": read_pinpoint,
    "jazzhr": read_jazzhr,
}
