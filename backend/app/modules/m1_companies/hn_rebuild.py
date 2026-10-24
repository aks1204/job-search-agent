"""One-time rebuild of everything built from Hacker News posts (owner decision, 2026-10-24).

Every saved post is fetched again and read by the new post reader; then:
- a post that belongs to another company than the one it was filed under moves there (or becomes its own
  company — name-only when the post has no website). The s&t post moves off zs.com this way.
- a company left with nothing but that misread post is dropped (rejected, never revived by itself): its
  open roles are closed and its unsent emails cancelled. One blocked as your employer stays blocked, and
  gets its real name back.
- companies known only from Hacker News get their name from the post and are judged again with the
  corrected facts — except ones you decided on yourself. Companies also known from elsewhere (your seed
  list, YC…) only have their HN part fixed.
- roles from posts count as real postings from now on (AI-graded, no lead cap).
Each post is marked as rebuilt, so running it again only touches posts it couldn't read the first time.
"""

import logging
from collections import Counter
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core import audit
from app.core.notify import notify
from app.models.company import (
    STATUS_BLOCKED, STATUS_CANDIDATE, STATUS_REJECTED, STATUS_UNRESOLVED, Company, CompanySighting, ContactLead,
)
from app.models.feed import RawPosting
from app.models.job import JobPosting
from app.models.verdict import CompanyVerdict
from app.modules.m1_companies.facts import feeder_facts
from app.modules.m1_companies.identity import assign_domain, normalize_name
from app.modules.m1_companies.scouts.hn import fetch_item, read_by_ai, read_by_rules, to_candidate

log = logging.getLogger(__name__)

VERSION = 1      # stamped on each rebuilt post's data
HIRING_FACTS = ("india_presence", "hires_remote_from_india", "sponsors_visa")


def _hiring_facts(source_data: dict) -> set:
    """What the company judgment takes from the post: where they hire, remote from India, visas."""
    return {(k, v) for k, v, *_ in feeder_facts(source_data) if k in HIRING_FACTS}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _sightings(session: Session, company_id: int) -> list[CompanySighting]:
    return session.scalars(select(CompanySighting).where(CompanySighting.company_id == company_id)).all()


FILLER = {"ai", "hq", "app", "io", "inc", "labs", "lab", "the", "co", "tech", "technologies", "software", "group",
          "global", "india", "systems"}


def _same_name(name: str, company: Company) -> bool:
    """'Acme AI' and 'Acme' are the same company — the extra words are only filler. 'Apple Pie Labs' and 'Apple'
    are not, nor 'Strategy & Transformation Consulting' and 'ZS'."""
    from app.modules.m1_companies.employers import _fit

    wanted = normalize_name(name)
    for n in {company.normalized_name, *(normalize_name(a) for a in company.aliases or [])}:
        if not n:
            continue
        short, long_ = sorted((n, wanted), key=len)
        if n == wanted or (long_.startswith(short + " ") and set(long_[len(short):].split()) <= FILLER):
            return True
    return bool(company.domain) and _fit(name, company.domain) == "full"


def _same_site(a: str | None, b: str | None) -> bool:
    from app.core.hosts import domain_label

    return bool(a and b) and domain_label(a) == domain_label(b)   # acme.com and acme.ai


def _latest_verdict(session: Session, company_id: int) -> CompanyVerdict | None:
    return session.scalars(select(CompanyVerdict).where(CompanyVerdict.company_id == company_id)
                           .order_by(CompanyVerdict.id.desc()).limit(1)).first()


def _new_company(session: Session, name: str, domain: str | None) -> Company:
    if domain is None:
        from app.modules.m1_companies.identity import _websiteless_namesake

        if (same := _websiteless_namesake(session, name)) is not None:
            return same
    c = Company(name=name[:255], normalized_name=normalize_name(name)[:255], domain=domain,
                domain_source="source" if domain else None, website=f"https://{domain}" if domain else None,
                status=STATUS_CANDIDATE, aliases=[], source_data={})
    session.add(c)
    session.flush()
    return c


