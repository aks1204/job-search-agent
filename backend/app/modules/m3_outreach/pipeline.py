"""Part 3a pipeline: for each pursued role, decide its channel and make sure the people are ready.

Pursued = roles in the configured auto bands (fast lane by default) plus roles you marked `act`.
Channel, in order:
  designated — the posting names an address for applications: ONLY that address, no bench, ever
  bench      — people at the company (startup founders / recruiters / hiring managers), with addresses
  mailbox    — no people, but the company publishes a hiring mailbox (jobs@, careers@…)
  none       — nothing found: the role goes on to the apply list anyway; we look again in 2 weeks
Nothing here sends email.
"""

import logging
from collections import Counter
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.config_loader import get_config
from app.core import audit
from app.core.http import PoliteClient
from app.core.llm import LLM
from app.db import session_scope
from app.models.assessment import JobAssessment
from app.models.company import STATUS_BLOCKED, STATUS_REJECTED, Company, ContactLead
from app.models.contact import Bench, BenchMember, Contact, ContactEmail, RoleChannel
from app.models.job import JobPosting
from app.models.score import JobScore
from app.modules.m3_outreach import bench as B
from app.modules.m3_outreach.addresses import best_address, find_address, mail_domain_for
from app.modules.m1_companies.candidate import APPLY_MARK
from app.modules.m3_outreach.designated import _OTHER_LOCALS, find_designated
from app.modules.m3_outreach.names import is_role_address, name_key, split_name
from app.modules.m3_outreach.people import person_still_there
from app.modules.m3_outreach.verify import verify_candidates

log = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --- store first: fold in addresses Module 1 found in passing ----------------------------------

def ingest_leads(session: Session) -> int:
    """contact_leads (HN posts, postings) → contact_emails; named ones also become contacts."""
    n = 0
    known = {(c, e) for c, e in session.execute(select(ContactEmail.company_id, ContactEmail.email)).all()}
    for lead in session.scalars(select(ContactLead)).all():
        email = lead.email.lower()
        if (lead.company_id, email) in known:
            continue
        contact = None
        if lead.name and (parts := split_name(lead.name)) and not is_role_address(email):
            contact = Contact(company_id=lead.company_id, full_name=lead.name, name_key=name_key(lead.name),
                              first_name=parts[0], last_name=parts[1], title=lead.role, kind="other",
                              source=lead.source[:16], snippet=lead.context)
            session.add(contact)
            session.flush()
        session.add(ContactEmail(company_id=lead.company_id, contact_id=contact.id if contact else None,
                                 email=email, source="published", checks={"lead": lead.source}))
        known.add((lead.company_id, email))
        n += 1
    session.flush()
    return n


# --- which roles ----------------------------------------------------------------------------------

def pursued_roles(session: Session, cluster_ids: list[int] | None = None):
    rules = get_config().outreach.contacts
    q = (select(JobScore, JobAssessment, JobPosting, Company)
         .join(JobAssessment, JobAssessment.id == JobScore.assessment_id)
         .join(JobPosting, JobPosting.id == JobAssessment.job_id)
         .join(Company, Company.id == JobPosting.company_id)
         .where(JobAssessment.outcome == "passed", JobPosting.status == "open",
                Company.status != STATUS_BLOCKED,            # e.g. your current / a past employer
                # a company no longer kept: only roles you chose yourself ("act")
                or_(Company.status != STATUS_REJECTED, JobScore.owner_action == "act")))
    if cluster_ids:
        q = q.where(JobScore.cluster_id.in_(cluster_ids))
    else:
        q = q.where(or_(JobScore.band.in_(rules.auto_bands), JobScore.owner_action == "act"),
                    or_(JobScore.owner_action.is_(None), JobScore.owner_action.not_in(("skip", "thumbs_down"))))
    return session.execute(q.order_by(JobScore.score.desc())).all()


