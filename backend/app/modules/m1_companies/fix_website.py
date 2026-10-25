"""Correcting a company's website or name (owner decision, 2026-10-26): the company starts over.

Kept: only what its sources explicitly said — its posts and listings (Hacker News, job sites…), with the
roles they name and the email addresses they give.
Everything else is removed and redone with the corrected details by the normal pipeline: company research
and judgment, careers page and open roles, email addresses found by searching, outreach drafts — and any
decisions or outreach on it (you'd only act on a company once its details are right).

A source that explicitly named the OLD website was about that company, so it stays with it as its own record
(your seed list's "ZS, zs.com" stays ZS when the s&t post moves off zs.com).

Always previewed first; carried out only when you confirm (one mistyped website would otherwise reset the
wrong company).
"""

from collections import Counter
from dataclasses import dataclass, field

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.core import audit
from app.core.hosts import is_junk, normalize_domain
from app.models.company import STATUS_CANDIDATE, STATUS_UNRESOLVED, Company, CompanySighting, ContactLead
from app.models.contact import Bench, Contact, ContactEmail
from app.models.draft import OutreachDraft
from app.models.feed import CompanyFeed, RawPosting
from app.models.job import JobCluster
from app.models.outreach import OutreachCampaign
from app.models.verdict import CompanyFact, CompanyVerdict
from app.modules.m1_companies.identity import normalize_name
from app.modules.m1_companies.watch.sync import LEAD_PREFIXES   # roles from posts / listings


@dataclass
class Correction:
    company_id: int
    name: str
    domain: str | None
    new_name: str
    new_domain: str | None
    kept_roles: list[str] = field(default_factory=list)
    kept_addresses: list[str] = field(default_factory=list)
    site_roles: int = 0
    found_addresses: int = 0
    drafts: int = 0
    other_id: int | None = None
    other_desc: str | None = None
    stay_ids: list[int] = field(default_factory=list)      # sources that named the OLD website: stay with it
    stay_name: str | None = None
    stay_desc: str | None = None

    def describe(self) -> list[str]:
        if self.new_domain != self.domain:
            head = f"{self.name}: website {self.domain or '(none)'} → {self.new_domain}"
        else:
            head = f"{self.name} → {self.new_name} (website {self.domain or '(none)'} stays)"
        what = "the new website" if self.new_domain != self.domain else "the new name"
        lines = [head, "  kept (from its posts and listings):"]
        lines += [f"    · role: {r}" for r in self.kept_roles[:10]] or ["    · (no roles named)"]
        lines += [f"    · address: {a}" for a in self.kept_addresses[:10]]
        if self.other_desc:
            lines.append(f"  {self.new_domain} already has a record ({self.other_desc}): its posts and listings "
                         "join this one, and it starts over too")
        if self.stay_desc:
            lines.append(f"  {self.stay_desc}")
        lines += [f"  redone with {what}:",
                  "    · company research and judgment",
                  f"    · careers page and open roles      ({self.site_roles} role(s) from the careers page removed)",
                  f"    · email addresses                   ({self.found_addresses} found by searching removed)",
                  f"    · outreach drafts                   ({self.drafts} draft(s) removed)"]
        return lines


def _roles_named(session: Session, company_id: int, skip: set[int] = frozenset()) -> list[str]:
    out = []
    for s in session.scalars(select(CompanySighting).where(CompanySighting.company_id == company_id)).all():
        if s.id in skip:
            continue
        d = s.data or {}
        if s.scout == "hn":
            for r in d.get("roles") or []:
                out.append(f"{r.get('title')}" + (f" ({r['location']})" if r.get("location") else ""))
            if not d.get("roles") and "|" in (d.get("headline") or ""):
                out.append(d["headline"].split("|")[1].strip())
        elif d.get("title"):
            out.append(f"{d['title']}" + (f" ({d['location']})" if d.get("location") else "") + f" — {s.scout}")
    return list(dict.fromkeys(o for o in out if o))


def _sightings(session: Session, company_id: int) -> list[CompanySighting]:
    return session.scalars(select(CompanySighting).where(CompanySighting.company_id == company_id)).all()


