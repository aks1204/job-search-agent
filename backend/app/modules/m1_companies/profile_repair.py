"""One-time repair (2026-10-26): companies glued onto a profile site's record.

The website-finder took search results like "Innerscore — Overview & Contacts | ZoomInfo" as Innerscore's
own website (zoominfo.com); every further such company was then merged into the same record because they
"shared" a website — 11 unrelated companies inside one record. Now a profile site only counts when the
company's whole name is that site (hosts.is_profile_site_for_other). This splits the glued ones back out:
each gets its own record with only its own listing's details, without a website, and the website-finder
looks for its real one again. If the profile site's own company genuinely has a source there (e.g. ZoomInfo
from a curated pack), that record starts over under its own name; otherwise the glued record is removed —
with what was built from the mix (job reading, roles, drafts) and the addresses that came with the glued
listings, which can't be told apart. Previewed first.
"""

from collections import Counter, defaultdict
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core import audit
from app.core.hosts import PROFILE_SITES, is_profile_site_for_other
from app.models.company import STATUS_CANDIDATE, STATUS_UNRESOLVED, Company, CompanySighting, ContactLead
from app.models.draft import OutreachDraft
from app.models.feed import RawPosting
from app.models.job import JobCluster
from app.modules.m1_companies.identity import _websiteless_namesake, normalize_name


@dataclass
class Split:
    company_id: int
    name: str
    domain: str
    out: dict[str, list[int]] = field(default_factory=dict)   # name seen → sightings that leave
    own_name: str | None = None                                # the site's own company, if it has a source there
    open_roles: int = 0
    drafts: int = 0
    addresses: int = 0


def plan(session: Session) -> list[Split]:
    plans = []
    for c in session.scalars(select(Company).where(Company.domain.in_(PROFILE_SITES))).all():
        groups: dict[str, list[int]] = defaultdict(list)
        own: Counter = Counter()
        for s in session.scalars(select(CompanySighting).where(CompanySighting.company_id == c.id)).all():
            if is_profile_site_for_other(c.domain, s.name_seen):
                groups[s.name_seen.strip()].append(s.id)
            else:
                own[s.name_seen.strip()] += 1
        if not groups:
            continue
        p = Split(c.id, c.name, c.domain, dict(groups), own.most_common(1)[0][0] if own else None)
        p.open_roles = session.scalar(select(func.count()).select_from(RawPosting).where(
            RawPosting.company_id == c.id, RawPosting.closed_at.is_(None))) or 0
        p.drafts = session.scalar(select(func.count()).select_from(OutreachDraft).join(
            JobCluster, JobCluster.id == OutreachDraft.cluster_id).where(JobCluster.company_id == c.id)) or 0
        p.addresses = session.scalar(select(func.count()).select_from(ContactLead).where(
            ContactLead.company_id == c.id)) or 0
        plans.append(p)
    return plans


def describe(plans: list[Split]) -> list[str]:
    if not plans:
        return ["Nothing to repair: no company is glued onto a profile site."]
    lines = []
    for p in plans:
        lines.append(f"{p.name} ({p.domain}) holds {len(p.out)} other compan{'y' if len(p.out) == 1 else 'ies'}:")
        lines += [f"  · {n} → its own record, with only its own listing; website looked up again" for n in sorted(p.out)]
        lines.append(f"  removed (built from the mix): its job reading, {p.open_roles} open role(s), {p.drafts} "
                     f"draft(s), {p.addresses} address(es) from the glued listings")
        lines.append(f"  {p.domain}: " + (f"starts over as {p.own_name} — its own source is there"
                                          if p.own_name else "nothing of its own — the record is removed"))
    return lines


def _target(session: Session, name: str) -> Company:
    """An existing website-less record of this name that's still waiting for its website — else a new one."""
    if normalize_name(name):
        existing = _websiteless_namesake(session, name)
        if existing is not None and existing.status in (STATUS_UNRESOLVED, STATUS_CANDIDATE):
            return existing
    c = Company(name=name[:255], normalized_name=normalize_name(name)[:255], status=STATUS_UNRESOLVED,
                aliases=[], source_data={})
    session.add(c)
    session.flush()
    return c


def apply(session: Session, plans: list[Split]) -> dict:
    from app.modules.m1_companies.fix_website import start_over

    split, removed = 0, 0
    for p in plans:
        c = session.get(Company, p.company_id)
        for name, ids in p.out.items():
            target = _target(session, name)
            for s in session.scalars(select(CompanySighting).where(CompanySighting.id.in_(ids))
                                     .order_by(CompanySighting.last_seen_at)).all():
                s.company_id, s.domain_seen = target.id, None
                # only its OWN listing's details (the glued record kept one per source: someone else's)
                target.source_data = {**(target.source_data or {}), s.scout: s.data or {}}
            if target.status == STATUS_UNRESOLVED:
                target.resolution_attempts, target.next_resolution_at, target.llm_resolution_tried_at = 0, None, None
            split += 1
        session.flush()
        if p.own_name is None:
            session.delete(c)                       # only the glue: it goes, with what was built from the mix
            removed += 1
        else:                                       # the site's own company starts over under its own name
            session.execute(ContactLead.__table__.delete().where(ContactLead.company_id == c.id))
            start_over(session, c)
            c.name, c.normalized_name = p.own_name[:255], normalize_name(p.own_name)[:255]
            c.source_data = {s.scout: s.data or {} for s in session.scalars(
                select(CompanySighting).where(CompanySighting.company_id == c.id)
                .order_by(CompanySighting.last_seen_at)).all()}
        session.flush()
    audit.record(session, "m1", "profile_site_repair",
                 f"Split {split} compan{'y' if split == 1 else 'ies'} off profile-site records; {removed} removed",
                 detail={"plans": [{"company": p.name, "domain": p.domain, "split": sorted(p.out),
                                    "own": p.own_name} for p in plans]})
    return {"split": split, "removed": removed}
