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

from app.core.hosts import (
    PLATFORMS, PROFILE_SITES, ats_hint, ats_hints_in, detect_vendors, domain_label, is_ats_url, is_junk,
    normalize_domain,
)
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
    vendors = detect_vendors(raw_html)
    hints = ats_hints_in(raw_html)
    host = urlsplit(base_url).hostname
    for platform in PLATFORMS:   # Jibe / Phenom / TalentBrew: this page is the careers site, read from its host
        if platform in vendors and host and not any(h["vendor"] == platform for h in hints):
            hints.append({"vendor": platform, "slug": host})
    return PageScan(url=base_url, links=links, ats_hints=hints, job_links=job_links, vendors=vendors)


def fetch_html(http: PoliteClient, url: str) -> tuple[str, str] | None:
    """(final URL, raw HTML) or None. Respects robots.txt."""
    try:
        resp = http.get(url, scrape=True)
    except (httpx.HTTPError, RobotsDisallowed):
        return None
    if resp.status_code >= 400 or "html" not in resp.headers.get("content-type", ""):
        return None
    return str(resp.url), resp.text


def _on_domain(url: str, domain: str | set[str]) -> bool:
    """On the company's domain — or, given a set, on any of its own domains (the site plus its careers site)."""
    found = normalize_domain(url)
    return found in domain if isinstance(domain, set) else found == domain


_GENERIC_LABELS = {"jobs", "job", "career", "careers", "work", "hire", "hiring", "talent", "join", "apply", "search"}


def _careers_domain(url: str, company) -> str | None:
    """A careers site on a domain of its own that carries the company's name: amazon.com → amazon.jobs,
    meta.com → metacareers.com, citigroup.com (Citi) → jobs.citi.com, barclays.com → search.jobs.barclays.
    Trusted only for a Careers/Jobs link on the company's own homepage — that link is what makes it theirs.
    Only the domain itself is compared, never what's in front of it (barclays.csod.com is Cornerstone's)."""
    from app.modules.m1_companies.identity import normalize_name

    found = normalize_domain(url)
    if not found or found == company.domain or is_junk(url) or found in PROFILE_SITES:
        return None
    words = normalize_name(company.name).split()
    # A one-word name is a name ("Citi"); a longer one only as a whole — "Health Catalyst" ≠ healthecareers.com.
    names = {n for n in {domain_label(company.domain), "".join(words)} if len(n) >= 4}
    label = domain_label(found)
    suffix = found[len(label) + 1:]                      # a brand's own ending: search.jobs.barclays
    for part in (label, suffix):
        if any(n in part or (len(part) >= 4 and part not in _GENERIC_LABELS and n.startswith(part)) for n in names):
            return found
    return None


def _real_hints(scan: PageScan) -> list[dict]:
    """Job systems on the page other than a careers-site platform front (Phenom, Jibe, TalentBrew)."""
    return [h for h in scan.ats_hints if h["vendor"] not in PLATFORMS]


def _check(http: PoliteClient, url: str, domain: str | set[str],
           how: str, company=None) -> tuple[Discovery | None, PageScan | None]:
    """(Discovery if this page lists jobs, the page scan if it was fetchable on-site)."""
    page = fetch_html(http, url)
    if page is None:
        return None, None
    final_url, raw = page
    if (isinstance(domain, set) and company is not None and _on_domain(url, domain)
            and not _on_domain(final_url, domain) and (careers_site := _careers_domain(final_url, company))):
        domain.add(careers_site)   # their own page sent us to their careers site (citigroup.com/careers → jobs.citi.com)
    if not (_on_domain(final_url, domain) or is_ats_url(final_url)):
        return None, None  # redirected off-site (e.g. to LinkedIn) — not trustworthy
    scan = scan_page(raw, final_url)
    own = ats_hint(final_url)  # the link led straight onto a job board's own page
    if own and own not in scan.ats_hints:
        scan.ats_hints.insert(0, own)
    if scan.is_listing:
        return Discovery(final_url, scan, how, on_company_site=_on_domain(final_url, domain)), scan
    return None, scan


_STRONG_JOB_TEXT = re.compile(
    r"\b((search|view|see|browse|explore|find|all|open)\s+)?(jobs|roles|positions|openings|opportunities|vacancies)\b",
    re.I,
)
DEEPER_LINKS = 3


def _deeper_links(scan: PageScan | None, domain: str | set[str], tried: set[str]) -> list[tuple[str, str]]:
    """The most promising next hops from a careers landing page, best first."""
    if scan is None:
        return []
    here = urlsplit(scan.url).netloc
    ranked = []
    for href, text in scan.links:
        if href in tried or not (_on_domain(href, domain) or is_ats_url(href)):
            continue
        if not (CAREER_WORDS.search(text) or _STRONG_JOB_TEXT.search(text)):
            continue
        score = (3 if is_ats_url(href) else 0) + (2 if urlsplit(href).netloc != here else 0) \
            + (1 if _STRONG_JOB_TEXT.search(text) else 0)
        ranked.append((score, href, text))
    ranked.sort(key=lambda r: -r[0])
    best: dict[str, str] = {}
    for _, href, text in ranked:
        best.setdefault(href, text)
    return list(best.items())[:DEEPER_LINKS]


