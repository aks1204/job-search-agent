"""Enrichment, cheapest first (spec §1.3): feeder data → company website (AI reads it) → targeted search.

Startups get the full recipe. Established companies (MNCs) use the AI's general
knowledge; the careers-page check for India presence happens during activation (Part 1c).
"""

import html
import re
from datetime import date, datetime, timezone
from typing import Literal

import httpx
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.http import PoliteClient, RobotsDisallowed
from app.core.search import web_search
from app.modules.m1_companies.facts import apply_feeder_facts, upsert_fact

YesNo = Literal["yes", "no", "unknown"]


class CompanyProfile(BaseModel):
    what_they_build: str = Field(description="One plain sentence on what the company builds or sells; 'unknown' if unclear.")
    categories: list[str] = Field(description="2-6 short lowercase industry tags, e.g. 'fintech', 'b2b saas', 'logistics'.")
    business_model: Literal[
        "product", "platform", "marketplace", "consulting", "it_outsourcing", "staffing_recruiting", "other", "unknown"
    ] = Field(description="it_outsourcing = software/IT services or BPO done for clients; staffing_recruiting = staffing or recruitment agency; consulting = advisory/strategy firms.")
    blocklisted_category: Literal["none", "ecommerce", "food_delivery", "quick_commerce", "generic_marketplace"] = Field(
        description="Only if the company ITSELF is one of these consumer businesses. A B2B tool that serves e-commerce or "
        "restaurants is 'none'. generic_marketplace = an undifferentiated two-sided marketplace for everyday goods/services."
    )
    company_stage: Literal["startup", "established", "unknown"] = Field(
        description="established = large/public/long-standing (roughly 1000+ staff or listed); startup = young, venture-style."
    )
    employee_estimate: Literal["1-10", "11-50", "51-200", "201-1000", "1001-10000", "10000+", "unknown"]
    hq_location: str = Field(description="City, country — or 'unknown'.")
    india_presence: YesNo = Field(description="Has an office, team or entity in India.")
    hires_remote_from_india: YesNo = Field(description="Hires people working remotely from India (or from anywhere).")
    sponsors_visa: YesNo
    actively_hiring: YesNo


class NewsSignals(BaseModel):
    last_funding: str = Field(description="Most recent funding round and date, e.g. 'Series A, 2025-06' — or 'unknown'.")
    raised_within_window: YesNo
    distress: YesNo = Field(description="Credible recent reports of layoffs, shutdown, insolvency or acquisition-for-parts.")
    distress_detail: str = Field(description="One line with the evidence, or 'none'.")


EXTRACT_SYSTEM = """You fill in a company fact card for a job-search assistant.
You are given text from the company's own website and/or its own Hacker News job post. State only what the
text supports.
Use "unknown" whenever the text doesn't say — never guess locations, hiring or visa policy.
Return the fact card in the requested JSON format."""

KNOWLEDGE_SYSTEM = """You fill in a company fact card for a job-search assistant from your general knowledge.
Only state facts you are confident about for THIS company (match the name AND the domain);
use "unknown" otherwise. Return the fact card in the requested JSON format."""

NEWS_SYSTEM = """You read web-search snippets about a company and extract funding and distress signals.
Only use what the snippets say, and make sure they are about THIS company (name and domain), not a namesake.
Return the result in the requested JSON format."""

_DROP = re.compile(r"<(script|style|noscript|svg|template)\b.*?</\1>", re.I | re.S)
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_META_DESC = re.compile(r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']*)', re.I)
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")

PROFILE_FIELDS = (
    "what_they_build", "categories", "business_model", "blocklisted_category", "company_stage",
    "employee_estimate", "hq_location", "india_presence", "hires_remote_from_india", "sponsors_visa",
    "actively_hiring",
)


def html_to_text(raw: str, limit: int = 8000) -> str:
    title = _TITLE.search(raw)
    desc = _META_DESC.search(raw)
    body = _WS.sub(" ", html.unescape(_TAG.sub(" ", _DROP.sub(" ", raw)))).strip()
    head = " — ".join(html.unescape(m.group(1)).strip() for m in (title, desc) if m)
    return (f"{head}\n\n{body}" if head else body)[:limit]


