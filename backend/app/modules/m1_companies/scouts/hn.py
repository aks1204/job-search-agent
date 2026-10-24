"""HN "Ask HN: Who is hiring?" via the official Firebase API.

The post is the source of truth (owner decision, 2026-10-24): one cheap AI read per post takes out the
company, its website, what it builds, every role (with location and apply link), its careers page or job
board, and how to apply (e.g. "email the founder"). Nothing is looked up on the web for these companies —
a website the post doesn't contain is never used, so a phrase in someone else's page title can't hijack
a company (the "Strategy & Transformation Consulting" → zs.com mix-up). A post without any website is kept
as a name-only company, judged from the post, its roles reachable through the post and its email.

Without an AI key, posts following "Company | Role | Location | ..." are read line by line instead, and
the rest wait for a run that has one.
"""

import html
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field

from app.core.hosts import ats_hint, is_junk, normalize_domain
from app.modules.m1_companies.candidate import APPLY_MARK, Candidate, ContactHint
from app.modules.m1_companies.identity import name_matches_domain, tidy_name
from app.modules.m1_companies.scouts.base import Scout, ScoutContext

API = "https://hacker-news.firebaseio.com/v0"
THREAD_PREFIX = "Ask HN: Who is hiring?"
THREADS_KEPT = 2  # current + previous month's seen-lists
LLM_PARSE_PER_RUN = 150  # posts waiting from a run without an AI key, read per run

_HREF = re.compile(r'href="([^"]+)"')
_TAG = re.compile(r"<[^>]+>")
_URL = re.compile(r"https?://[^\s<>\"')\]]+")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
_AT = re.compile(r"\s*[\[\(\{<]\s*at\s*[\]\)\}>]\s*", re.I)
_DOT = re.compile(r"\s*[\[\(\{<]\s*dot\s*[\]\)\}>]\s*", re.I)
_YC_TAG = re.compile(r"\s*\((?:YC\s*)?[WSFX]\d{2}\)", re.I)
_TRAILING_PARENS = re.compile(r"\s*\([^)]*\)\s*$")


def html_to_text(raw: str) -> str:
    return html.unescape(_TAG.sub("", raw.replace("<p>", "\n")))


# --- what the post literally contains ---------------------------------------------------------------------

@dataclass
class PostFacts:
    text: str
    headline: str
    urls: list[str]
    emails: list[str]
    domains: list[str]            # websites the post links to or writes (no job boards, no webmail)
    email_domains: list[str]      # company domains of the post's email addresses (no gmail etc.)


def post_facts(raw_html: str) -> PostFacts:
    text = html_to_text(raw_html).strip()
    urls = [html.unescape(u) for u in _HREF.findall(raw_html)] + _URL.findall(text)
    urls = list(dict.fromkeys(u.rstrip(".,;") for u in urls))
    deobfuscated = _DOT.sub(".", _AT.sub("@", text))
    emails = sorted({e.lower().rstrip(".") for e in _EMAIL.findall(deobfuscated)})
    domains = list(dict.fromkeys(d for u in urls if (d := normalize_domain(u)) and not is_junk(u)))
    email_domains = list(dict.fromkeys(d for e in emails if (d := normalize_domain(e)) and not is_junk(d)))
    return PostFacts(text, text.split("\n", 1)[0].strip()[:300], urls, emails, domains, email_domains)


def _same_url(a: str, b: str) -> bool:
    strip = lambda u: re.sub(r"^https?://(www\.)?", "", u.strip().lower()).rstrip("/.,;")  # noqa: E731
    return strip(a) == strip(b)


def text_with_links(raw_html: str) -> str:
    """The post as the AI reads it: every link written out in full (HN shortens long link text to '…')."""
    return html_to_text(_ANCHOR.sub(lambda m: f" {html.unescape(m.group(1))} ", raw_html)).strip()


_ANCHOR = re.compile(r'<a\s[^>]*href="([^"]+)"[^>]*>.*?</a>', re.I | re.S)


def _fits(name: str, domain: str) -> bool:
    from app.modules.m1_companies.employers import _fit

    return name_matches_domain(name, domain) or _parts_in_name(domain, name) or _fit(name, domain) is not None


def _homepage_linked(domain: str, facts: "PostFacts") -> bool:
    """The post links the site's front page (acme.com, not acme.com/some/job-description)."""
    from urllib.parse import urlsplit

    return any(normalize_domain(u) == domain and urlsplit(u).path.strip("/") == "" for u in facts.urls)


def _in_post(url: str | None, facts: PostFacts) -> str | None:
    """The URL exactly as the post has it — or None if the post doesn't contain it."""
    if not url or url.lower() == "unknown":
        return None
    return next((u for u in facts.urls if _same_url(u, url)), None)


# --- the AI read ------------------------------------------------------------------------------------------

class HNRole(BaseModel):
    title: str = Field(description="The role's title as written, e.g. 'Senior Data Analyst'.")
    location: str = Field(description="Where, as written, e.g. 'Bangalore', 'Remote (US)', 'NYC or remote' — or 'unknown'.")
    apply_url: str = Field(description="A link in the post for THIS role (job page / apply page) — or 'unknown'.")


