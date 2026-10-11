"""Operator commands.

    python -m app.cli selftest       # end-to-end check of the whole stack; exit code 1 on failure
    python -m app.cli scout <name>   # run one scout now (seeds, yc, adzuna, jooble, remotive, hn)
    python -m app.cli resolve        # run one batch of the domain resolver now
    python -m app.cli stats          # what's in the database + per-scout health + AI spend
    python -m app.cli evaluate [--limit N] [--domain acme.com] [--force]
                                     # judge companies now (respects the daily limit unless --force)
    python -m app.cli verdicts [--last 20] [--rejected | --passed] [--details]
    python -m app.cli override <domain> keep|reject "<reason>" [--permanent]
    python -m app.cli activate [--limit N] [--domain acme.com]   # set up job feeds for kept companies now
    python -m app.cli sync [--limit N] [--domain acme.com]       # read due feeds now
    python -m app.cli feeds                                      # watchlist health
    python -m app.cli jobs --domain acme.com                     # a company's open postings
    python -m app.cli garden                                     # run the weekly upkeep now
"""

import argparse
import sys
import time

from app.api.health import check_config, check_database, check_redis, check_scheduler
from app.settings import get_settings


def _line(ok: bool, name: str, detail: str = "") -> bool:
    print(f"  [{'ok' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    return ok


def _check_embeddings() -> bool:
    from app.constants import EMBEDDING_DIM
    from app.core.embeddings import embed

    started = time.monotonic()
    vectors = embed(["Senior Data Analyst, remote India", "Lead Analytics Specialist, WFH (IN)"])
    ms = (time.monotonic() - started) * 1000
    return _line(len(vectors[0]) == EMBEDDING_DIM, "embeddings", f"{len(vectors[0])} dims in {ms:.0f} ms")


def _check_memory_roundtrip() -> bool:
    """Write precedents, read them back by meaning, check dedupe — then roll everything back."""
    from app.core.memory import remember, search
    from app.db import get_session_factory

    session = get_session_factory()()
    try:
        first = remember(
            session,
            "precedent",
            "For an analytics career, Uber is a step-up brand compared to a pharma company",
            {"verdict": "pass"},
        )
        remember(session, "precedent", "Food delivery clones are not novel startups", {"verdict": "fail"})
        hits = search(session, "precedent", "Is a ride-hailing tech company better than pharma for analytics?", k=1)
        found = bool(hits) and hits[0][0].id == first.id
        _line(found, "memory search by meaning", f"similarity {hits[0][1]:.2f}" if hits else "no hits")

        second = remember(
            session,
            "precedent",
            "For an analytics career, Uber is a step-up brand compared with a pharma company",
            {"verdict": "pass"},
        )
        session.flush()
        deduped = first.status == "superseded" and first.superseded_by_id == second.id
        _line(deduped, "memory dedupe gate", "near-duplicate superseded the old precedent")
        return found and deduped
    finally:
        session.rollback()
        session.close()


def _check_celery_roundtrip() -> bool:
    from app.celery_app import celery_app

    try:
        reply = celery_app.send_task("system.ping").get(timeout=30)
    except Exception as e:
        return _line(False, "celery round-trip", str(e))
    return _line(True, "celery round-trip", reply)


def selftest() -> int:
    print("Job Agent self-test")
    results = []

    cfg = check_config()
    results.append(_line(cfg["ok"], "config", f"version {cfg['version']}, files: {', '.join(cfg['files'])}"))

    db = check_database()
    results.append(
        _line(db["ok"], "database + pgvector", f"pgvector {db.get('pgvector')}, migration {db.get('migration')}"
              if db["ok"] else db.get("error", "pgvector extension missing"))
    )

    r = check_redis()
    results.append(_line(r["ok"], "redis", r.get("error", "")))

    for check in (_check_embeddings, _check_memory_roundtrip, _check_celery_roundtrip):
        try:
            results.append(check())
        except Exception as e:
            results.append(_line(False, check.__name__.removeprefix("_check_"), f"{type(e).__name__}: {e}"))

    sched = check_scheduler()
    results.append(
        _line(sched["ok"], "beat scheduler heartbeat",
              f"{sched['age_seconds']}s ago" if "age_seconds" in sched else sched.get("error", ""))
    )

    has_key = bool(get_settings().anthropic_api_key)
    results.append(_line(has_key, "ANTHROPIC_API_KEY set", "" if has_key else "required before Module 1"))

    from app.config_loader import get_config

    owner = get_config().owner
    if owner is None:
        results.append(_line(False, "owner_stub.yaml", "missing — copy config/owner_stub.example.yaml"))
    else:
        placeholders = owner.placeholder_fields()
        results.append(_line(not placeholders, "owner_stub.yaml filled in",
                             "still has sample values: " + ", ".join(placeholders) if placeholders else
                             f"{len(owner.employment_history)} jobs, {len(owner.target_roles)} target roles"))

    passed = sum(results)
    print(f"\n{passed}/{len(results)} checks passed")
    return 0 if all(results) else 1


def scout(name: str) -> int:
    from app.modules.m1_companies.runner import run_scout

    print(f"Running scout '{name}'…")
    result = run_scout(name)
    print(f"  status: {result['status']}")
    for key in ("reason", "error"):
        if result.get(key):
            print(f"  {key}: {result[key]}")
    for outcome, n in sorted(result.get("counts", {}).items()):
        print(f"  {outcome}: {n}")
    return 0 if result["status"] in ("ok", "skipped") else 1


def resolve() -> int:
    from app.modules.m1_companies.resolver import run_resolver

    print("Resolving domains for companies seen by name only…")
    for outcome, n in sorted(run_resolver().items()):
        print(f"  {outcome}: {n}")
    return 0


def stats() -> int:
    from sqlalchemy import func, select

    from app.db import session_scope
    from app.models import Company, CompanySighting, ContactLead
    from app.modules.m1_companies.health import scout_health

    with session_scope() as s:
        print("Companies by status:")
        for status, n in s.execute(select(Company.status, func.count()).group_by(Company.status)).all():
            print(f"  {status:<12} {n}")
        print("Companies by type:")
        for ctype, n in s.execute(select(Company.type, func.count()).group_by(Company.type)).all():
            print(f"  {ctype:<12} {n}")
        print("Companies seen per scout:")
        per_scout = select(CompanySighting.scout, func.count(func.distinct(CompanySighting.company_id)))
        for name, n in s.execute(per_scout.group_by(CompanySighting.scout)).all():
            print(f"  {name:<12} {n}")
        print(f"Contact leads: {s.scalar(select(func.count()).select_from(ContactLead))}")
        print("Scout health:")
        for h in scout_health(s):
            last = h["last_run"]
            when = f"last run {last['started_at']:%Y-%m-%d %H:%M} UTC, {last['status']}" if last else "never run"
            note = h["reason"] or (last or {}).get("error") or ""
            print(f"  {h['scout']:<9} {h['health']:<15} {when}" + (f" — {note[:120]}" if note else ""))

        from app.config_loader import get_config
        from app.models import LLMCall
        from app.modules.m1_companies.queue import judged_today, start_of_today_utc

        limit = get_config().rules.evaluation.max_new_per_day
        print(f"Judged today: {judged_today(s)} / {limit}")
        for label, since in (("today", start_of_today_utc()), ("all time", None)):
            q = select(func.count(), func.coalesce(func.sum(LLMCall.cost_usd), 0))
            if since is not None:
                q = q.where(LLMCall.created_at >= since)
            calls, cost = s.execute(q).one()
            print(f"AI spend {label}: ${float(cost):.4f} over {calls} calls")
    return 0


def evaluate(limit: int | None, domain: str | None, force: bool) -> int:
    from sqlalchemy import select

    from app.db import session_scope
    from app.models import Company
    from app.modules.m1_companies.queue import run_batch

    ids = None
    if domain:
        with session_scope() as s:
            cid = s.scalar(select(Company.id).where(Company.domain == domain.lower()))
        if cid is None:
            print(f"No company with domain {domain}")
            return 1
        ids = [cid]
    print("Evaluating…")
    for k, v in sorted(run_batch(limit=limit, force=force, company_ids=ids).items()):
        print(f"  {k}: {v}")
    return 0


def verdicts(last: int, passed: bool | None, details: bool = False) -> int:
    import textwrap

    from sqlalchemy import select

    from app.db import session_scope
    from app.models import Company, CompanyVerdict

    with session_scope() as s:
        q = select(CompanyVerdict, Company).join(Company, Company.id == CompanyVerdict.company_id)
        if passed is not None:
            q = q.where(CompanyVerdict.passed.is_(passed))
        rows = s.execute(q.order_by(CompanyVerdict.created_at.desc()).limit(last)).all()
        for v, c in rows:
            mark = "KEEP  " if v.passed else ("BLOCK " if v.permanent else "REJECT")
            who = "you" if v.decided_by == "owner" else v.profile
            print(f"{mark} {c.name} ({c.domain}) [{who}, ${v.cost_usd:.3f}]")
            print(textwrap.fill(v.summary, width=100, initial_indent="       ", subsequent_indent="       "))
            if details:
                for check in v.checks:
                    flag = "✓" if check.get("outcome") == "pass" else "✗"
                    print(textwrap.fill(f"{flag} {check.get('name')}: {check.get('reason')}", width=100,
                                        initial_indent="         ", subsequent_indent="           "))
            print()
    return 0


def override_cmd(domain: str, decision: str, reason: str, permanent: bool) -> int:
    from sqlalchemy import select

    from app.db import session_scope
    from app.models import Company
    from app.modules.m1_companies.override import override

    with session_scope() as s:
        company = s.scalar(select(Company).where(Company.domain == domain.lower()))
        if company is None:
            print(f"No company with domain {domain}")
            return 1
        v = override(s, company, "pass" if decision == "keep" else "fail", reason, permanent=permanent)
        print(f"{company.name}: {v.summary} (saved as a precedent for future judgments)")
    return 0


def _company_ids(domain: str | None) -> list[int] | None:
    if not domain:
        return None
    from sqlalchemy import select

    from app.db import session_scope
    from app.models import Company

    with session_scope() as s:
        cid = s.scalar(select(Company.id).where(Company.domain == domain.lower()))
    if cid is None:
        raise SystemExit(f"No company with domain {domain}")
    return [cid]


def _print_counts(result: dict) -> None:
    for k, v in sorted(result.items()):
        print(f"  {k}: {v}")
    if not result:
        print("  nothing to do")


def activate_cmd(limit: int | None, domain: str | None) -> int:
    from app.modules.m1_companies.watch.queue import run_activation_batch

    print("Setting up job feeds…")
    _print_counts(run_activation_batch(limit=limit, company_ids=_company_ids(domain)))
    return 0


def sync_cmd(limit: int | None, domain: str | None) -> int:
    from app.modules.m1_companies.watch.queue import run_sync_batch

    print("Reading job feeds…")
    _print_counts(run_sync_batch(limit=limit, company_ids=_company_ids(domain)))
    return 0


def garden_cmd() -> int:
    from app.modules.m1_companies.watch.queue import run_gardening

    print("Running weekly upkeep…")
    _print_counts(run_gardening())
    return 0


def feeds_cmd() -> int:
    from sqlalchemy import func, select

    from app.db import session_scope
    from app.models import Company, CompanyFeed

    with session_scope() as s:
        print("Feeds by reading method:")
        for method, n, jobs in s.execute(
            select(CompanyFeed.method, func.count(), func.sum(CompanyFeed.open_jobs)).group_by(CompanyFeed.method)
        ).all():
            print(f"  {method:<16} {n:>4} companies, {jobs or 0:>5} open jobs")
        print("Feeds by health:")
        for health, n in s.execute(select(CompanyFeed.health, func.count()).group_by(CompanyFeed.health)).all():
            print(f"  {health:<16} {n}")
        print("Companies:")
        for name, domain, method, slug, jobs, health in s.execute(
            select(Company.name, Company.domain, CompanyFeed.method, CompanyFeed.slug,
                   CompanyFeed.open_jobs, CompanyFeed.health)
            .join(Company, Company.id == CompanyFeed.company_id).order_by(Company.name).limit(60)
        ).all():
            board = f"{method}/{slug}" if slug else method
            print(f"  {name[:32]:<32} {domain[:24]:<24} {board[:30]:<30} {jobs:>4} jobs  {health}")
        trouble = s.execute(
            select(Company.name, Company.domain, CompanyFeed.method, CompanyFeed.health, CompanyFeed.last_error)
            .join(Company, Company.id == CompanyFeed.company_id)
            .where(CompanyFeed.health.in_(("failing", "stale"))).limit(20)
        ).all()
        if trouble:
            print("Needs attention (self-healing will retry):")
            for name, domain, method, health, err in trouble:
                print(f"  {name} ({domain}) {method} {health}: {(err or '')[:100]}")
    return 0


def jobs_cmd(domain: str) -> int:
    from sqlalchemy import select

    from app.db import session_scope
    from app.models import CompanyFeed, RawPosting

    ids = _company_ids(domain)
    with session_scope() as s:
        feed = s.scalar(select(CompanyFeed).where(CompanyFeed.company_id == ids[0]))
        if feed is None:
            print("No feed yet — run: python -m app.cli activate --domain " + domain)
            return 0
        print(f"{domain}: {feed.method}" + (f"/{feed.slug}" if feed.slug else "") + f", health {feed.health}")
        print(f"  ownership: {feed.ownership_evidence}")
        rows = s.scalars(select(RawPosting).where(RawPosting.feed_id == feed.id, RawPosting.closed_at.is_(None))
                         .order_by(RawPosting.first_seen_at.desc()).limit(40)).all()
        for p in rows:
            print(f"  - {p.title} | {p.location or '—'}" + (" (lead only)" if p.lead_only else ""))
            if p.url:
                print(f"    {p.url}")
        print(f"{len(rows)} shown (open postings)")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("selftest", help="end-to-end check of the whole stack")
    p_scout = sub.add_parser("scout", help="run one scout now")
    p_scout.add_argument("name")
    sub.add_parser("resolve", help="run one domain-resolver batch now")
    sub.add_parser("stats", help="database contents + scout health + AI spend")
    p_eval = sub.add_parser("evaluate", help="judge companies now")
    p_eval.add_argument("--limit", type=int)
    p_eval.add_argument("--domain")
    p_eval.add_argument("--force", action="store_true", help="ignore the daily limit")
    p_ver = sub.add_parser("verdicts", help="recent verdicts with reasons")
    p_ver.add_argument("--last", type=int, default=20)
    p_ver.add_argument("--details", action="store_true", help="also list every check and its reason")
    group = p_ver.add_mutually_exclusive_group()
    group.add_argument("--passed", action="store_true")
    group.add_argument("--rejected", action="store_true")
    p_ovr = sub.add_parser("override", help="overrule a verdict (becomes a precedent)")
    p_ovr.add_argument("domain")
    p_ovr.add_argument("decision", choices=["keep", "reject"])
    p_ovr.add_argument("reason")
    p_ovr.add_argument("--permanent", action="store_true", help="reject forever (never auto-revive)")
    for name, help_text in (("activate", "set up job feeds now"), ("sync", "read due job feeds now")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--limit", type=int)
        p.add_argument("--domain")
    sub.add_parser("feeds", help="watchlist health")
    p_jobs = sub.add_parser("jobs", help="a company's open postings")
    p_jobs.add_argument("--domain", required=True)
    sub.add_parser("garden", help="run the weekly upkeep now")
    args = parser.parse_args()

    if args.command == "selftest":
        sys.exit(selftest())
    if args.command == "scout":
        sys.exit(scout(args.name))
    if args.command == "resolve":
        sys.exit(resolve())
    if args.command == "stats":
        sys.exit(stats())
    if args.command == "evaluate":
        sys.exit(evaluate(args.limit, args.domain, args.force))
    if args.command == "verdicts":
        sys.exit(verdicts(args.last, True if args.passed else False if args.rejected else None, args.details))
    if args.command == "override":
        sys.exit(override_cmd(args.domain, args.decision, args.reason, args.permanent))
    if args.command == "activate":
        sys.exit(activate_cmd(args.limit, args.domain))
    if args.command == "sync":
        sys.exit(sync_cmd(args.limit, args.domain))
    if args.command == "feeds":
        sys.exit(feeds_cmd())
    if args.command == "jobs":
        sys.exit(jobs_cmd(args.domain))
    if args.command == "garden":
        sys.exit(garden_cmd())


if __name__ == "__main__":
    main()