def _due(channel: RoleChannel | None, now: datetime) -> bool:
    if channel is None:
        return True
    if channel.manual or channel.mode == "designated":
        return False
    return channel.next_retry_at is not None and channel.next_retry_at <= now


def _company_address(email: str, company: Company) -> bool:
    """An address from a post is used for this company only if it's at the company's own mail domain — or a
    webmail address (gmail…), which belongs to no other company. Never another company's address."""
    from app.core.hosts import is_junk, normalize_domain

    domain = normalize_domain(email)
    if domain is None:
        return False
    if is_junk(domain):
        return True                     # gmail.com, outlook.com… (a founder's own inbox)
    return company.domain is not None and domain in (company.domain, mail_domain_for(company))


def _designated_for(session: Session, job: JobPosting, company: Company) -> tuple[str, str] | None:
    if hit := find_designated(job.description):          # the job page itself names an address
        return hit
    leads = [l for l in session.scalars(select(ContactLead).where(ContactLead.company_id == company.id,
                                                                  ContactLead.source == "hn")
                                        .order_by(ContactLead.found_at.desc())).all()
             if not _OTHER_LOCALS.match(l.email.split("@", 1)[0])   # never press@, security@, privacy@…
             and _company_address(l.email, company)]
    # Their HN post said "email X to apply": X is how they asked to be reached — for every role there.
    if apply := next((l for l in leads if (l.context or "").startswith(APPLY_MARK)), None):
        return apply.email.lower(), apply.context[len(APPLY_MARK):] or "the Hacker News post asks to apply here"
    if leads:                                           # otherwise the address their HN post gives
        return leads[0].email.lower(), leads[0].context or "address given in the Hacker News post"
    return None


def _shared_mailbox(session: Session, company: Company) -> ContactEmail | None:
    rows = session.scalars(select(ContactEmail).where(
        ContactEmail.company_id == company.id, ContactEmail.contact_id.is_(None),
        ContactEmail.confidence != "invalid")).all()
    return next((r for r in rows if is_role_address(r.email)), None)


def _ensure_address(session: Session, http: PoliteClient, company: Company, email: str, **kw) -> ContactEmail:
    row = session.scalar(select(ContactEmail).where(ContactEmail.company_id == company.id, ContactEmail.email == email))
    if row is None:
        row = ContactEmail(company_id=company.id, email=email, source="published")
        session.add(row)
        session.flush()
    if row.confidence == "unchecked":
        verify_candidates(session, http, [row], **kw)
    return row


def decide_channel(session: Session, http: PoliteClient, llm, score: JobScore, a: JobAssessment, job: JobPosting,
                   company: Company, budget: B.TickBudget, **kw) -> RoleChannel:
    rules = get_config().outreach.contacts
    now = _now()
    channel = session.get(RoleChannel, score.cluster_id)
    if channel is None:
        channel = RoleChannel(cluster_id=score.cluster_id, company_id=company.id, mode="none")
        session.add(channel)
    elif channel.manual:
        return channel  # your own call stands

    if hit := _designated_for(session, job, company):
        email, context = hit
        _ensure_address(session, http, company, email, **kw)  # checked for 3c's sake; still the only channel
        channel.mode, channel.designated_email, channel.designated_context = "designated", email, context[:1000]
        channel.bench_id, channel.next_retry_at, channel.note = None, None, "the posting names this address"
        audit.record(session, "m3", "channel_designated", f"{company.name}: the posting asks for {email}",
                     subject_type="job_cluster", subject_id=str(score.cluster_id))
        return channel

    if not company.domain:   # name-only (HN post without a website): nobody to look up — apply via the post
        channel.mode, channel.bench_id, channel.next_retry_at = "none", None, None
        channel.note, channel.decided_at = "no website or address in their post — apply through the post", now
        return channel

    shape = B.company_class(session, company)
    function = B.function_of(f"{(a.read or {}).get('role_family', '')} {job.title}")
    geo = "india" if job.is_india else "remote"
    bench = B.get_bench(session, company, B.bench_key(shape, function, geo), shape)
    counts = B.fill_bench(session, http, llm, company, bench, function, geo, budget, **kw)
    ready = B.usable_members(session, bench)
    retry_at = now + timedelta(days=rules.retry_no_contact_days)
    if ready:
        channel.mode, channel.bench_id = "bench", bench.id
        channel.next_retry_at = retry_at if B.open_slots(session, bench) else None  # sweeps top up open slots
        channel.note = f"{len(ready)} people ready on the {bench.key} bench"
    elif mailbox := _shared_mailbox(session, company):
        channel.mode, channel.bench_id, channel.next_retry_at = "mailbox", bench.id, retry_at
        channel.designated_email, channel.note = mailbox.email, "no people found yet; the company's hiring mailbox"
    else:
        channel.mode, channel.bench_id, channel.next_retry_at = "none", bench.id, retry_at
        channel.note = ("no contact found — apply anyway; looking again "
                        f"{retry_at:%d %b}" + ("" if counts["searches"] or budget.searches else " (search budget used up)"))
    channel.decided_at = now
    return channel