def _leads_staying(session: Session, c: Company, stay_scouts: set[str]) -> list[ContactLead]:
    """Addresses at the old website that came from a source staying with it."""
    if not stay_scouts:
        return []
    return [l for l in session.scalars(select(ContactLead).where(ContactLead.company_id == c.id)).all()
            if l.source in stay_scouts and normalize_domain(l.email) == c.domain]


def start_over(session: Session, c: Company) -> None:
    """Remove everything built for the company (roles, judgments, contacts found, outreach — the database removes
    what hangs off them: scores, drafts, campaigns, applications…) and send it through the pipeline afresh,
    first in the judging queue. Its sources (posts, listings) and the addresses they gave stay."""
    session.execute(delete(JobCluster).where(JobCluster.company_id == c.id))
    session.execute(delete(CompanyFeed).where(CompanyFeed.company_id == c.id))
    session.execute(delete(OutreachCampaign).where(OutreachCampaign.company_id == c.id))
    session.execute(delete(Bench).where(Bench.company_id == c.id))
    session.execute(delete(ContactEmail).where(ContactEmail.company_id == c.id))
    session.execute(delete(Contact).where(Contact.company_id == c.id))
    session.execute(delete(CompanyFact).where(CompanyFact.company_id == c.id))
    session.execute(delete(CompanyVerdict).where(CompanyVerdict.company_id == c.id))
    c.aliases = []
    c.type, c.type_source, c.enriched_at = "unknown", None, None
    c.status, c.reeval_reason = STATUS_CANDIDATE, "rules_change"


def plan(session: Session, c: Company, *, new_domain: str | None = None, new_name: str | None = None) -> Correction:
    domain = c.domain
    if new_domain is not None:
        domain = normalize_domain(new_domain)
        if not domain:
            raise ValueError(f"“{new_domain}” isn't a website — e.g. strategy-transformation.com")
        if is_junk(domain):
            raise ValueError(f"{domain} is a job, social or email site — not a company's own website")
        if domain == c.domain:
            raise ValueError(f"{c.name} is already at {domain}")
    name = c.name
    if new_name is not None:
        name = new_name.strip()
        if not name:
            raise ValueError("a company needs a name")
        if name == c.name:
            raise ValueError(f"{c.name} already has that name")
    p = Correction(c.id, c.name, c.domain, name[:255], domain)
    sightings = _sightings(session, c.id)
    if c.domain and domain != c.domain:
        stay = [s for s in sightings if s.domain_seen == c.domain]
        if stay and len(stay) < len(sightings):   # if every source named it, your correction is what counts
            from app.modules.maintenance.sources import LABELS

            p.stay_ids = [s.id for s in stay]
            p.stay_name = Counter(s.name_seen for s in stay).most_common(1)[0][0]
            who = ", ".join(sorted({LABELS.get(s.scout, s.scout).lower() for s in stay}))
            p.stay_desc = (f"{c.domain} stays as its own company, “{p.stay_name}”: {who} named it with that "
                           "website (it goes through the pipeline as itself)")
    staying = set(p.stay_ids)
    stay_scouts = {s.scout for s in sightings if s.id in staying}
    leaving = {l.email for l in _leads_staying(session, c, stay_scouts)}
    p.kept_roles = _roles_named(session, c.id, staying)
    p.kept_addresses = sorted(set(session.scalars(select(ContactLead.email).where(ContactLead.company_id == c.id)))
                              - leaving)
    p.site_roles = sum(1 for (ext,) in session.execute(select(RawPosting.external_id).where(
        RawPosting.company_id == c.id, RawPosting.closed_at.is_(None))).all() if not ext.startswith(LEAD_PREFIXES))
    p.found_addresses = session.scalar(select(func.count()).select_from(ContactEmail).where(
        ContactEmail.company_id == c.id, ContactEmail.email.not_in(p.kept_addresses or [""]))) or 0
    p.drafts = session.scalar(select(func.count()).select_from(OutreachDraft).join(
        JobCluster, JobCluster.id == OutreachDraft.cluster_id).where(JobCluster.company_id == c.id)) or 0
    if domain and domain != c.domain and (other := session.scalar(select(Company).where(Company.domain == domain))):
        p.other_id, p.other_desc = other.id, f"{other.name}, {other.status}"
        p.kept_roles += [r for r in _roles_named(session, other.id) if r not in p.kept_roles]
        p.kept_addresses = sorted(set(p.kept_addresses) | set(session.scalars(
            select(ContactLead.email).where(ContactLead.company_id == other.id))))
    return p


