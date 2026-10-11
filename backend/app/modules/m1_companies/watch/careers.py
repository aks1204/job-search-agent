"""Finding a company's careers page (spec §1.5 step 1).

Rungs: homepage (ATS links + nav scan) → URL guesses → web search (never LinkedIn/
Glassdoor) → AI picks from the homepage's links. Every candidate page is
validated — it must actually contain listings or an ATS board — before it's trusted.
"""

import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

import httpx
from pydantic import BaseModel, Field

from app.core.hosts import ats_hint, ats_hints_in, detect_vendors, is_ats_url, normalize_domain
from app.core.http import PoliteClient, RobotsDisallowed
from app.core.search import search_available, web_search

_LINK = re.compile(r"<a\b[^>]*?href=[\"']([^\"'#]+)[\"'][^>]*>(.*?)</a>", re.I | re.S)
_TAG = re.compile(r"<[^>]+>")
CAREER_WORDS = re.compile(
    r"\b(careers?|jobs?|join\s+(us|our\s+team|the\s+team)|work\s+(with|at|for)\s+us|we'?re\s+hiring|"
    r"open\s+(roles|positions)|hiring|vacancies)\b",
    re.I,
)
JOB_PATH = re.compile(r"/(jobs?|careers?|positions?|openings?|vacanc(?:y|ies)|roles?|opportunit(?:y|ies))/[^/?#]{3,}", re.I)
MAX_CANDIDATES = 8
CAREERS_SEARCH_BUDGET = 300  # Brave searches/month for careers-page discovery


@dataclass
class PageScan:
    url: str
    links: list[tuple[str, str]] = field(default_factory=list)  # (absolute href, link text)
    ats_hints: list[dict] = field(default_factory=list)
    job_links: list[str] = field(default_factory=list)
    vendors: set[str] = field(default_factory=set)  # every job-system vendor recognised on the page

    @property
    def is_listing(self) -> bool:
        return bool(self.ats_hints) or len(self.job_links) >= 3


@dataclass
class Discovery:
    careers_url: str
    scan: PageScan
    how: str
    on_company_site: bool  # page lives on the company's own domain (strong ownership evidence)


class CareersLinkPick(BaseModel):
    url: str = Field(description="The link that leads to job listings, copied exactly from the list — or 'none'.")


PICK_SYSTEM = """You help find a company's careers / open-jobs page.
You get the links from the company's homepage. Pick the single link most likely to list open jobs.
Answer "none" if no link plausibly does. Answer in the requested JSON format."""


def scan_page(raw_html: str, base_url: str) -> PageScan:
    links = []
    for href, text in _LINK.findall(raw_html):
        href = href.strip()
        if href.startswith(("mailto:", "tel:", "javascript:")):
            continue
        links.append((urljoin(base_url, href), _TAG.sub(" ", text).strip()[:80]))
    job_links = list(dict.fromkeys(h for h, _ in links if JOB_PATH.search(urlsplit(h).path)))
    return PageScan(url=base_url, links=links, ats_hints=ats_hints_in(raw_html), job_links=job_links,
                    vendors=detect_vendors(raw_html))


def fetch_html(http: PoliteClient, url: str) -> tuple[str, str] | None:
    """(final URL, raw HTML) or None. Respects robots.txt."""
    try:
        resp = http.get(url, scrape=True)
    except (httpx.HTTPError, RobotsDisallowed):
        return None
    if resp.status_code >= 400 or "html" not in resp.headers.get("content-type", ""):
        return None
    return str(resp.url), resp.text


def _on_domain(url: str, domain: str) -> bool:
    return normalize_domain(url) == domain


def _check(http: PoliteClient, url: str, domain: str, how: str) -> tuple[Discovery | None, PageScan | None]:
    """(Discovery if this page lists jobs, the page scan if it was fetchable on-site)."""
    page = fetch_html(http, url)
    if page is None:
        return None, None
    final_url, raw = page
    if not (_on_domain(final_url, domain) or is_ats_url(final_url)):
        return None, None  # redirected off-site (e.g. to LinkedIn) — not trustworthy
    scan = scan_page(raw, final_url)
    own = ats_hint(final_url)  # the link led straight onto a job board's own page
    if own and own not in scan.ats_hints:
        scan.ats_hints.insert(0, own)
    if scan.is_listing:
        return Discovery(final_url, scan, how, on_company_site=_on_domain(final_url, domain)), scan
    return None, scan


_LOC = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.I)
_SITEMAP_LINE = re.compile(r"^\s*sitemap:\s*(\S+)", re.I | re.M)
SITEMAP_JOB_PAGES = 2


def _get_text(http: PoliteClient, url: str) -> str | None:
    try:
        resp = http.get(url, scrape=True)
    except (httpx.HTTPError, RobotsDisallowed):
        return None
    return resp.text if resp.status_code < 400 else None


