"""Person search (spec §3.1): LinkedIn-scoped web-search snippets ONLY.

Queries are shaped to pull linkedin.com/in results from Brave; we read the public title + snippet
that the search engine shows. We never log into, open or crawl LinkedIn itself.

Identity is the full fingerprint — name + title + location + snippet + profile URL (+ mailbox history
later) — never the name alone. Two different profiles with the same name at the same company can't
be told apart for email purposes, so they are frozen together (spec: don't guess).
"""

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config_loader import get_config
from app.core.http import PoliteClient
from app.core.search import SearchResult, web_search
from app.models.company import Company
from app.models.contact import Contact
from app.modules.m3_outreach.names import name_key, name_tokens, split_name

log = logging.getLogger(__name__)

_PROFILE_URL = re.compile(r"^https?://([a-z]{2,3}\.)?(www\.)?linkedin\.com/in/([^/?#]+)", re.I)
_TITLE_SPLIT = re.compile(r"\s+[-–—|]\s+")


def profile_url(url: str) -> str | None:
    m = _PROFILE_URL.match(url or "")
    return f"https://www.linkedin.com/in/{m.group(3).lower()}" if m else None


@dataclass
class Snippet:
    url: str
    name: str
    headline: str           # everything after the name in the result title
    text: str               # the search engine's description


def parse_result(r: SearchResult) -> Snippet | None:
    url = profile_url(r.url)
    if not url:
        return None
    title = re.sub(r"\s*\|\s*LinkedIn\s*$", "", r.title or "", flags=re.I).strip()
    parts = _TITLE_SPLIT.split(title, maxsplit=1)
    name = parts[0].strip()
    if len(name_tokens(name)) < 2 or len(name) > 80:
        return None
    return Snippet(url, name, parts[1].strip() if len(parts) > 1 else "", (r.description or "").strip())


def build_query(company: Company, kind: str, function: str, geo: str) -> str:
    base = f'site:linkedin.com/in "{company.name}"'
    where = " India" if geo == "india" else ""
    if kind == "founder":
        return f'{base} (founder OR "co-founder" OR CEO OR CTO)'
    if kind == "recruiting":
        return f'{base} (recruiter OR "talent acquisition" OR recruiting OR "talent partner"){where}'
    fn = function.replace("_", " ")
    return f'{base} ("{fn} manager" OR "head of {fn}" OR "{fn} lead" OR "director of {fn}"){where}'


# --- reading the snippets ------------------------------------------------------------------------

class PersonRead(BaseModel):
    index: int
    works_there_now: Literal["yes", "no", "unclear"] = Field(
        description="Does the snippet show this person CURRENTLY at the company? 'ex-', 'former', "
                    "'previously' or a different current employer → no.")
    kind: Literal["founder", "recruiting", "hiring_manager", "other"] = Field(
        description="founder = founder/co-founder/CEO; recruiting = recruiter/talent acquisition/HR hiring; "
                    "hiring_manager = leads a team in the given function; other = anything else.")
    function: str = Field(description="Their function in 1-2 words, e.g. analytics, data, product, engineering, hr.")
    seniority: int = Field(description="1 junior, 2 mid, 3 senior/lead, 4 head/director, 5 VP/C-level/founder.")
    location: str = Field(description="Their location if shown, else ''.")
    geo: Literal["india", "elsewhere", "unknown"]
    title: str = Field(description="Their current job title, short.")


class PeopleRead(BaseModel):
    people: list[PersonRead]


READ_SYSTEM = """You read public web-search snippets of professional profiles to help a job seeker find
the right person to write to at one company. Be literal: only say someone works there now if the
snippet shows it. Answer in the requested JSON format."""


def _keyword_read(i: int, s: Snippet, company: Company) -> PersonRead:
    """Fallback when no AI key is set: plain keyword rules."""
    blob = f"{s.headline} {s.text}".lower()
    co = company.name.lower()
    now = "no" if re.search(rf"\b(ex-?|former|previously)\s*{re.escape(co)}", blob) else \
        "yes" if co in blob else "unclear"
    kind = "founder" if re.search(r"\b(co-?founder|founder|ceo)\b", blob) else \
        "recruiting" if re.search(r"\b(recruit|talent acquisition|talent partner|sourcer)", blob) else \
        "hiring_manager" if re.search(r"\b(manager|head of|lead|director)\b", blob) else "other"
    seniority = 5 if kind == "founder" else 4 if re.search(r"\b(head|director|vp)\b", blob) else \
        3 if re.search(r"\b(senior|lead|manager)\b", blob) else 2
    india = re.search(r"\b(india|bengaluru|bangalore|mumbai|delhi|gurgaon|gurugram|noida|hyderabad|pune|chennai)\b", blob)
    return PersonRead(index=i, works_there_now=now, kind=kind, function="", seniority=seniority, location="",
                      geo="india" if india else "unknown", title=s.headline[:120])