def apply(session: Session, p: Correction) -> Company:
    """Start the company over with the corrected details."""
    c = session.get(Company, p.company_id)
    if p.other_id:                                     # one company, two records (an earlier mistake): one now
        other = session.get(Company, p.other_id)
        for s in _sightings(session, other.id):
            s.company_id = c.id
        for lead in session.scalars(select(ContactLead).where(ContactLead.company_id == other.id)).all():
            if session.scalar(select(ContactLead.id).where(ContactLead.company_id == c.id,
                                                           ContactLead.email == lead.email)):
                session.delete(lead)
            else:
                lead.company_id = c.id
        c.source_data = {**(other.source_data or {}), **(c.source_data or {})}
        session.flush()
        session.delete(other)
        session.flush()
    old_domain, old_data = c.domain, dict(c.source_data or {})
    old_domain_source, old_type, old_type_source = c.domain_source, c.type, c.type_source
    stay = [s for s in _sightings(session, c.id) if s.id in set(p.stay_ids)]
    stay_scouts = {s.scout for s in stay}
    stay_leads = _leads_staying(session, c, stay_scouts)
    moving_scouts = {s.scout for s in _sightings(session, c.id) if s.id not in set(p.stay_ids)}
    start_over(session, c)
    # The corrected details.
    c.name, c.normalized_name = p.new_name, normalize_name(p.new_name)[:255]
    if p.new_domain != p.domain:
        c.domain, c.website, c.domain_source = p.new_domain, f"https://{p.new_domain}", "manual"
    c.source_data = {k: v for k, v in old_data.items()
                     if k != "owner" and not (k in stay_scouts and k not in moving_scouts)}
    if c.domain is None and "hn" not in moving_scouts:
        # No website yet and no post to judge it from: back to the website-finder, which searches the new name.
        c.status, c.reeval_reason = STATUS_UNRESOLVED, None
        c.resolution_attempts, c.next_resolution_at, c.llm_resolution_tried_at = 0, None, None
    session.flush()
    if stay:                                            # the old website's own company, from its own sources
        # It keeps what its own sources gave it: "website from your seed list" (protects it from clean-ups) and
        # its type ("established" from your seed list) — when those came from a source that stays with it.
        domain_source = "seed" if "seeds" in stay_scouts else (
            old_domain_source if old_domain_source in ("seed", "source") else "source")
        keep_type = old_type_source in stay_scouts
        left = Company(name=p.stay_name[:255], normalized_name=normalize_name(p.stay_name)[:255], domain=old_domain,
                       domain_source=domain_source, website=f"https://{old_domain}", status=STATUS_CANDIDATE,
                       reeval_reason="rules_change", aliases=[],
                       type=old_type if keep_type else "unknown", type_source=old_type_source if keep_type else None,
                       source_data={k: v for k, v in old_data.items() if k in stay_scouts and k not in moving_scouts})
        session.add(left)
        session.flush()
        for s in stay:
            s.company_id = left.id
        for lead in stay_leads:
            lead.company_id = left.id
        session.flush()
    audit.record(session, "m1", "company_corrected",
                 f"You corrected {p.name}" + (f": website {p.domain or '(none)'} → {p.new_domain}"
                                              if p.new_domain != p.domain else f" → {p.new_name}")
                 + " — starting over with the corrected details"
                 + (f"; {old_domain} stays as {p.stay_name}" if stay else ""),
                 subject_type="company", subject_id=str(c.id),
                 detail={"from": {"name": p.name, "domain": p.domain},
                         "to": {"name": p.new_name, "domain": p.new_domain}, "combined_with": p.other_id,
                         "left_behind": p.stay_ids})
    return c