_LOC = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.I)
_SITEMAP_LINE = re.compile(r"^\s*sitemap:\s*(\S+)", re.I | re.M)
SITEMAP_JOB_PAGES = 2


def _get_text(http: PoliteClient, url: str) -> str | None:
    try:
        resp = http.get(url, scrape=True)
    except (httpx.HTTPError, RobotsDisallowed):
        return None
    return resp.text if resp.status_code < 400 else None


def _from_sitemaps(http: PoliteClient, hosts: list[str], domain: str | set[str], seen: set[str]) -> Discovery | None:
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

    # A careers-site platform (Phenom, Jibe, TalentBrew) is only a front: a real job system behind it (e.g.
    # Workday — far more jobs, full descriptions) is looked for first; the front is the second choice.
    platform_only: Discovery | None = None

    # Rung 1a: the homepage itself links to an ATS board.
    if home_scan and _real_hints(home_scan):
        return Discovery(home_scan.url, home_scan, "ATS link on homepage", on_company_site=True)
    if home_scan and home_scan.ats_hints:
        platform_only = Discovery(home_scan.url, home_scan, "careers platform on homepage", on_company_site=True)

    # Rung 1b: nav links that say careers/jobs, then the usual URL guesses.
    candidates: list[tuple[str, str]] = []
    own = {domain}   # the company's domains: its site, plus a careers site its homepage links to (amazon.jobs)
    if home_scan:
        for href, text in home_scan.links:
            if not (CAREER_WORDS.search(text) or CAREER_WORDS.search(urlsplit(href).path)):
                continue
            if _on_domain(href, own) or is_ats_url(href):
                candidates.append((href, f"homepage link “{text or href}”"))
            elif CAREER_WORDS.search(text) and (careers_site := _careers_domain(href, company)):   # its text, too
                own.add(careers_site)
                candidates.append((href, f"homepage link “{text or href}” (their careers site)"))
    for guess in (f"https://{domain}/careers", f"https://{domain}/jobs",
                  f"https://careers.{domain}", f"https://jobs.{domain}"):
        candidates.append((guess, "URL guess"))

    # A page that only *looks* like a listing (many /careers/… links) is kept as a fallback while we keep
    # looking for an actual job board — Amex's landing page has 37 such links, and its one "Jobs" link
    # leads to the real (Oracle) board.
    fallback: Discovery | None = None

    def consider(found: Discovery | None) -> Discovery | None:
        nonlocal fallback, platform_only
        if found and _real_hints(found.scan):
            return found
        if found and found.scan.ats_hints:          # only a careers-site platform: keep looking
            platform_only = platform_only or found
        elif found and fallback is None:
            fallback = found
        return None

    tried = set()
    for url, how in candidates:
        if url in tried or len(tried) >= MAX_CANDIDATES:
            continue
        tried.add(url)
        found, scan = _check(http, url, own, how, company)
        if scan:
            seen |= scan.vendors
        if board := consider(found):
            return board
        # One level deeper: careers landing pages link on to "Jobs" / "Open roles" / a careers subdomain.
        for href, text in _deeper_links(scan, own, tried):
            tried.add(href)
            found, deeper = _check(http, href, own, f"{how} → “{text}”", company)
            if deeper:
                seen |= deeper.vendors
            if board := consider(found):
                return board

    # Rung 1c: JavaScript careers sites (Phenom & co.) hide the board from the landing page, but publish
    # sitemaps for crawlers, and a job page's server-rendered "Apply" link names the exact board.
    hosts = [f"https://{domain}"] + [
        f"{urlsplit(u).scheme}://{urlsplit(u).netloc}" for u in tried if _on_domain(u, own)
    ]
    if board := consider(_from_sitemaps(http, list(dict.fromkeys(hosts))[:4], own, seen)):
        return board

    # Rung 2: web search, keeping only the company's own site or ATS boards (never LinkedIn/Glassdoor).
    if platform_only is None and search_available():   # (a platform front already found: no search spend)
        results = web_search(http, f'"{company.name}" careers jobs', purpose="careers",
                             monthly_budget=CAREERS_SEARCH_BUDGET) or []
        for r in results[:5]:
            if (_on_domain(r.url, own) or is_ats_url(r.url)) and r.url not in tried:
                tried.add(r.url)
                found, _ = _check(http, r.url, own, "web search")
                if board := consider(found):
                    return board

    # Rung 3: the AI picks from the homepage links.
    if platform_only is None and llm is not None and home_scan and home_scan.links:
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
            found, _ = _check(http, pick.url, own, "AI-picked homepage link")
            if board := consider(found):
                return board
    # A careers-site platform with nothing better behind it (BCG); else an on-site page of job links with no
    # known board (custom careers page), or None.
    return platform_only or fallback
