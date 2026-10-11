"""Fact cards (spec §1.3): every fact tagged verified / inferred / unknown, with its source.

A fact never downgrades: an inferred value can't overwrite a verified one.
"""

import re
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.verdict import CompanyFact

CONF_RANK = {"unknown": 1, "inferred": 2, "verified": 3}
UNKNOWN = "unknown"

_INDIA = re.compile(
    r"\b(india|bengaluru|bangalore|mumbai|delhi|ncr|gurgaon|gurugram|noida|hyderabad|pune|"
    r"chennai|kolkata|ahmedabad)\b",
    re.I,
)
_REMOTE_GLOBAL = re.compile(r"\b(worldwide|anywhere|global(ly)?|remote[- ]first|fully remote|apac|asia)\b", re.I)
_REMOTE = re.compile(r"\bremote\b", re.I)


def _is_unknown(value) -> bool:
    return value is None or value == "" or value == UNKNOWN or value == []


def upsert_fact(session: Session, company_id: int, key: str, value, confidence: str, source: str) -> bool:
    """Write a fact; returns True when the stored value actually changed."""
    if _is_unknown(value):
        value, confidence = UNKNOWN, "unknown"
    row = session.scalar(select(CompanyFact).where(CompanyFact.company_id == company_id, CompanyFact.key == key))
    if row is None:
        session.add(CompanyFact(company_id=company_id, key=key, value=value, confidence=confidence, source=source))
        return confidence != "unknown"
    if CONF_RANK[confidence] < CONF_RANK[row.confidence]:
        return False
    changed = row.value != value
    row.value, row.confidence, row.source = value, confidence, source
    row.observed_at = datetime.now(timezone.utc)
    return changed


def fact_card(session: Session, company_id: int) -> dict[str, dict]:
    rows = session.scalars(select(CompanyFact).where(CompanyFact.company_id == company_id))
    return {r.key: {"value": r.value, "confidence": r.confidence, "source": r.source} for r in rows}


def value(card: dict, key: str):
    return card.get(key, {}).get("value", UNKNOWN)


def _team_bucket(n) -> str:
    try:
        n = int(n)
    except (TypeError, ValueError):
        return UNKNOWN
    for limit, label in ((10, "1-10"), (50, "11-50"), (200, "51-200"), (1000, "201-1000"), (10000, "1001-10000")):
        if n <= limit:
            return label
    return "10000+"


def _text(v) -> str:
    return " | ".join(map(str, v)) if isinstance(v, list) else str(v or "")


def feeder_facts(source_data: dict) -> list[tuple[str, object, str, str]]:
    """Facts derivable for free from what scouts already collected: (key, value, confidence, source)."""
    facts: list[tuple[str, object, str, str]] = []

    if yc := source_data.get("yc"):
        if yc.get("isHiring"):
            facts.append(("actively_hiring", "yes", "verified", "yc"))
        if yc.get("team_size"):
            facts.append(("employee_estimate", _team_bucket(yc["team_size"]), "verified", "yc"))
        where = f"{_text(yc.get('regions'))} | {_text(yc.get('all_locations'))}"
        if _INDIA.search(where):
            facts.append(("india_presence", "yes", "verified", "yc"))
        if _REMOTE.search(_text(yc.get("regions"))):
            facts.append(("hires_remote_from_india", "yes", "inferred", "yc"))
        tags = [t for k in ("industry", "subindustry", "industries", "tags") for t in _as_list(yc.get(k))]
        if tags:
            facts.append(("categories", sorted({t.lower() for t in tags}), "verified", "yc"))
        if yc.get("one_liner"):
            facts.append(("what_they_build", yc["one_liner"], "verified", "yc"))

    if hn := source_data.get("hn"):
        facts.append(("actively_hiring", "yes", "verified", "hn"))
        headline = hn.get("headline", "")
        if _INDIA.search(headline):
            facts.append(("india_presence", "yes", "inferred", "hn"))
        if _REMOTE.search(headline) and (_INDIA.search(headline) or _REMOTE_GLOBAL.search(headline)):
            facts.append(("hires_remote_from_india", "yes", "inferred", "hn"))

    if waas := source_data.get("waas"):
        facts.append(("actively_hiring", "yes", "verified", "waas"))
        loc = waas.get("location") or ""
        if _INDIA.search(loc):
            facts.append(("india_presence", "yes", "inferred", "waas"))
        if _REMOTE.search(loc) and (_INDIA.search(loc) or _REMOTE_GLOBAL.search(loc)):
            facts.append(("hires_remote_from_india", "yes", "inferred", "waas"))
        if waas.get("one_liner"):
            facts.append(("what_they_build", waas["one_liner"], "inferred", "waas"))

    if source_data.get("adzuna"):
        facts.append(("actively_hiring", "yes", "verified", "adzuna"))
        facts.append(("india_presence", "yes", "verified", "adzuna"))  # Adzuna India search

    if jooble := source_data.get("jooble"):
        facts.append(("actively_hiring", "yes", "verified", "jooble"))
        if _INDIA.search(jooble.get("location") or ""):
            facts.append(("india_presence", "yes", "verified", "jooble"))

    if remotive := source_data.get("remotive"):
        facts.append(("actively_hiring", "yes", "verified", "remotive"))
        loc = remotive.get("location") or ""
        if _INDIA.search(loc) or _REMOTE_GLOBAL.search(loc):
            facts.append(("hires_remote_from_india", "yes", "inferred", "remotive"))

    return facts


def _as_list(v) -> list[str]:
    if not v:
        return []
    if isinstance(v, list):
        return [str(x) for x in v if x]
    return [s.strip() for s in re.split(r"[;,>/]| -> ", str(v)) if s.strip()]


def apply_feeder_facts(session: Session, company) -> set[str]:
    changed = set()
    for key, val, conf, source in feeder_facts(company.source_data or {}):
        if upsert_fact(session, company.id, key, val, conf, source):
            changed.add(key)
    return changed
