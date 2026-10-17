"""Address lookups from enrichment free tiers (spec §3.1 ladder step 2): pooled budget, cached forever.

Every lookup (including "nothing found") is stored in enrichment_lookups, so a credit is never spent
twice on the same question. FinalScout (named in the spec) has no public API, so the pool is
Hunter first, Apollo as the reserve.
"""

import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config_loader import get_config
from app.core.http import PoliteClient
from app.core.quota import try_consume
from app.models.contact import EnrichmentLookup
from app.settings import get_settings, redact

log = logging.getLogger(__name__)


@dataclass
class Found:
    email: str
    provider: str
    vouched: bool            # the provider stands behind it (high score / verified status)
    detail: dict


def _cached(session: Session, provider: str, key: str) -> EnrichmentLookup | None:
    return session.scalar(select(EnrichmentLookup).where(EnrichmentLookup.provider == provider,
                                                         EnrichmentLookup.key == key))


def _store(session: Session, provider: str, key: str, found: bool, result: dict) -> None:
    session.add(EnrichmentLookup(provider=provider, key=key, found=found, result=result))
    session.flush()


def hunter_finder(session: Session, http: PoliteClient, domain: str, first: str, last: str) -> Found | None:
    key = f"{domain}|{first} {last}"
    if (hit := _cached(session, "hunter_finder", key)) is not None:
        return Found(hit.result["email"], "hunter", hit.result.get("vouched", False), hit.result) if hit.found else None
    api_key = get_settings().hunter_api_key
    budget = get_config().outreach.contacts.enrichment.hunter_finder_per_month
    if not api_key or budget <= 0 or not try_consume("hunter:finder", budget):
        return None
    resp = http.get("https://api.hunter.io/v2/email-finder",
                    params={"domain": domain, "first_name": first, "last_name": last},
                    headers={"X-API-KEY": api_key})
    if resp.status_code == 404:
        _store(session, "hunter_finder", key, False, {})
        return None
    resp.raise_for_status()
    data = resp.json().get("data") or {}
    email = (data.get("email") or "").lower()
    if not email:
        _store(session, "hunter_finder", key, False, {})
        return None
    status = (data.get("verification") or {}).get("status")
    result = {"email": email, "score": data.get("score"), "status": status, "accept_all": data.get("accept_all"),
              "sources": len(data.get("sources") or []),
              "vouched": status == "valid" or (data.get("score") or 0) >= 90}
    _store(session, "hunter_finder", key, True, result)
    return Found(email, "hunter", result["vouched"], result)


def hunter_pattern(session: Session, http: PoliteClient, domain: str) -> dict | None:
    """{pattern, accept_all, sample_emails} for a domain (Hunter's domain search; one credit, cached)."""
    if (hit := _cached(session, "hunter_domain", domain)) is not None:
        return hit.result if hit.found else None
    api_key = get_settings().hunter_api_key
    budget = get_config().outreach.contacts.enrichment.hunter_domain_search_per_month
    if not api_key or budget <= 0 or not try_consume("hunter:domain", budget):
        return None
    resp = http.get("https://api.hunter.io/v2/domain-search", params={"domain": domain, "limit": 10},
                    headers={"X-API-KEY": api_key})
    resp.raise_for_status()
    data = resp.json().get("data") or {}
    result = {"pattern": data.get("pattern"), "accept_all": data.get("accept_all"),
              "emails": [{"email": e.get("value"), "first": e.get("first_name"), "last": e.get("last_name"),
                          "position": e.get("position"), "confidence": e.get("confidence")}
                         for e in data.get("emails") or [] if e.get("value")]}
    found = bool(result["pattern"] or result["emails"])
    _store(session, "hunter_domain", domain, found, result)
    return result if found else None


def apollo_match(session: Session, http: PoliteClient, domain: str, first: str, last: str) -> Found | None:
    key = f"{domain}|{first} {last}"
    if (hit := _cached(session, "apollo", key)) is not None:
        return Found(hit.result["email"], "apollo", hit.result.get("vouched", False), hit.result) if hit.found else None
    api_key = get_settings().apollo_api_key
    budget = get_config().outreach.contacts.enrichment.apollo_per_month
    if not api_key or budget <= 0 or not try_consume("apollo:match", budget):
        return None
    resp = http.post("https://api.apollo.io/api/v1/people/match",
                     json={"first_name": first, "last_name": last, "domain": domain,
                           "reveal_personal_emails": False},
                     headers={"X-Api-Key": api_key, "Content-Type": "application/json"})
    resp.raise_for_status()
    person = resp.json().get("person") or {}
    email = (person.get("email") or "").lower()
    # Apollo answers "email_not_unlocked@domain.com" when the plan can't reveal it.
    if not email or email.startswith("email_not_unlocked") or not email.endswith("@" + domain):
        _store(session, "apollo", key, False, {"title": person.get("title")})
        return None
    result = {"email": email, "status": person.get("email_status"), "title": person.get("title"),
              "vouched": person.get("email_status") == "verified"}
    _store(session, "apollo", key, True, result)
    return Found(email, "apollo", result["vouched"], result)


def lookup_person(session: Session, http: PoliteClient, domain: str, first: str, last: str) -> Found | None:
    """The pool, in order. Provider errors fall through to the next provider."""
    for fn in (hunter_finder, apollo_match):
        try:
            if found := fn(session, http, domain, first, last):
                return found
        except Exception as e:
            log.info("%s lookup failed for %s: %s", fn.__name__, domain, redact(str(e))[:200])
    return None


def enrichment_available() -> bool:
    s = get_settings()
    return bool(s.hunter_api_key or s.apollo_api_key)
