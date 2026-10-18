"""Benches (spec §3.1): the people one company's outreach can use, filled eagerly in one go.

Target selection by company type: startup → founders (+ recruiting); mid-size → TA + hiring manager;
MNC → recruiters per function + geography. Caps from config/outreach.yaml. One human = one slot
(two records that turn out to share an address are merged). Same-name people we can't tell apart
share one frozen slot. Rotation order: function/geo fit → seniority → address confidence → history.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config_loader import get_config
from app.config_schemas import SLOT_KINDS
from app.core.http import PoliteClient
from app.models.company import Company
from app.models.contact import CONFIDENCE_RANK, Bench, BenchMember, Contact, ContactEmail
from app.modules.m1_companies.facts import fact_card, value
from app.modules.m3_outreach.addresses import best_address, find_address
from app.modules.m3_outreach.people import same_name_others, search_people


@dataclass
class TickBudget:
    """Searches this tick may still spend (the monthly cap is enforced separately, in Redis)."""
    searches: int

    def take(self) -> bool:
        if self.searches <= 0:
            return False
        self.searches -= 1
        return True


def company_class(session: Session, company: Company) -> str:
    classes = get_config().outreach.contacts.classes
    size = value(fact_card(session, company.id), "employee_estimate")
    if size in classes.startup_sizes:
        return "startup"
    if size in classes.mid_sizes:
        return "mid_size"
    if size in ("1001-10000", "10000+"):
        return "mnc"
    return "startup" if company.type == "startup" else "mnc"


def function_of(text: str | None) -> str:
    rules = get_config().outreach.contacts
    t = (text or "").lower()
    for name, words in rules.functions.items():
        if any(w in t for w in words):
            return name
    return rules.default_function


def bench_key(shape: str, function: str, geo: str) -> str:
    return f"{function}/{geo}" if shape == "mnc" else "all"


def get_bench(session: Session, company: Company, key: str, shape: str) -> Bench:
    bench = session.scalar(select(Bench).where(Bench.company_id == company.id, Bench.key == key))
    if bench is None:
        bench = Bench(company_id=company.id, key=key, shape=shape, searched={})
        session.add(bench)
        session.flush()
    return bench


def members(session: Session, bench: Bench) -> list[tuple[BenchMember, Contact]]:
    return session.execute(
        select(BenchMember, Contact).join(Contact, Contact.id == BenchMember.contact_id)
        .where(BenchMember.bench_id == bench.id).order_by(BenchMember.rank, BenchMember.id)
    ).all()


def usable_members(session: Session, bench: Bench) -> list[tuple[BenchMember, Contact, ContactEmail]]:
    out = []
    for m, c in members(session, bench):
        if c.status != "active" or m.co_suspects:
            continue
        if (addr := best_address(session, c)) is not None and addr.confidence != "invalid":
            out.append((m, c, addr))
    return out


def _rank_key(session: Session, c: Contact, function: str, geo: str) -> tuple:
    addr = best_address(session, c)
    fit = (c.function == function or c.kind == "founder") + (c.geo == geo or (geo == "remote" and c.geo != "elsewhere"))
    history = (addr.replied_count * 2 + addr.delivered_count) if addr else 0
    return (fit, c.seniority, CONFIDENCE_RANK[addr.confidence] if addr else -1, addr.vouched if addr else False, history)


def geo_fits(bench: Bench, c: Contact, geo: str) -> bool:
    """MNC benches are per geography: an India bench never holds someone known to be elsewhere
    (unknown location stays — ambiguity is not proof). Smaller companies' benches are company-wide."""
    return bench.shape != "mnc" or geo != "india" or c.geo != "elsewhere"


def _drop_departed(session: Session, bench: Bench, geo: str) -> None:
    for m, c in members(session, bench):
        if c.status == "departed" or c.company_id != bench.company_id or not geo_fits(bench, c, geo):
            session.delete(m)
    session.flush()