def run_contacts(*, cluster_ids: list[int] | None = None, limit: int | None = None, **kw) -> dict:
    rules = get_config().outreach.contacts
    counts: Counter = Counter()
    with session_scope() as s:
        counts["leads_ingested"] = ingest_leads(s)
    llm = LLM() if LLM.available() else None
    budget = B.TickBudget(rules.searches_per_tick if not cluster_ids else max(rules.searches_per_tick, 6))
    with session_scope() as s:
        now = _now()
        todo = [(sc.cluster_id) for sc, *_ in pursued_roles(s, cluster_ids)
                if cluster_ids or _due(s.get(RoleChannel, sc.cluster_id), now)]
    with PoliteClient() as http:
        for cluster_id in todo[: limit or len(todo)]:
            try:
                with session_scope() as s:
                    row = next(iter(pursued_roles(s, [cluster_id])), None)
                    if row is None:
                        continue
                    channel = decide_channel(s, http, llm, *row, budget, **kw)
                    counts[f"channel_{channel.mode}"] += 1
            except Exception as e:
                counts["errors"] += 1
                log.warning("contact discovery failed for cluster %s: %r", cluster_id, e)
            if budget.searches <= 0 and not cluster_ids:
                break
    counts["searches_left_this_tick"] = budget.searches
    return dict(counts)


# --- freshness ------------------------------------------------------------------------------------

def refresh_contact(session: Session, http: PoliteClient, llm, contact: Contact, **kw) -> ContactEmail | None:
    """On-use re-verification (spec: 6 months): is the person still there, does the mailbox still work?
    Called before a contact is used (Part 3c) and by the sweeps."""
    rules = get_config().outreach.contacts
    company = session.get(Company, contact.company_id)
    stale = _now() - timedelta(days=rules.reverify_after_days)
    if contact.linkedin_url and (contact.person_checked_at is None or contact.person_checked_at < stale):
        if person_still_there(session, http, llm, contact, company) is False:
            return None
    addr = best_address(session, contact)
    if addr is None or addr.verified_at is None or addr.verified_at < stale:
        if addr is not None and addr.confidence != "proven":
            addr.confidence = "unchecked"
        addr = find_address(session, http, contact, company, allow_enrich=False,
                            startup=B.company_class(session, company) == "startup", **kw)
    return addr


