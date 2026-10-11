"""Job-feed readers: public ATS endpoints, Workday's JSON, per-company adapters.

Each reader returns a FetchResult: the jobs, plus the board's own company name
when the vendor reports one (used for the ownership check).
"""

import html
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.core.http import PoliteClient
from app.modules.m1_companies.enrich import html_to_text

DESCRIPTION_LIMIT = 20_000


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


READERS = {
    "greenhouse": read_greenhouse,
    "lever": read_lever,
    "ashby": read_ashby,
    "smartrecruiters": read_smartrecruiters,
    "workable": read_workable,
    "recruitee": read_recruitee,
    "workday": read_workday,
}
