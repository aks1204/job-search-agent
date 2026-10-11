"""Domain normalisation and host classification.

A company's identity is its registrable domain (e.g. "acme.co.in"). Job boards,
social networks, link shorteners and ATS vendors are never a company's identity.
"""

import re
from urllib.parse import urlsplit

import tldextract

# Bundled public-suffix snapshot only: no network fetch at runtime.
_extract = tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None)

# Registrable domains that are never a company's identity.
JUNK_DOMAINS = frozenset({
    # job boards / aggregators (also hard boundary: never crawled)
    "linkedin.com", "glassdoor.com", "glassdoor.co.in", "indeed.com", "naukri.com",
    "adzuna.com", "adzuna.in", "jooble.org", "remotive.com", "wellfound.com", "angel.co",
    "ycombinator.com", "workatastartup.com", "instahyre.com", "cutshort.io", "hirist.tech",
    "foundit.in", "monster.com", "simplyhired.com", "ziprecruiter.com", "builtin.com",
    "weworkremotely.com", "remoteok.com", "otta.com",
    # social / content / tools
    "twitter.com", "x.com", "facebook.com", "instagram.com", "youtube.com", "github.com",
    "gitlab.com", "medium.com", "substack.com", "notion.site", "notion.so", "calendly.com",
    "typeform.com", "forms.gle", "goo.gl", "bit.ly", "tinyurl.com", "linktr.ee",
    "crunchbase.com", "wikipedia.org", "hn.algolia.com",
    # webmail (valid contact addresses, but never a company's identity)
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "yahoo.com", "proton.me",
    "protonmail.com", "icloud.com",
})

# Full hostnames that are junk even though their registrable domain is a real company.
JUNK_HOSTS = frozenset({
    "docs.google.com", "drive.google.com", "sites.google.com", "forms.google.com",
    "news.ycombinator.com", "ats.rippling.com", "jobs.ashbyhq.com",
})

# Applicant-tracking-system vendors: a link here is a great *feed* hint (Part 1c)
# but says nothing about the company's own domain.
ATS_DOMAINS = frozenset({
    "greenhouse.io", "lever.co", "ashbyhq.com", "workable.com", "smartrecruiters.com",
    "recruitee.com", "myworkdayjobs.com", "bamboohr.com", "breezy.hr", "jobvite.com",
    "icims.com", "teamtailor.com", "personio.de", "personio.com", "zohorecruit.com",
    "zohorecruit.in", "keka.com", "darwinbox.in", "freshteam.com", "pinpointhq.com",
})

_ATS_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("greenhouse", re.compile(
        r"^(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_board(?:/js)?\?for=)?([\w-]+)", re.I)),
    ("lever", re.compile(r"^jobs(?:\.eu)?\.lever\.co/([\w.-]+)", re.I)),
    ("ashby", re.compile(r"^jobs\.ashbyhq\.com/([\w.%-]+)", re.I)),
    ("workable", re.compile(r"^apply\.workable\.com/([\w-]+)", re.I)),
    ("smartrecruiters", re.compile(r"^(?:jobs|careers)\.smartrecruiters\.com/([\w-]+)", re.I)),
    ("recruitee", re.compile(r"^([\w-]+)\.recruitee\.com", re.I)),
    ("workday", re.compile(r"^([\w-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[a-z]{2}/)?([\w-]+)", re.I)),
]

# Path segments that look like slugs but aren't.
_NOT_SLUGS = {"embed", "api", "js", "v0", "v1", "jobs", "careers", "job", "apply", "www", "app", "widget", "en-us"}

# Any ATS URL inside raw HTML/JS (hrefs, iframes, script srcs, JSON config).
ATS_URL = re.compile(
    r"(?:https?:)?//[a-z0-9.-]*(?:greenhouse\.io|lever\.co|ashbyhq\.com|workable\.com|smartrecruiters\.com|"
    r"recruitee\.com|myworkdayjobs\.com)(?:/[^\s\"'<>)\\]*)?",
    re.I,
)


def hostname(url_or_host: str | None) -> str | None:
    if not url_or_host:
        return None
    v = url_or_host.strip().lower()
    if "@" in v and "://" not in v:
        v = v.rsplit("@", 1)[1]
    if "://" not in v:
        v = "http://" + v
    try:
        host = urlsplit(v).hostname
    except ValueError:
        return None
    return host.rstrip(".") if host else None


def normalize_domain(url_or_host: str | None) -> str | None:
    """'https://www.Acme.co.in/careers' -> 'acme.co.in'; 'jobs@acme.ai' -> 'acme.ai'."""
    host = hostname(url_or_host)
    if not host:
        return None
    ext = _extract(host)
    if not ext.domain or not ext.suffix:
        return None
    return f"{ext.domain}.{ext.suffix}"


def is_ats_url(url: str | None) -> bool:
    return normalize_domain(url) in ATS_DOMAINS


def is_junk(url_or_host: str | None) -> bool:
    """True when this host can never identify a company (boards, social, ATS, shorteners)."""
    host = hostname(url_or_host)
    domain = normalize_domain(url_or_host)
    if not host or not domain:
        return True
    return host in JUNK_HOSTS or domain in JUNK_DOMAINS or domain in ATS_DOMAINS


def ats_hint(url: str | None) -> dict | None:
    """'https://jobs.lever.co/acme/123' -> {'vendor': 'lever', 'slug': 'acme'}."""
    if not url:
        return None
    stripped = re.sub(r"^(?:https?:)?//", "", url.strip(), flags=re.I)
    for vendor, pattern in _ATS_PATTERNS:
        m = pattern.match(stripped)
        if not m:
            continue
        if vendor == "workday":
            tenant, wd, site = m.group(1), m.group(2), m.group(3)
            if site.lower() in _NOT_SLUGS:
                return None
            return {"vendor": vendor, "slug": tenant, "site": site, "host": f"{tenant}.{wd}"}
        slug = m.group(1)
        if slug.lower() in _NOT_SLUGS:
            return None
        return {"vendor": vendor, "slug": slug}
    return None


def ats_hints_in(raw_html: str) -> list[dict]:
    """Every distinct ATS board referenced anywhere in a page."""
    text = raw_html.replace("\\/", "/")
    seen, hints = set(), []
    for m in ATS_URL.finditer(text):
        hint = ats_hint(m.group(0))
        if hint:
            key = (hint["vendor"], hint["slug"].lower(), hint.get("site"))
            if key not in seen:
                seen.add(key)
                hints.append(hint)
    return hints


def domain_label(domain: str) -> str:
    """'acme.co.in' -> 'acme'."""
    return _extract(domain).domain