def sweep(*, searches: int | None = None, **kw) -> dict:
    """Weekly: re-check people on hot benches (live roles) monthly and warm ones quarterly, within a
    small search budget; positive proof of departure retires a contact, ambiguity never does."""
    rules = get_config().outreach.contacts
    left = rules.sweep_searches_per_week if searches is None else searches
    counts: Counter = Counter()
    llm = LLM() if LLM.available() else None
    now = _now()
    with session_scope() as s:
        hot_benches = {bid for (bid,) in s.execute(select(RoleChannel.bench_id).where(
            RoleChannel.mode == "bench", RoleChannel.bench_id.is_not(None))).all()}
        rows = s.execute(select(Contact.id, Bench.id).join(BenchMember, BenchMember.contact_id == Contact.id)
                         .join(Bench, Bench.id == BenchMember.bench_id)
                         .where(Contact.status == "active")).all()
        due = []
        for cid, bid in rows:
            c = s.get(Contact, cid)
            age = timedelta(days=rules.sweep_hot_days if bid in hot_benches else rules.sweep_warm_days)
            if c.linkedin_url and (c.person_checked_at is None or now - c.person_checked_at > age):
                due.append((0 if bid in hot_benches else 1, cid))
        due = [cid for _, cid in sorted(set(due))]
    with PoliteClient() as http:
        for cid in due:
            if left <= 0:
                break
            with session_scope() as s:
                c = s.get(Contact, cid)
                still = person_still_there(s, http, llm, c, s.get(Company, c.company_id))
                left -= 1
                counts["checked"] += 1
                if still is False:
                    counts["departed"] += 1
                    audit.record(s, "m3", "contact_departed", f"{c.full_name} seems to have left ({c.status_reason})",
                                 subject_type="contact", subject_id=str(c.id))
    return dict(counts)


# --- your own overrides ---------------------------------------------------------------------------

def designate(session: Session, cluster_id: int, email: str | None, reason: str) -> RoleChannel:
    """Owner override: make `email` the only channel for this role (or, with None, release the role
    back to automatic channel choice)."""
    score = session.scalar(select(JobScore).where(JobScore.cluster_id == cluster_id))
    if score is None:
        raise ValueError(f"no scored role with cluster id {cluster_id}")
    a = session.get(JobAssessment, score.assessment_id)
    company_id = session.get(JobPosting, a.job_id).company_id
    channel = session.get(RoleChannel, cluster_id) or RoleChannel(cluster_id=cluster_id, company_id=company_id, mode="none")
    session.add(channel)
    if email is None:
        channel.manual, channel.next_retry_at, channel.note = False, _now(), f"released by you: {reason}"
        if channel.mode == "designated":
            channel.mode, channel.designated_email = "none", None
    else:
        channel.mode, channel.designated_email, channel.designated_context = "designated", email.lower(), reason
        channel.manual, channel.bench_id, channel.next_retry_at, channel.note = True, None, None, "set by you"
        if session.scalar(select(ContactEmail).where(ContactEmail.company_id == company_id,
                                                     ContactEmail.email == email.lower())) is None:
            session.add(ContactEmail(company_id=company_id, email=email.lower(), source="manual"))
    audit.record(session, "m3", "channel_override", f"You set the channel for role {cluster_id}: {email or 'automatic'}"
                 f" ({reason})", subject_type="job_cluster", subject_id=str(cluster_id),
                 detail={"email": email, "reason": reason})
    return channel


def add_contact(session: Session, company: Company, full_name: str, email: str | None, title: str | None,
                kind: str) -> Contact:
    """Owner adds a person they know (e.g. a referral). Joins the store; the next bench fill can use them."""
    parts = split_name(full_name)
    if not parts:
        raise ValueError("give a first and last name")
    contact = Contact(company_id=company.id, full_name=full_name, name_key=name_key(full_name), first_name=parts[0],
                      last_name=parts[1], title=title, kind=kind, source="manual", function=B.function_of(title))
    session.add(contact)
    session.flush()
    if email:
        session.add(ContactEmail(company_id=company.id, contact_id=contact.id, email=email.lower(), source="manual"))
    audit.record(session, "m3", "contact_added", f"You added {full_name} at {company.name}",
                 subject_type="contact", subject_id=str(contact.id))
    return contact
