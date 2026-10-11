"""HN "Ask HN: Who is hiring?" via the official Firebase API.

Most comments follow the convention "Company | Role | Location | ...", which we
parse deterministically. Comments that don't are remembered as `unparsed` for
Part 1b's LLM pass. Contact emails found along the way are kept for Module 3.
"""

import html
import re
from collections.abc import Iterator
from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from app.core.hosts import ats_hint, is_junk, normalize_domain
from app.modules.m1_companies.candidate import Candidate, ContactHint
from app.modules.m1_companies.identity import name_matches_domain
from app.modules.m1_companies.scouts.base import Scout, ScoutContext

API = "https://hacker-news.firebaseio.com/v0"
THREAD_PREFIX = "Ask HN: Who is hiring?"
THREADS_KEPT = 2  # current + previous month's seen-lists
LLM_PARSE_PER_RUN = 150  # free-text posts handed to the cheap AI model per run

_HREF = re.compile(r'href="([^"]+)"')
_TAG = re.compile(r"<[^>]+>")
_URL = re.compile(r"https?://[^\s<>\"')\]]+")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
_AT = re.compile(r"\s*[\[\(\{<]\s*at\s*[\]\)\}>]\s*", re.I)
_DOT = re.compile(r"\s*[\[\(\{<]\s*dot\s*[\]\)\}>]\s*", re.I)
_YC_TAG = re.compile(r"\s*\((?:YC\s*)?[WSFX]\d{2}\)", re.I)
_TRAILING_PARENS = re.compile(r"\s*\([^)]*\)\s*$")


@dataclass
class ParsedComment:
    name: str
    headline: str
    website: str | None
    emails: list[str] = field(default_factory=list)
    ats_links: list[str] = field(default_factory=list)
    text: str = ""


def html_to_text(raw: str) -> str:
    return html.unescape(_TAG.sub("", raw.replace("<p>", "\n")))


def _pick_website(name: str, urls: list[str], emails: list[str]) -> str | None:
    if "." in name and " " not in name and (d := normalize_domain(name)) and not is_junk(d):
        return d  # "Acme.ai | ..."
    domains = list(dict.fromkeys(d for u in urls if (d := normalize_domain(u)) and not is_junk(u)))
    email_domains = [d for e in emails if (d := normalize_domain(e)) and not is_junk(d)]
    for d in domains + email_domains:
        if name_matches_domain(name, d):
            return d
    if len(domains) == 1:
        return domains[0]  # the only real link in the post
    return None


def parse_comment(raw_html: str) -> ParsedComment | None:
    text = html_to_text(raw_html).strip()
    headline = text.split("\n", 1)[0].strip()
    if "|" not in headline:
        return None
    name = _TRAILING_PARENS.sub("", _YC_TAG.sub("", headline.split("|", 1)[0])).strip(" -–—:")
    if not name or len(name) > 60:
        return None

    urls = [html.unescape(u) for u in _HREF.findall(raw_html)] + _URL.findall(text)
    urls = list(dict.fromkeys(u.rstrip(".,;") for u in urls))
    deobfuscated = _DOT.sub(".", _AT.sub("@", text))
    emails = sorted({e.lower().rstrip(".") for e in _EMAIL.findall(deobfuscated)})

    return ParsedComment(
        name=name,
        headline=headline[:300],
        website=_pick_website(name, urls, emails),
        emails=emails,
        ats_links=[u for u in urls if ats_hint(u)],
        text=text[:4000],
    )


class HNScout(Scout):
    name = "hn"

    def _item(self, ctx: ScoutContext, item_id: int) -> dict | None:
        resp = ctx.http.get(f"{API}/item/{item_id}.json")
        resp.raise_for_status()
        return resp.json()

    def _current_thread(self, ctx: ScoutContext) -> dict:
        resp = ctx.http.get(f"{API}/user/whoishiring.json")
        resp.raise_for_status()
        for item_id in (resp.json().get("submitted") or [])[:12]:  # newest first; 3 posts a month
            item = self._item(ctx, item_id)
            if item and (item.get("title") or "").startswith(THREAD_PREFIX):
                return item
        raise RuntimeError("no recent 'Ask HN: Who is hiring?' thread found")

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
            comment = self._item(ctx, kid)
            seen.add(kid)
            entry["seen"].append(kid)
            if not comment or comment.get("deleted") or comment.get("dead") or not comment.get("text"):
                continue
            parsed = parse_comment(comment["text"])
            if parsed is None:
                entry["unparsed"].append(kid)
                continue
            yield Candidate(
                scout=self.name,
                external_id=f"{key}:{kid}",
                name=parsed.name,
                domain=parsed.website,
                url=f"https://news.ycombinator.com/item?id={kid}",
                data={
                    "thread_id": thread["id"],
                    "comment_id": kid,
                    "posted": comment.get("time"),
                    "headline": parsed.headline,
                    "ats_links": parsed.ats_links,
                    "text": parsed.text,
                },
                contacts=[ContactHint(email=e, context=parsed.headline) for e in parsed.emails],
            )

        yield from self._parse_leftovers(ctx, key, entry)

    def _parse_leftovers(self, ctx: ScoutContext, key: str, entry: dict) -> Iterator[Candidate]:
        """Free-text posts the deterministic parser skipped: the cheap AI model reads them."""
        if ctx.llm is None or not entry["unparsed"]:
            return
        from app.core.llm import LLMError

        for kid in list(entry["unparsed"][:LLM_PARSE_PER_RUN]):
            entry["unparsed"].remove(kid)
            comment = self._item(ctx, kid)
            if not comment or not comment.get("text"):
                continue
            try:
                post = ctx.llm.structured(
                    "fast", system=HN_SYSTEM, schema=HNPost, purpose="m1.parse_hn",
                    user=html_to_text(comment["text"])[:4000], session=ctx.session,
                ).parsed
            except LLMError:
                continue
            name = post.company_name.strip()
            if not post.is_hiring_post or not name or name.lower() == "unknown" or len(name) > 60:
                continue
            emails = [e.lower() for e in post.emails if _EMAIL.fullmatch(e)]
            yield Candidate(
                scout=self.name,
                external_id=f"{key}:{kid}",
                name=name,
                domain=None if post.website.lower() == "unknown" else post.website,
                url=f"https://news.ycombinator.com/item?id={kid}",
                data={"thread_id": int(key), "comment_id": kid, "posted": comment.get("time"),
                      "headline": html_to_text(comment["text"])[:300], "parsed_by": "llm"},
                contacts=[ContactHint(email=e, context="HN post (AI-parsed)") for e in emails],
            )


class HNPost(BaseModel):
    is_hiring_post: bool = Field(description="True if this comment advertises open roles at a company.")
    company_name: str = Field(description="The hiring company's name, or 'unknown'.")
    website: str = Field(description="The company's own website domain, or 'unknown'. Never a job board or ATS link.")
    emails: list[str] = Field(description="Contact email addresses written in the post (de-obfuscate 'x [at] y').")


HN_SYSTEM = """You read one comment from Hacker News "Who is hiring?" and extract the hiring company.
Only use what the comment says. Answer in the requested JSON format."""