def _target(session: Session, old: Company, name: str, domain: str | None, s: CompanySighting) -> Company:
    """Which company this post really belongs to."""
    everyone = [x for x in _sightings(session, old.id) if x.id != s.id]
    others = [x for x in everyone if x.scout != "hn"]
    only_post = not any(x.scout == "hn" for x in everyone)
    if domain:
        if old.domain == domain:
            return old
        owner = session.scalar(select(Company).where(Company.domain == domain))
        if owner is not None:
            return owner
        if others and _same_name(name, old) and (_same_site(domain, old.domain)
                                                  or old.domain_source in ("seed", "manual")):
            return old                      # e.g. seeded as acme.com, the post says acme.ai: same company
        if old.domain is None and ((only_post and not others) or _same_name(name, old)):
            return assign_domain(session, old, domain, source="source")   # the post now gives its website
        return _new_company(session, name, domain)
    # The post has no website.
    if old.domain is None:
        if only_post and not others or _same_name(name, old):
            return old
        return _new_company(session, name, None)   # an old reader merged different companies' posts here
    if (others and _same_name(name, old)) or (_same_name(name, old) and _website_fits(name, old.domain)):
        return old        # other sources vouch for it, or its website plainly is theirs (mastra.ai ↔ Mastra)
    return _new_company(session, name, None)       # its website came from a misread link (perfectwiki.page)


def _website_fits(name: str, domain: str | None) -> bool:
    from app.modules.m1_companies.scouts.hn import _fits

    return bool(domain) and _fits(name, domain)


def _upsert_contacts(session: Session, company: Company, cand) -> None:
    for hint in cand.contacts:
        email = hint.email.strip().lower()
        lead = session.scalar(select(ContactLead).where(ContactLead.company_id == company.id,
                                                        ContactLead.email == email))
        if lead is None:
            session.add(ContactLead(company_id=company.id, email=email, name=hint.name, role=hint.role,
                                    source="hn", context=hint.context))
        else:
            lead.name, lead.role, lead.context = hint.name or lead.name, hint.role or lead.role, hint.context


def _move_contacts(session: Session, old: Company, new: Company, emails: list[str]) -> None:
    """The post's addresses go with it — and the old company stops using them right away."""
    from app.models.contact import RoleChannel
    from app.models.outreach import OutreachCampaign, OutreachEmail

    emails = [e.lower() for e in emails]
    if emails:
        for ch in session.scalars(select(RoleChannel).where(RoleChannel.company_id == old.id)).all():
            if ch.designated_email in emails:
                ch.mode, ch.designated_email, ch.also_emails, ch.next_retry_at = "none", None, [], _now()
                ch.note = "the address belonged to another company's post"   # looked for again
            elif set(ch.also_emails or []) & set(emails):
                ch.also_emails = [a for a in ch.also_emails if a not in emails]
        for e in session.scalars(select(OutreachEmail).join(OutreachCampaign,
                                                            OutreachCampaign.id == OutreachEmail.campaign_id)
                                 .where(OutreachCampaign.company_id == old.id, OutreachEmail.status == "queued",
                                        OutreachEmail.to_email.in_(emails))).all():
            e.status, e.status_reason = "cancelled", "the address belonged to another company's post"
    for lead in session.scalars(select(ContactLead).where(ContactLead.company_id == old.id, ContactLead.source == "hn",
                                                          ContactLead.email.in_(emails or [""]))).all():
        if session.scalar(select(ContactLead.id).where(ContactLead.company_id == new.id,
                                                       ContactLead.email == lead.email)):
            session.delete(lead)
        else:
            lead.company_id = new.id


def _close_all_roles(session: Session, company_id: int) -> None:
    now = _now()
    for r in session.scalars(select(RawPosting).where(RawPosting.company_id == company_id,
                                                      RawPosting.closed_at.is_(None))).all():
        r.closed_at = now
    for j in session.scalars(select(JobPosting).where(JobPosting.company_id == company_id,
                                                      JobPosting.status == "open")).all():
        j.status, j.closed_at = "closed", now          # right away, not at the next job-intake run


def _retire(session: Session, company: Company, why: str) -> str:
    """A company whose only source was a post that turned out to be someone else's."""
    from app.modules.m3_outreach.campaigns import stop_company

    _close_all_roles(session, company.id)
    stop_company(session, company.id, f"dropped: {why}")
    if company.status == STATUS_BLOCKED:
        latest = _latest_verdict(session, company.id)
        key = ((latest.checks or [{}])[0].get("employer") if latest is not None else None)
        if key:   # blocked as your employer: it keeps the block and gets your profile's name for it
            from app.modules.m1_companies.employers import employers

            if e := next((x for x in employers(session) if x.normalized == key), None):
                if company.name != e.name:
                    company.aliases = [*(company.aliases or []), company.name]
                    company.name, company.normalized_name = e.name, normalize_name(e.name)
        return "kept blocked"
    session.add(CompanyVerdict(
        company_id=company.id, passed=False, profile=company.type if company.type != "unknown" else "startup",
        failed_check="misattributed", summary=f"Dropped: {why}",
        checks=[{"name": "misattributed", "outcome": "fail", "reason": why, "on_unknown": False, "permanent": False}],
        judgment={}, permanent=False, trigger="hn_rebuild", decided_by="machine", rules_versions={}, precedent_ids=[],
    ))
    company.status, company.reeval_reason = STATUS_REJECTED, None
    audit.record(session, "m1", "company_dropped", f"Dropped {company.name} ({company.domain or 'no website'}): {why}",
                 subject_type="company", subject_id=str(company.id))
    return "dropped"