def fill_bench(session: Session, http: PoliteClient, llm, company: Company, bench: Bench, function: str, geo: str,
               budget: TickBudget, *, pool=None, resolver=None, smtp=None) -> dict:
    """Fill open slots (store first, then one search per slot kind), find addresses, re-rank."""
    from app.modules.m3_outreach.verify import lookup_mx, smtp_check

    rules = get_config().outreach.contacts
    shape = getattr(rules.benches, bench.shape)
    counts = {"searches": 0, "added": 0, "frozen": 0, "addresses": 0}
    now = datetime.now(timezone.utc)
    retry = timedelta(days=rules.retry_no_contact_days)
    _drop_departed(session, bench, geo)
    on_bench = {c.id for _, c in members(session, bench)}
    taken_suspects = {sid for m, _ in members(session, bench) for sid in m.co_suspects}

    for kind in SLOT_KINDS:
        need = getattr(shape, kind) - sum(1 for m, _ in members(session, bench) if m.slot_kind == kind)
        if need <= 0:
            continue
        # Store first: people we already know at this company.
        pool_people = session.scalars(select(Contact).where(
            Contact.company_id == company.id, Contact.kind == kind, Contact.status == "active")).all()
        candidates = [c for c in pool_people if c.id not in on_bench and c.id not in taken_suspects
                      and geo_fits(bench, c, geo)]
        last = bench.searched.get(kind)
        searched_recently = last and now - datetime.fromisoformat(last) < retry
        if len(candidates) < need and not searched_recently and budget.take():
            found = search_people(session, http, llm, company, kind, function, geo, function_of)
            if found is not None:
                counts["searches"] += 1
                bench.searched = {**bench.searched, kind: now.isoformat()}
                ids = {c.id for c in candidates}
                candidates += [c for c, read in found if read.kind == kind and c.id not in on_bench and c.id not in ids
                               and geo_fits(bench, c, geo)]
        candidates.sort(key=lambda c: _rank_key(session, c, function, geo), reverse=True)
        for c in candidates:
            if need <= 0:
                break
            if c.id in taken_suspects:
                continue
            twins = [t for t in same_name_others(session, c)]
            if twins:  # can't tell them apart: one shared, frozen slot — never guessed at
                ids = [c.id, *[t.id for t in twins]]
                for person in (c, *twins):
                    person.status, person.status_reason = "frozen", "same-name profiles at this company"
                session.add(BenchMember(bench_id=bench.id, contact_id=c.id, slot_kind=kind, co_suspects=ids))
                taken_suspects.update(ids)
                counts["frozen"] += 1
            else:
                session.add(BenchMember(bench_id=bench.id, contact_id=c.id, slot_kind=kind, co_suspects=[]))
                counts["added"] += 1
            on_bench.add(c.id)
            need -= 1
        session.flush()

    # Addresses: the first few use enrichment credits; the rest are built from the learned pattern.
    enriched = 0
    startup = bench.shape == "startup"
    seen_emails: dict[str, int] = {}
    for m, c in members(session, bench):
        if m.co_suspects or c.status != "active":
            continue
        addr = best_address(session, c)
        if addr is None or addr.confidence in ("unchecked",):
            allow = enriched < rules.enrichment.enrich_per_bench
            addr = find_address(session, http, c, company, allow_enrich=allow, startup=startup, pool=pool,
                                resolver=resolver or lookup_mx, smtp=smtp or smtp_check)
            if allow and addr is not None and addr.source in ("hunter", "apollo"):
                enriched += 1
            if addr is not None:
                counts["addresses"] += 1
        if addr is not None and addr.email in seen_emails:  # one human = one bench slot
            session.delete(m)
            continue
        if addr is not None:
            seen_emails[addr.email] = c.id
    session.flush()

    # Rotation order.
    ranked = sorted(members(session, bench), key=lambda mc: _rank_key(session, mc[1], function, geo), reverse=True)
    for i, (m, _) in enumerate(ranked, start=1):
        m.rank = i if not m.co_suspects else 99
    bench.filled_at = now
    return counts


def open_slots(session: Session, bench: Bench) -> int:
    shape = getattr(get_config().outreach.contacts.benches, bench.shape)
    return max(0, shape.cap - len(members(session, bench)))