def _from_sitemaps(http: PoliteClient, hosts: list[str], domain: str, seen: set[str]) -> Discovery | None:
    for host in hosts:
        robots = _get_text(http, f"{host}/robots.txt") or ""
        sitemaps = [u for u in _SITEMAP_LINE.findall(robots) if _on_domain(u, domain)][:3] or [f"{host}/sitemap.xml"]
        for sitemap in sitemaps:
            xml = _get_text(http, sitemap)
            if not xml:
                continue
            locs = _LOC.findall(xml)
            if "<sitemapindex" in xml.lower():  # an index of sitemaps: open the first child or two
                children = sorted(locs, key=lambda u: "job" not in u.lower())[:2]
                locs = [loc for child in children for loc in _LOC.findall(_get_text(http, child) or "")]
            job_pages = [u for u in locs if _on_domain(u, domain) and JOB_PATH.search(urlsplit(u).path)]
            for page_url in job_pages[:SITEMAP_JOB_PAGES]:
                page = fetch_html(http, page_url)
                if not page:
                    continue
                seen |= detect_vendors(page[1])
                hints = ats_hints_in(page[1])
                if hints:
                    scan = PageScan(url=page[0], ats_hints=hints)
                    return Discovery(page[0], scan, "job page found via sitemap", on_company_site=True)
    return None


def discover(
    http: PoliteClient, company, *, llm=None, session=None, vendors_seen: set[str] | None = None
) -> Discovery | None:
    """Find a listing page. `vendors_seen` collects every job-system vendor recognised along the way
    (readable or not) so a fallback can say which reader it was missing."""
    seen = vendors_seen if vendors_seen is not None else set()
    domain = company.domain
    home = fetch_html(http, f"https://{domain}") or fetch_html(http, f"https://www.{domain}")
    home_scan = scan_page(home[1], home[0]) if home else None
    if home_scan:
        seen |= home_scan.vendors

    # Rung 1a: the homepage itself links to an ATS board.
    if home_scan and home_scan.ats_hints:
        return Discovery(home_scan.url, home_scan, "ATS link on homepage", on_company_site=True)

    # Rung 1b: nav links that say careers/jobs, then the usual URL guesses.
    candidates: list[tuple[str, str]] = []
    if home_scan:
        for href, text in home_scan.links:
            if (CAREER_WORDS.search(text) or CAREER_WORDS.search(urlsplit(href).path)) and (
                _on_domain(href, domain) or is_ats_url(href)
            ):
                candidates.append((href, f"homepage link “{text or href}”"))
    for guess in (f"https://{domain}/careers", f"https://{domain}/jobs",
                  f"https://careers.{domain}", f"https://jobs.{domain}"):
        candidates.append((guess, "URL guess"))

    tried = set()
    for url, how in candidates:
        if url in tried or len(tried) >= MAX_CANDIDATES:
            continue
        tried.add(url)
        found, scan = _check(http, url, domain, how)
        if scan:
            seen |= scan.vendors
        if found:
            return found
        # One level deeper: careers landing pages often link to "open positions".
        for href, text in (scan.links if scan else []):
            if CAREER_WORDS.search(text) and href not in tried and (_on_domain(href, domain) or is_ats_url(href)):
                tried.add(href)
                found, deeper = _check(http, href, domain, f"{how} → “{text}”")
                if deeper:
                    seen |= deeper.vendors
                if found:
                    return found
                break

    # Rung 1c: JavaScript careers sites (Phenom & co.) hide the board from the landing page, but publish
    # sitemaps for crawlers, and a job page's server-rendered "Apply" link names the exact board.
    hosts = [f"https://{domain}"] + [
        f"{urlsplit(u).scheme}://{urlsplit(u).netloc}" for u in tried if _on_domain(u, domain)
    ]
    if found := _from_sitemaps(http, list(dict.fromkeys(hosts))[:4], domain, seen):
        return found

    # Rung 2: web search, keeping only the company's own site or ATS boards (never LinkedIn/Glassdoor).
    if search_available():
        results = web_search(http, f'"{company.name}" careers jobs', purpose="careers",
                             monthly_budget=CAREERS_SEARCH_BUDGET) or []
        for r in results[:5]:
            if (_on_domain(r.url, domain) or is_ats_url(r.url)) and r.url not in tried:
                tried.add(r.url)
                found, _ = _check(http, r.url, domain, "web search")
                if found:
                    return found

    # Rung 3: the AI picks from the homepage links.
    if llm is not None and home_scan and home_scan.links:
        listing = "\n".join(f"- {text or '(no text)'} → {href}" for href, text in home_scan.links[:80])
        try:
            pick = llm.structured(
                "fast", system=PICK_SYSTEM, schema=CareersLinkPick, purpose="m1.pick_careers_link",
                user=f"Company: {company.name} ({domain})\nHomepage links:\n{listing}",
                session=session, subject=("company", str(company.id)),
            ).parsed
        except Exception:
            pick = None
        if pick and pick.url != "none" and pick.url not in tried:
            found, _ = _check(http, pick.url, domain, "AI-picked homepage link")
            if found:
                return found
    return None