def _yours(session: Session, company: Company) -> bool:
    """You decided on this company yourself (override, or kept your employer anyway): never re-judged."""
    latest = _latest_verdict(session, company.id)
    return latest is not None and latest.decided_by == "owner"


def _one(session: Session, s: CompanySighting, http, llm, out: Counter, changes: list[str],
         emptied: dict[int, str], rejudge: set[int]) -> None:
    from app.core.llm import LLMError

    d = s.data or {}
    kid, thread = d.get("comment_id"), d.get("thread_id")
    if d.get("rebuilt") == VERSION:
        out["already_rebuilt"] += 1
        return
    if not kid:
        out["no_post_id"] += 1
        return
    try:
        item = fetch_item(http, int(kid))
    except Exception:
        out["post_unreachable"] += 1
        return
    if not item or item.get("deleted") or item.get("dead") or not item.get("text"):
        out["post_gone"] += 1
        return
    try:
        read = read_by_ai(llm, session, item["text"]) if llm is not None else read_by_rules(item["text"])
    except LLMError:
        out["ai_unavailable"] += 1
        return
    if read is None:
        out["not_a_hiring_post"] += 1
        s.data = {**d, "rebuilt": VERSION}
        return
    cand = to_candidate(read, thread_id=int(thread or 0), kid=int(kid), posted=item.get("time"))
    data = {**cand.data, "rebuilt": VERSION}
    old = session.get(Company, s.company_id)
    target = _target(session, old, cand.name, cand.domain, s)
    # Judged again only if what the judgment uses changed: the website, or where they hire / visas.
    changed = (d.get("website", old.domain) != data.get("website")
               or _hiring_facts({"hn": d}) != _hiring_facts({"hn": data}))
    if target.id != old.id:
        s.company_id = target.id
        _move_contacts(session, old, target, [c.email for c in cand.contacts])
        session.flush()
        if not any(x.scout == "hn" for x in _sightings(session, old.id)):   # no stale post facts left behind
            from app.models.verdict import CompanyFact

            old.source_data = {k: v for k, v in (old.source_data or {}).items() if k != "hn"}
            for f in session.scalars(select(CompanyFact).where(CompanyFact.company_id == old.id,
                                                               CompanyFact.source == "hn")).all():
                session.delete(f)
            latest = _latest_verdict(session, old.id)
            if _sightings(session, old.id) and old.status not in (STATUS_BLOCKED, STATUS_UNRESOLVED) \
                    and (latest is None or latest.decided_by == "machine"):
                old.status, old.reeval_reason = STATUS_CANDIDATE, "rules_change"   # judged without that post
                out["to_judge_again"] += 1
        emptied[old.id] = f"its Hacker News post belongs to {cand.name} ({cand.domain or 'no website'})"
        changes.append(f"moved: the post filed under {old.name} ({old.domain or 'no website'}) "
                       f"→ {cand.name} ({cand.domain or 'no website'})")
        out["moved"] += 1
        changed = True
    s.name_seen, s.domain_seen, s.data = cand.name[:255], cand.domain, data
    target.source_data = {**(target.source_data or {}), "hn": data}
    _upsert_contacts(session, target, cand)
    hn_only = all(x.scout == "hn" for x in _sightings(session, target.id))
    if hn_only and target.status != STATUS_BLOCKED and target.name != cand.name:
        changes.append(f"renamed: {target.name} → {cand.name}")
        if normalize_name(target.name) != normalize_name(cand.name):    # not just tidier capitals
            target.aliases = [*(target.aliases or []), target.name]
        target.name, target.normalized_name = cand.name[:255], normalize_name(cand.name)[:255]
        out["renamed"] += 1
        changed = True
    if (changed or target.status == STATUS_UNRESOLVED) and hn_only:
        rejudge.add(target.id)