class HNContact(BaseModel):
    email: str = Field(description="An email address written in the post (de-obfuscate 'x [at] y dot com').")
    name: str = Field(description="The person's name if the post gives it, else 'unknown'.")
    role: str = Field(description="Their role if given (founder, CTO, recruiter…), else 'unknown'.")
    to_apply: bool = Field(description="True if the post asks candidates to apply / send a CV to this address.")


class HNRead(BaseModel):
    is_hiring_post: bool = Field(description="True if this comment advertises open roles at a company.")
    company_name: str = Field(description="The hiring company's name as the company calls itself — or 'unknown'.")
    website: str = Field(description="The company's OWN website, only if it appears in the post (a link, or the "
                                     "domain of its email address). Never a job board, ATS, docs or social "
                                     "link. 'unknown' if the post doesn't contain it.")
    careers_url: str = Field(description="A link in the post to their careers page or job board — or 'unknown'.")
    what_they_build: str = Field(description="What the company does, one plain line from the post — or 'unknown'.")
    hires_in_india: Literal["yes", "no", "unknown"] = Field(
        description="'yes' if the post offers roles based in India (or lists Indian cities); else 'unknown'.")
    remote_from_india: Literal["yes", "no", "unknown"] = Field(
        description="Could someone living in India do a remote role here? 'yes' for worldwide/anywhere/APAC/"
                    "India remote; 'no' if remote is limited to other countries/time zones; else 'unknown'.")
    visa: Literal["yes", "no", "unknown"] = Field(
        description="'yes'/'no' only if the post explicitly says whether they sponsor visas; else 'unknown'.")
    roles: list[HNRole] = Field(description="Every distinct role the post is hiring for.")
    contacts: list[HNContact] = Field(description="Every email address in the post.")


HN_SYSTEM = """You read one comment from Hacker News "Who is hiring?" and take out the hiring company's facts.
Only use what the comment says — never add a website, link or email that isn't written in it.
The first line usually reads "Company | Role | Location | ...", but some put a tagline or the role first;
the company is whoever is hiring. Answer in the requested JSON format."""


@dataclass
class PostRead:
    name: str
    website: str | None
    data: dict
    contacts: list[ContactHint] = field(default_factory=list)


def _pick_website(name: str, facts: PostFacts) -> str | None:
    """Line-by-line fallback: a website the post contains AND that fits the company's name. (No more "the only
    link must be theirs" — a lone link is as often a job-description page on someone else's site.)"""
    if "." in name and " " not in name and (d := normalize_domain(name)) and not is_junk(d):
        return d  # "Acme.ai | ..."
    for d in facts.domains + facts.email_domains:
        if name_matches_domain(name, d) or _parts_in_name(d, name):
            return d
    return None


def _parts_in_name(domain: str, name: str) -> bool:
    """strategy-transformation.com fits 'Strategy & Transformation Consulting': every part of it is a word of
    the name."""
    from app.core.hosts import domain_label
    from app.modules.m1_companies.identity import normalize_name

    parts = [p for p in domain_label(domain).split("-") if p]
    words = set(normalize_name(name).split())
    return len(parts) >= 2 and all(p in words for p in parts)


def _base_data(facts: PostFacts, by: str) -> dict:
    return {"headline": facts.headline, "text": facts.text[:4000], "parsed_by": by,
            "board_links": [u for u in facts.urls if ats_hint(u)]}


def read_by_rules(raw_html: str) -> PostRead | None:
    facts = post_facts(raw_html)
    if "|" not in facts.headline:
        return None
    name = _TRAILING_PARENS.sub("", _YC_TAG.sub("", facts.headline.split("|", 1)[0])).strip(" -–—:")
    if not name or len(name) > 60:
        return None
    parts = [p.strip() for p in facts.headline.split("|")]
    role = {"title": parts[1] if len(parts) > 1 and parts[1] else facts.headline,
            "location": parts[2] if len(parts) > 2 else None, "apply_url": None}
    data = {**_base_data(facts, "rules"), "roles": [role]}
    return PostRead(tidy_name(name), _pick_website(name, facts), data,
                    [ContactHint(email=e, context=facts.headline) for e in facts.emails])