def fetch_site_text(http: PoliteClient, domain: str) -> tuple[str, str] | None:
    """(page text, final URL) of the homepage, or None if unreachable / disallowed."""
    for url in (f"https://{domain}", f"https://www.{domain}"):
        try:
            resp = http.get(url, scrape=True)
        except (httpx.HTTPError, RobotsDisallowed):
            continue
        if resp.status_code >= 400 or "html" not in resp.headers.get("content-type", ""):
            continue
        text = html_to_text(resp.text)
        if len(text) >= 200:
            return text, str(resp.url)
    return None


def _apply_profile(session: Session, company_id: int, p: CompanyProfile, *, source: str) -> set[str]:
    changed = set()
    for key in PROFILE_FIELDS:
        if upsert_fact(session, company_id, key, getattr(p, key), "inferred", source):
            changed.add(key)
    return changed


def enrich(session: Session, company, *, llm, http: PoliteClient, profile: str) -> set[str]:
    """Fill the fact card. Returns the keys whose values changed."""
    changed = apply_feeder_facts(session, company)
    subject = ("company", str(company.id))
    site = fetch_site_text(http, company.domain) if (company.domain and profile == "startup") else None
    # Their own Hacker News job post says what they build and where they hire — read with (or instead of)
    # the website, never replaced by a guess from the AI's general knowledge.
    post = ((company.source_data or {}).get("hn") or {}).get("text")

    if site or (post and profile == "startup"):
        parts = [f"Company: {company.name}", f"Domain: {company.domain or 'unknown'}"]
        if site:
            parts += [f"Page: {site[1]}", "", "--- website text ---", site[0][:6000]]
        if post:
            parts += ["", "--- their Hacker News job post ---", post[:4000]]
        result = llm.structured(
            "fast", system=EXTRACT_SYSTEM, schema=CompanyProfile, purpose="m1.extract_website",
            user="\n".join(parts), session=session, subject=subject,
        )
        changed |= _apply_profile(session, company.id, result.parsed, source="website" if site else "hn_post")
    else:
        result = llm.structured(
            "fast", system=KNOWLEDGE_SYSTEM, schema=CompanyProfile, purpose="m1.extract_knowledge",
            user=f"Company: {company.name}\nDomain: {company.domain or 'unknown'}\nAlso known as: {', '.join(company.aliases) or '—'}"
                 + (f"\n\n--- their Hacker News job post (for context) ---\n{post[:3000]}" if post else ""),
            session=session, subject=subject,
        )
        changed |= _apply_profile(session, company.id, result.parsed, source="llm_knowledge")

    company.enriched_at = datetime.now(timezone.utc)
    return changed


def news_signals(session: Session, company, *, llm, http: PoliteClient, window_months: int, monthly_budget: int) -> bool:
    """Targeted search for funding recency + distress. Returns False when search isn't available."""
    results = web_search(
        http, f'"{company.name}" funding OR raised OR layoffs OR "shuts down"',
        purpose="enrichment", monthly_budget=monthly_budget, count=8,
    )
    if not results:
        return False
    snippets = "\n".join(f"- {r.title} ({r.url}): {r.description}" for r in results)
    result = llm.structured(
        "fast", system=NEWS_SYSTEM, schema=NewsSignals, purpose="m1.news_signals",
        user=(f"Today's date: {date.today().isoformat()}\nFunding window: last {window_months} months\n"
              f"Company: {company.name} ({company.domain})\n\nSearch results:\n{snippets}"),
        session=session, subject=("company", str(company.id)),
    )
    n = result.parsed
    upsert_fact(session, company.id, "last_funding", n.last_funding, "inferred", "news")
    upsert_fact(session, company.id, "raised_within_window", n.raised_within_window, "inferred", "news")
    upsert_fact(session, company.id, "distress", n.distress, "inferred", "news")
    if n.distress == "yes":
        upsert_fact(session, company.id, "distress_detail", n.distress_detail, "inferred", "news")
    return True
