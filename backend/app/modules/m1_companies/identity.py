"""Identity resolution + dedupe (spec §1.3): domain is the true ID; names merge fuzzily; source data unions.

Rules:
- Same domain -> same company, always.
- Different known domains -> different companies, even with the same name
  ("Slice" the fintech vs "Slice" the pizza app).
- No domain yet -> match by name; a later domain attaches to that row, and if
  another row already owns the domain the two are merged.
"""

import re
import unicodedata
from datetime import datetime, timezone

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core import audit
from app.core.hosts import domain_label, is_junk, normalize_domain
from app.models.company import (
    STATUS_CANDIDATE,
    STATUS_UNRESOLVED,
    Company,
    CompanySighting,
    ContactLead,
)
from app.modules.m1_companies.candidate import Candidate

NAME_SIMILARITY = 0.9  # pg_trgm similarity required for a fuzzy name merge

# Legal / filler suffixes dropped before comparing names.
_SUFFIXES = {
    "inc", "incorporated", "llc", "llp", "ltd", "limited", "pvt", "private", "plc", "corp",
    "corporation", "co", "company", "gmbh", "ag", "sa", "bv", "pte", "technologies",
    "technology", "tech", "labs", "lab", "solutions", "software", "group", "holdings", "hq",
    "and",  # left dangling by "McKinsey & Company" once "company" is dropped
}

# Whose word wins on company type: the owner's seed list, then curated packs, then YC, then anyone else.
_TYPE_AUTHORITY = {"seeds": 4, "packs": 3, "yc": 2}


def normalize_name(name: str) -> str:
    """'Acme Technologies Pvt. Ltd.' -> 'acme'; 'Bristol-Myers Squibb' -> 'bristol myers squibb'."""
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    words = re.sub(r"[^a-z0-9]+", " ", s.replace("&", " and ")).split()
    while len(words) > 1 and words[-1] in _SUFFIXES:
        words.pop()
    return " ".join(words)


def tidy_name(name: str) -> str:
    """Shouting capitals become a name: 'ARTIFICIAL ANALYSIS' → 'Artificial Analysis', 'BREAKFAST Studio' →
    'Breakfast Studio'. Short acronyms stay as they are (ICEYE, FUTO, AWS)."""
    return " ".join(w.capitalize() if w.isalpha() and w.isupper() and len(w) > 5 else w
                    for w in name.strip().split())


def name_matches_domain(name: str, domain: str) -> bool:
    """Does the domain plausibly belong to this name? 'Acme Labs' ~ acmelabs.io / acme.ai."""
    label = domain_label(domain).replace("-", "")
    words = normalize_name(name).split()
    if not words or len(label) < 3:
        return False
    joined = "".join(words)
    initials = "".join(w[0] for w in words) if len(words) > 1 else ""
    return label in (joined, words[0], initials) or (len(joined) >= 4 and joined in label)


def site_mentions_name(text: str, name: str) -> bool:
    """Does a homepage's text name the company? Used to confirm AI-suggested domains."""
    words = [w for w in normalize_name(name).split() if len(w) >= 3] or normalize_name(name).split()
    lowered = text[:4000].lower()
    return bool(words) and all(w in lowered for w in words)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _clean_domain(value: str | None) -> str | None:
    domain = normalize_domain(value)
    return None if domain is None or is_junk(domain) else domain


def find_by_name(session: Session, name: str, *, domain: str | None) -> Company | None:
    """Best name match. If we know a domain, only domain-less rows are eligible."""
    normalized = normalize_name(name)
    if not normalized:
        return None
    similarity = func.similarity(Company.normalized_name, normalized)
    stmt = select(Company).where(
        or_(Company.normalized_name == normalized, similarity >= NAME_SIMILARITY)
    )
    if domain is not None:
        stmt = stmt.where(Company.domain.is_(None))
    stmt = stmt.order_by((Company.normalized_name == normalized).desc(), similarity.desc()).limit(1)
    return session.scalars(stmt).first()


def _absorb(company: Company, cand: Candidate) -> None:
    """Union a sighting's facts into the company row."""
    name = cand.name.strip()[:255]
    if name.lower() != company.name.strip().lower() and name not in company.aliases:
        company.aliases = [*company.aliases, name]
    if cand.data:
        company.source_data = {**company.source_data, cand.scout: cand.data}
    if cand.type_hint and cand.type_hint != company.type:
        if _TYPE_AUTHORITY.get(cand.scout, 1) >= _TYPE_AUTHORITY.get(company.type_source or "", 0):
            company.type = cand.type_hint
            company.type_source = cand.scout


def _add_contacts(session: Session, company_id: int, cand: Candidate) -> None:
    for hint in cand.contacts:
        email = hint.email.strip().lower()
        exists = session.scalar(
            select(ContactLead.id).where(ContactLead.company_id == company_id, ContactLead.email == email)
        )
        if not exists:
            session.add(ContactLead(
                company_id=company_id, email=email, name=hint.name, role=hint.role,
                source=cand.scout, context=hint.context,
            ))