def read_by_ai(llm, session, raw_html: str) -> PostRead | None:
    """The AI reads the post; everything it returns is checked against what the post literally contains."""
    facts = post_facts(raw_html)
    post = llm.structured("fast", system=HN_SYSTEM, schema=HNRead, purpose="m1.read_hn_post",
                          user=text_with_links(raw_html)[:6000], session=session).parsed
    name = tidy_name(post.company_name.strip())
    if not post.is_hiring_post or not name or name.lower() == "unknown" or len(name) > 60:
        return None
    website = normalize_domain(post.website) if post.website.lower() != "unknown" else None
    # Only a website the post contains — as its email's domain, its front page, or a link that fits the name
    # (a job description hosted on someone else's site is none of these).
    if not (website in facts.email_domains
            or (website in facts.domains and (_fits(name, website) or _homepage_linked(website, facts)))):
        website = _pick_website(name, facts)
    roles = [{"title": r.title.strip()[:300], "location": None if r.location.lower() == "unknown" else r.location[:200],
              "apply_url": _in_post(r.apply_url, facts)} for r in post.roles if r.title.strip()][:20]
    if not roles:
        roles = [{"title": facts.headline, "location": None, "apply_url": None}]
    unknown = lambda v: None if not v or v.lower() == "unknown" else v  # noqa: E731
    data = {**_base_data(facts, "ai"), "roles": roles, "careers_url": _in_post(post.careers_url, facts),
            "what_they_build": unknown(post.what_they_build), "hires_in_india": post.hires_in_india,
            "remote_from_india": post.remote_from_india, "visa": post.visa}
    contacts = []
    for c in post.contacts:
        email = c.email.strip().lower()
        if email not in facts.emails:
            continue                                   # an address the post doesn't contain
        contacts.append(ContactHint(email=email, name=unknown(c.name), role=unknown(c.role),
                                    context=(APPLY_MARK if c.to_apply else "") + f"HN post: {facts.headline[:200]}"))
    known = {c.email for c in contacts}
    contacts += [ContactHint(email=e, context=facts.headline) for e in facts.emails if e not in known]
    return PostRead(name, website, data, contacts)


def to_candidate(read: PostRead, *, thread_id: int, kid: int, posted) -> Candidate:
    return Candidate(
        scout="hn", external_id=f"{thread_id}:{kid}", name=read.name, domain=read.website,
        url=f"https://news.ycombinator.com/item?id={kid}", name_only_ok=True,
        data={"thread_id": thread_id, "comment_id": kid, "posted": posted, "website": read.website, **read.data},
        contacts=read.contacts,
    )


def fetch_item(http, item_id: int) -> dict | None:
    resp = http.get(f"{API}/item/{item_id}.json")
    resp.raise_for_status()
    return resp.json()


class HNScout(Scout):
    name = "hn"

    def _current_thread(self, ctx: ScoutContext) -> dict:
        resp = ctx.http.get(f"{API}/user/whoishiring.json")
        resp.raise_for_status()
        for item_id in (resp.json().get("submitted") or [])[:12]:  # newest first; 3 posts a month
            item = fetch_item(ctx.http, item_id)
            if item and (item.get("title") or "").startswith(THREAD_PREFIX):
                return item
        raise RuntimeError("no recent 'Ask HN: Who is hiring?' thread found")

    def _read(self, ctx: ScoutContext, raw_html: str) -> tuple[PostRead | None, bool]:
        """(read, done). done=False: wait for a run with an AI key (or the AI was briefly unavailable)."""
        if ctx.llm is None:
            read = read_by_rules(raw_html)
            return read, read is not None
        from app.core.llm import LLMError

        try:
            return read_by_ai(ctx.llm, ctx.session, raw_html), True
        except LLMError:
            return None, False

    def discover(self, ctx: ScoutContext) -> Iterator[Candidate]:
        thread = self._current_thread(ctx)
        threads: dict = ctx.state.setdefault("threads", {})
        key = str(thread["id"])
        entry = threads.setdefault(key, {"title": thread.get("title"), "seen": [], "unparsed": []})
        seen = set(entry["seen"])

        for old in sorted(threads, key=int)[:-THREADS_KEPT]:
            del threads[old]

        processed = 0
        for kid in thread.get("kids") or []:
            if kid in seen:
                continue
            if processed >= self.settings.max_comments_per_run:
                break
            processed += 1
            comment = fetch_item(ctx.http, kid)
            seen.add(kid)
            entry["seen"].append(kid)
            if not comment or comment.get("deleted") or comment.get("dead") or not comment.get("text"):
                continue
            read, done = self._read(ctx, comment["text"])
            if not done:
                entry["unparsed"].append(kid)
                continue
            if read is not None:
                yield to_candidate(read, thread_id=thread["id"], kid=kid, posted=comment.get("time"))

        yield from self._read_waiting(ctx, thread["id"], entry)

    def _read_waiting(self, ctx: ScoutContext, thread_id: int, entry: dict) -> Iterator[Candidate]:
        """Posts an earlier run couldn't read (no AI key then): read them now that the AI is here."""
        if ctx.llm is None or not entry["unparsed"]:
            return
        for kid in list(entry["unparsed"][:LLM_PARSE_PER_RUN]):
            comment = fetch_item(ctx.http, kid)
            if not comment or not comment.get("text"):
                entry["unparsed"].remove(kid)
                continue
            read, done = self._read(ctx, comment["text"])
            if not done:
                continue                               # still unavailable: next run
            entry["unparsed"].remove(kid)
            if read is not None:
                yield to_candidate(read, thread_id=thread_id, kid=kid, posted=comment.get("time"))