def read_snippets(session: Session, llm, company: Company, snippets: list[Snippet]) -> list[PersonRead]:
    if not snippets:
        return []
    if llm is None:
        return [_keyword_read(i, s, company) for i, s in enumerate(snippets)]
    lines = [f"Company: {company.name} ({company.domain})", ""]
    for i, s in enumerate(snippets):
        lines.append(f"[{i}] {s.name} — {s.headline}\n    {s.text[:400]}")
    result = llm.structured("fast", system=READ_SYSTEM, user="\n".join(lines), schema=PeopleRead,
                            purpose="m3.read_people", session=session, subject=("company", str(company.id)))
    by_index = {p.index: p for p in result.parsed.people if 0 <= p.index < len(snippets)}
    return [by_index.get(i) or _keyword_read(i, s, company) for i, s in enumerate(snippets)]


# --- storing people -----------------------------------------------------------------------------

def upsert_person(session: Session, company: Company, s: Snippet, read: PersonRead, function_of) -> Contact | None:
    parts = split_name(s.name)
    if not parts:
        return None
    first, last = parts
    contact = session.scalar(select(Contact).where(Contact.linkedin_url == s.url))
    now = datetime.now(timezone.utc)
    fields = dict(company_id=company.id, full_name=s.name, name_key=name_key(s.name), first_name=first,
                  last_name=last, title=read.title or s.headline[:500], location=read.location or None,
                  kind=read.kind, function=function_of(read.function or read.title), geo=read.geo,
                  seniority=max(1, min(5, read.seniority)), snippet=f"{s.headline}\n{s.text}"[:2000],
                  person_checked_at=now)
    if contact is None:
        contact = Contact(linkedin_url=s.url, source="search", **fields)
        session.add(contact)
    else:
        for k, v in fields.items():
            setattr(contact, k, v)
        if contact.status == "departed":
            contact.status, contact.status_reason = "active", None
    session.flush()
    return contact


def same_name_others(session: Session, contact: Contact) -> list[Contact]:
    """Other profiles at the same company with the same name (different person, or can't tell)."""
    return session.scalars(select(Contact).where(
        Contact.company_id == contact.company_id, Contact.name_key == contact.name_key,
        Contact.id != contact.id, Contact.status != "departed")).all()


def search_people(session: Session, http: PoliteClient, llm, company: Company, kind: str, function: str,
                  geo: str, function_of) -> list[tuple[Contact, PersonRead]] | None:
    """One Brave search → people who work there now in this slot kind. None when no search is possible."""
    rules = get_config().outreach.contacts
    results = web_search(http, build_query(company, kind, function, geo), purpose="contacts",
                         monthly_budget=rules.searches_per_month, count=rules.results_per_search)
    if results is None:
        return None
    snippets = [s for r in results if (s := parse_result(r))]
    found: list[tuple[Contact, PersonRead]] = []
    for s, read in zip(snippets, read_snippets(session, llm, company, snippets)):
        if read.works_there_now != "yes":
            continue
        if contact := upsert_person(session, company, s, read, function_of):
            found.append((contact, read))
    return found


def person_still_there(session: Session, http: PoliteClient, llm, contact: Contact, company: Company) -> bool | None:
    """Re-check one person (spec: 6-month on-use re-verification). True = still there, False = positive
    proof they left, None = couldn't tell (ambiguity never retires anyone)."""
    rules = get_config().outreach.contacts
    results = web_search(http, f'site:linkedin.com/in "{contact.full_name}" "{company.name}"',
                         purpose="contacts", monthly_budget=rules.searches_per_month, count=5)
    if results is None:
        return None
    contact.person_checked_at = datetime.now(timezone.utc)
    snippets = [s for r in results if (s := parse_result(r)) and s.url == contact.linkedin_url]
    if not snippets:
        return None
    read = read_snippets(session, llm, company, snippets[:1])[0]
    if read.works_there_now == "no":
        contact.status, contact.status_reason = "departed", f"search snippet: {snippets[0].headline[:200]}"
        return False
    return True if read.works_there_now == "yes" else None