def merge_companies(session: Session, keep: Company, drop: Company, *, reason: str) -> Company:
    """Fold `drop` into `keep`: move sightings and contacts, union names and source data."""
    for sighting in session.scalars(select(CompanySighting).where(CompanySighting.company_id == drop.id)).all():
        sighting.company_id = keep.id
    keep_emails = set(session.scalars(select(ContactLead.email).where(ContactLead.company_id == keep.id)))
    for lead in session.scalars(select(ContactLead).where(ContactLead.company_id == drop.id)).all():
        if lead.email in keep_emails:
            session.delete(lead)
        else:
            lead.company_id = keep.id
    names = [drop.name, *drop.aliases]
    keep.aliases = [*keep.aliases, *(n for n in names if n != keep.name and n not in keep.aliases)]
    keep.source_data = {**drop.source_data, **keep.source_data}
    if keep.type == "unknown" and drop.type != "unknown":
        keep.type, keep.type_source = drop.type, drop.type_source
    audit.record(
        session, "m1", "company_merged", f"Merged “{drop.name}” into {keep.name} ({keep.domain})",
        subject_type="company", subject_id=str(keep.id),
        detail={"reason": reason, "merged_id": drop.id, "merged_name": drop.name},
    )
    session.flush()
    session.delete(drop)
    session.flush()
    return keep


def assign_domain(session: Session, company: Company, domain: str, *, source: str) -> Company:
    """Give an unresolved company its identity; merge if that domain is already known."""
    owner = session.scalar(select(Company).where(Company.domain == domain, Company.id != company.id))
    if owner is not None:
        return merge_companies(session, owner, company, reason=f"domain {domain} found via {source}")
    company.domain = domain
    company.domain_source = source
    company.website = company.website or f"https://{domain}"
    if company.status == STATUS_UNRESOLVED:
        company.status = STATUS_CANDIDATE
    return company


CURATED_SCOUTS = ("seeds", "packs", "yc")   # names written with care: kept exactly (NVIDIA stays NVIDIA)


def _websiteless_namesake(session: Session, name: str) -> Company | None:
    """A post without a website joins only a company that has no website either and exactly this name — never
    a same-name company elsewhere (its post's gmail address would become that company's contact), and never
    one dropped because its only post turned out to be someone else's."""
    from app.models.verdict import CompanyVerdict

    dropped = select(CompanyVerdict.company_id).where(CompanyVerdict.failed_check == "misattributed")
    return session.scalar(select(Company).where(Company.domain.is_(None),
                                                Company.normalized_name == normalize_name(name),
                                                Company.id.not_in(dropped)).limit(1))


def intake(session: Session, cand: Candidate) -> str:
    """Record one sighting. Returns 'new' | 'matched' | 'seen'."""
    now = _utcnow()
    existing = session.scalar(
        select(CompanySighting).where(
            CompanySighting.scout == cand.scout, CompanySighting.external_id == cand.external_id
        )
    )
    if existing is not None:
        existing.last_seen_at = now
        existing.data = cand.data
        company = session.get(Company, existing.company_id)
        _absorb(company, cand)
        if cand.name_only_ok and company.status == STATUS_UNRESOLVED:
            company.status = STATUS_CANDIDATE
        _add_contacts(session, company.id, cand)
        return "seen"

    domain = _clean_domain(cand.domain)
    company = None
    outcome = "matched"
    if domain:
        company = session.scalar(select(Company).where(Company.domain == domain))
    if company is None and cand.name_only_ok and not domain:
        company = _websiteless_namesake(session, cand.name)
    elif company is None:
        company = find_by_name(session, cand.name, domain=domain)
        if company is not None and domain:
            company = assign_domain(session, company, domain, source="seed" if cand.scout == "seeds" else "source")
    if company is None:
        company = Company(
            name=(cand.name.strip() if cand.scout in CURATED_SCOUTS else tidy_name(cand.name))[:255],
            normalized_name=normalize_name(cand.name)[:255],
            domain=domain,
            domain_source=("seed" if cand.scout == "seeds" else "source") if domain else None,
            website=f"https://{domain}" if domain else None,
            status=STATUS_CANDIDATE if domain or cand.name_only_ok else STATUS_UNRESOLVED,
            aliases=[],
            source_data={},
        )
        session.add(company)
        session.flush()
        outcome = "new"

    _absorb(company, cand)
    if cand.name_only_ok and company.status == STATUS_UNRESOLVED:
        company.status = STATUS_CANDIDATE   # the post is enough to judge it: no web search for its website
    session.add(CompanySighting(
        company_id=company.id, scout=cand.scout, external_id=cand.external_id,
        name_seen=cand.name[:255], domain_seen=domain, url=cand.url, data=cand.data,
        first_seen_at=now, last_seen_at=now,
    ))
    _add_contacts(session, company.id, cand)
    session.flush()
    if outcome == "matched":
        from app.modules.m1_companies.revival import on_new_sighting  # late import: revival → evaluator deps

        on_new_sighting(session, company, cand.scout)
    return outcome