def repair(session: Session) -> dict:
    """Undo the first rebuild's mistake (2026-10-25): a company whose post has no link was split off its
    website and dropped even when that website plainly is theirs (Mastra and mastra.ai). Such companies get
    their post, contacts and jobs back and are judged again; the website-less copy goes. Companies whose
    website doesn't fit their name (GiveDirectly and perfectwiki.page) stay dropped."""
    out: Counter = Counter()
    restored: list[str] = []
    dropped = session.scalars(select(Company).where(Company.status == STATUS_REJECTED,
                                                    Company.domain.is_not(None))).all()
    copies = session.scalars(select(Company).where(Company.domain.is_(None))).all()
    for c in dropped:
        latest = _latest_verdict(session, c.id)
        if latest is None or latest.failed_check != "misattributed" or not _website_fits(c.name, c.domain):
            continue
        twins = [d for d in copies if d.id != c.id and _same_name(d.name, c)
                 and all(x.scout == "hn" for x in _sightings(session, d.id))]
        if not twins:
            continue
        for d in twins:
            for s in _sightings(session, d.id):
                s.company_id = c.id
            for lead in session.scalars(select(ContactLead).where(ContactLead.company_id == d.id)).all():
                if session.scalar(select(ContactLead.id).where(ContactLead.company_id == c.id,
                                                               ContactLead.email == lead.email)):
                    session.delete(lead)
                else:
                    lead.company_id = c.id
            if hn := (d.source_data or {}).get("hn"):
                c.source_data = {**(c.source_data or {}), "hn": hn}
            session.flush()
            session.delete(d)                        # the website-less copy (it has nothing else)
            copies.remove(d)
            out["copies_removed"] += 1
        c.status, c.reeval_reason = STATUS_CANDIDATE, "rules_change"   # judged again; its feed resumes if kept
        audit.record(session, "m1", "company_restored",
                     f"Restored {c.name} ({c.domain}): its website fits its name, so the post is theirs",
                     subject_type="company", subject_id=str(c.id))
        restored.append(f"{c.name} ({c.domain})")
        out["restored"] += 1
    session.flush()
    if restored:
        notify(session, "update", "happened", f"Hacker News repair: {len(restored)} companies restored",
               detail=", ".join(restored)[:2000])
    return {"counts": dict(out), "restored": restored}


def rebuild(session: Session, http, llm) -> dict:
    out: Counter = Counter()
    changes: list[str] = []
    emptied: dict[int, str] = {}
    rejudge: set[int] = set()
    for s in session.scalars(select(CompanySighting).where(CompanySighting.scout == "hn")
                             .order_by(CompanySighting.id)).all():
        try:
            with session.begin_nested():   # one post going wrong never undoes the others
                _one(session, s, http, llm, out, changes, emptied, rejudge)
        except Exception as e:
            log.warning("hn rebuild: sighting %s failed: %r", s.id, e)
            out["errors"] += 1

    for company_id, why in emptied.items():
        company = session.get(Company, company_id)
        if company is None or _sightings(session, company_id):
            continue
        result = _retire(session, company, why)
        changes.append(f"{result}: {company.name} ({company.domain or 'no website'}) — {why}")
        out["dropped" if result == "dropped" else "kept_blocked"] += 1

    for company_id in rejudge:
        company = session.get(Company, company_id)
        if company is None or company.status in (STATUS_BLOCKED, STATUS_REJECTED) and company_id in emptied:
            continue
        if company.status == STATUS_BLOCKED or _yours(session, company):
            out["kept_your_decision"] += 1
            continue
        company.status, company.reeval_reason = STATUS_CANDIDATE, "rules_change"
        out["to_judge_again"] += 1

    # Roles from posts are real postings now: graded like any other, no lead cap.
    from app.models.assessment import JobAssessment

    hn_jobs = select(JobPosting.id).where(JobPosting.external_id.like("hn:%"))
    session.execute(update(JobAssessment).where(JobAssessment.job_id.in_(hn_jobs)).values(lead_only=False))
    session.execute(update(RawPosting).where(RawPosting.external_id.like("hn:%")).values(lead_only=False))
    session.execute(update(JobPosting).where(JobPosting.external_id.like("hn:%")).values(lead_only=False))
    session.flush()

    summary = (f"Hacker News rebuild: {out['moved']} post(s) moved to the right company, {out['dropped']} wrongly "
               f"built compan{'y' if out['dropped'] == 1 else 'ies'} dropped, {out['renamed']} renamed, "
               f"{out['to_judge_again']} to be judged again")
    audit.record(session, "m1", "hn_rebuild", summary, detail={"counts": dict(out), "changes": changes[:200]})
    if out["moved"] or out["dropped"] or out["renamed"]:
        notify(session, "update", "happened", summary, detail="; ".join(changes[:10])[:2000])
    return {"counts": dict(out), "changes": changes, "summary": summary}
