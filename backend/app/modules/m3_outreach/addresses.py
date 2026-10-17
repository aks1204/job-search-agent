"""Address ladder (spec §3.1, owner's order): published → pooled enrichment → construction → none.

Construction learns the domain's pattern from one known-real "anchor" address (a published,
provider-found or mailbox-proven address of a named person), else tries common formats (startups
skew first@) and keeps the first one the verifier confirms.
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config_loader import get_config
from app.core.http import PoliteClient
from app.models.company import Company
from app.models.contact import CONFIDENCE_RANK, Contact, ContactEmail, MailDomain
from app.modules.m3_outreach import enrich
from app.modules.m3_outreach.names import infer_pattern, render
from app.modules.m3_outreach.verify import lookup_mx, mail_domain, smtp_check, verify_candidates

TRUSTED_SOURCES = ("published", "hunter", "apollo", "manual")


def mail_domain_for(company: Company) -> str:
    return get_config().outreach.contacts.mail_domains.get(company.domain, company.domain)


def best_address(session: Session, contact: Contact) -> ContactEmail | None:
    rows = session.scalars(select(ContactEmail).where(ContactEmail.contact_id == contact.id)).all()
    usable = [r for r in rows if r.confidence != "invalid"]
    return max(usable, key=lambda r: (CONFIDENCE_RANK[r.confidence], r.vouched), default=None)


def learn_pattern(session: Session, company: Company, md: MailDomain) -> str | None:
    """Pattern from an anchor: a real address of a named person at this domain."""
    if md.pattern and not md.pattern_distrusted:
        return md.pattern
    anchors = session.execute(
        select(ContactEmail, Contact).join(Contact, Contact.id == ContactEmail.contact_id)
        .where(ContactEmail.company_id == company.id, ContactEmail.email.like(f"%@{md.domain}"),
               ContactEmail.confidence != "invalid")
    ).all()
    for row, person in sorted(anchors, key=lambda x: -CONFIDENCE_RANK[x[0].confidence]):
        trusted = row.confidence == "proven" or (row.source in TRUSTED_SOURCES and
                                                 (row.confidence == "verified" or row.vouched))
        if trusted and (pattern := infer_pattern(row.email, person.full_name)):
            if md.pattern_distrusted and pattern == md.pattern:
                continue
            md.pattern, md.pattern_source, md.pattern_distrusted = pattern, row.email, False
            return pattern
    return None


def _taken(session: Session, company_id: int, email: str) -> ContactEmail | None:
    return session.scalar(select(ContactEmail).where(ContactEmail.company_id == company_id,
                                                     ContactEmail.email == email))


def _add(session: Session, company: Company, contact: Contact | None, email: str, source: str, *,
         pattern: str | None = None, vouched: bool = False, detail: dict | None = None) -> ContactEmail:
    row = ContactEmail(company_id=company.id, contact_id=contact.id if contact else None, email=email.lower(),
                       source=source, pattern=pattern, vouched=vouched, checks={"found": detail} if detail else {})
    session.add(row)
    session.flush()
    return row


def find_address(session: Session, http: PoliteClient, contact: Contact, company: Company, *,
                 allow_enrich: bool, startup: bool, pool=None, resolver=lookup_mx, smtp=smtp_check) -> ContactEmail | None:
    """Best address for one person, walking the ladder only as far as needed."""
    rules = get_config().outreach.contacts
    domain = mail_domain_for(company)
    kw = dict(pool=pool, resolver=resolver, smtp=smtp)
    stale = datetime.now(timezone.utc) - timedelta(days=rules.reverify_after_days)

    # 1 — published / already known.
    current = best_address(session, contact)
    if current and current.confidence in ("proven", "verified") and current.verified_at and current.verified_at > stale:
        return current
    if current and current.confidence in ("unchecked", "verified", "low") and current.source in TRUSTED_SOURCES:
        verify_candidates(session, http, [current], **kw)
        if current.confidence != "invalid":
            return current

    md = mail_domain(session, domain, resolver)
    if md.has_mx is False:
        return None

    # 2 — enrichment (pooled free tiers; cached forever).
    if allow_enrich:
        found = enrich.lookup_person(session, http, domain, contact.first_name, contact.last_name)
        if found and not _taken(session, company.id, found.email):
            row = _add(session, company, contact, found.email, found.provider, vouched=found.vouched, detail=found.detail)
            verify_candidates(session, http, [row], **kw)
            if row.confidence != "invalid":
                learn_pattern(session, company, md)
                return row

    # 3 — construction from the learned pattern, else the common formats.
    pattern = learn_pattern(session, company, md)
    if not pattern and allow_enrich and (hint := enrich.hunter_pattern(session, http, domain)):
        if hint.get("pattern"):
            md.pattern, md.pattern_source, pattern = hint["pattern"], "hunter domain search", hint["pattern"]
        if hint.get("accept_all"):
            md.catch_all = True
    formats = [pattern] if pattern else (rules.startup_formats if startup else rules.common_formats)
    rows: list[ContactEmail] = []
    for fmt in formats[: rules.verification.max_candidates_per_person]:
        try:
            email = f"{render(fmt, contact.first_name, contact.last_name)}@{domain}"
        except (KeyError, IndexError, ValueError):
            continue
        existing = _taken(session, company.id, email)
        if existing is not None:
            if existing.contact_id == contact.id and existing.confidence != "invalid":
                rows.append(existing)
            continue  # someone else's address (or already ruled out)
        rows.append(_add(session, company, contact, email, "constructed", pattern=fmt))
    if not rows:
        return None
    chosen = verify_candidates(session, http, rows, **kw)
    if chosen is not None and not pattern:
        md.pattern, md.pattern_source = chosen.pattern, chosen.email  # one confirmed guess teaches the domain
    if chosen is None:
        chosen = next((r for r in rows if r.confidence == "low"), None)
    # Keep the one we'd use; drop the other guesses (invalid ones stay so they're never retried).
    for r in rows:
        if r is not chosen and r.confidence not in ("invalid",) and r.source == "constructed":
            session.delete(r)
    session.flush()
    return chosen
