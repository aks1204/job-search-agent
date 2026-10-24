"""Operator commands.

    python -m app.cli selftest       # end-to-end check of the whole stack; exit code 1 on failure
    python -m app.cli scout <name>   # run one scout now (seeds, yc, adzuna, jooble, remotive, hn)
    python -m app.cli resolve        # run one batch of the domain resolver now
    python -m app.cli stats          # what's in the database + per-scout health + AI spend
    python -m app.cli evaluate [--limit N] [--domain acme.com] [--force]
                                     # judge companies now (respects the daily limit unless --force)
    python -m app.cli verdicts [--last 20] [--rejected | --passed] [--details]
    python -m app.cli override <domain> keep|reject "<reason>" [--permanent]
    python -m app.cli employers [fix "<name>" <domain>|none]     # your current / past employers: never watched
    python -m app.cli activate [--limit N] [--domain acme.com]   # set up job feeds for kept companies now
    python -m app.cli sync [--limit N] [--domain acme.com]       # read due feeds now
    python -m app.cli feeds                                      # watchlist health
    python -m app.cli jobs --domain acme.com                     # a company's open postings
    python -m app.cli garden                                     # run the weekly upkeep now
    python -m app.cli pipeline [--refilter]                      # Module 2: process job postings now
    python -m app.cli funnel                                     # Module 2: counts + top filter reasons
    python -m app.cli passed [--domain acme.com]                 # Module 2: postings that passed the free filters
    python -m app.cli assess [--limit N] [--force]               # Module 2: AI-read descriptions now
    python -m app.cli assessed [--passed|--rejected] [--domain]  # Module 2: AI-read roles, reasons, flags
    python -m app.cli rates [--refresh]                          # Module 2: PPP / exchange-rate factors
    python -m app.cli brand <domain> up|lateral|down "<reason>"  # your own brand call for a company
    python -m app.cli score [--limit N] [--force]                # Module 2: similarity + AI grades now
    python -m app.cli shortlist [--band fast_lane|inbox|maybe]   # Module 2: scored roles, best first
    python -m app.cli feedback <cluster_id> act|skip|thumbs_up|thumbs_down|maybe_rescue ["reason"]
    python -m app.cli contacts [--cluster N] [--limit N]           # Module 3: find contacts for pursued roles
    python -m app.cli channel <cluster_id>                       # Module 3: designated address or bench for a role
    python -m app.cli bench <domain>                             # Module 3: a company's benches
    python -m app.cli designate <cluster_id> <email>|auto "<reason>"  # your override: the only address for a role
    python -m app.cli add-contact <domain> "<name>" [--email] [--title] [--kind]
    python -m app.cli sweep                                      # Module 3: re-check people on benches
    python -m app.cli lookups                                    # Module 3: contact services + free-tier use
    python -m app.cli drafts [--cluster N] [--force]              # Module 3: write outreach drafts now
    python -m app.cli draft <cluster_id>                         # Module 3: show a draft as recipients get it
    python -m app.cli gmail connect|status|test                  # Module 3: your Gmail (Google sign-in)
    python -m app.cli review [show|approve|skip|export|import|redraft <campaign>] [--email N|--all] [--allow-low]
    python -m app.cli outreach status|pause|unpause|reset-breaker | hold|release|stop <domain>
    python -m app.cli replies [--last N]                         # Module 3: replies and how they were sorted
    python -m app.cli pings [--all] [--read]                     # what the agent wants you to know
    python -m app.cli apply                                      # Module 4: your apply list
    python -m app.cli applied <cluster_id> ["note"]              # Module 4: mark a role applied
    python -m app.cli apply-skip <cluster_id> "<reason>"
    python -m app.cli applications [--status S]                  # Module 4: where each application stands
    python -m app.cli story <cluster_id>                         # one role's whole story
    python -m app.cli track                                      # Module 4: run the tracker now
    python -m app.cli assign <no> [<role id> | --other "Job title" | --ignore]   # Module 4: which role an email is about
    python -m app.cli profile read|export|confirm|show|edit     # Module 5: your profile from your resume
    python -m app.cli direction [refresh | yes <id> | no <id> | edit | confirm]   # Module 5: career direction
    python -m app.cli memory [list|search <words>|pin|unpin|forget|restore|keep <id>]   # Module 5: memory
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
        # A slow reply usually means every worker slot is busy with a long round (judging, syncing).
        # If the worker answers an inspect call, it's alive — report what it's doing instead of failing.
        try:
            active = celery_app.control.inspect(timeout=3).active() or {}
        except Exception:
            active = {}
        running = sorted({t.get("name", "?") for tasks in active.values() for t in tasks})
        if active:
            return _line(True, "celery round-trip", "worker alive but busy running " + ", ".join(running or ["tasks"]))
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

    resume = get_settings().config_dir / "resume.pdf"
    results.append(_line(resume.is_file(), "config/resume.pdf present",
                         f"{resume.stat().st_size // 1024} KB" if resume.is_file() else "needed for Module 2 scoring"))

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


def employers_cmd(action: str, name: str | None, domain: str | None) -> int:
    from app.core.http import PoliteClient
    from app.core.llm import LLM
    from app.db import session_scope
    from app.modules.m1_companies import employers as E

    with session_scope() as s:
        if action == "fix":
            if not name or not domain:
                print('Usage: employers fix "<employer name>" <domain>|none')
                return 1
            try:
                e, released = E.fix(s, name, None if domain.lower() == "none" else domain)
            except ValueError as err:
                print(err)
                return 1
            if e is None:
                print(f"“{name}” isn't an employer in your profile. The list: python -m app.cli employers")
                return 1
            print(f"{e.name}: website set to {e.domain or 'none (matched by name only)'}")
            out = {"released": released, "blocked": E.apply(s)}
        else:
            with PoliteClient() as http:
                out = E.refresh(s, LLM() if LLM.available() else None, http)
        known = E.employers(s)
        if not known:
            print("No employers in your profile yet (profile show).")
            return 0
        print("Your employers — never watched or contacted:")
        for e in known:
            blocked = E.blocked_for(s, e)
            seen = f" — not watching: {', '.join(c.name for c in blocked)}" if blocked else ""
            print(f"  {e.name} ({'current' if e.current else 'past'}): "
                  f"{e.domain or 'website not found yet — matched by exact name only'}{seen}")
        print(f"{out['blocked']} compan{'y' if out['blocked'] == 1 else 'ies'} newly taken off your watchlist.")
        if out.get("released"):
            print(f"{out['released']} wrongly matched compan{'y' if out['released'] == 1 else 'ies'} "
                  "put back for a fresh look.")
        print('Wrong website? employers fix "<name>" <domain>. Keep one anyway? override <domain> keep "<reason>"')
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


def probe_workday_cmd(tenant: str, site: str, server: int | None) -> int:
    """Diagnose a Workday board from this machine: which server answers, and how."""
    from app.core.http import USER_AGENT, PoliteClient
    from app.modules.m1_companies.watch.readers import WORKDAY_SERVERS, probe_workday

    print(f"Probing Workday tenant '{tenant}', site '{site}' (user-agent: {USER_AGENT})")
    servers = [server] if server else WORKDAY_SERVERS
    with PoliteClient() as http:
        for n in servers:
            outcome = probe_workday(http, f"{tenant}.wd{n}", tenant, site)
            print(f"  wd{n:<4} {outcome}")
            if outcome == "ok":
                print(f"Live board: https://{tenant}.wd{n}.myworkdayjobs.com/{site}")
                return 0
    print("No server answered for this tenant/site.")
    return 1


def coverage_cmd() -> int:
    from app.db import session_scope
    from app.modules.m1_companies.watch.coverage import coverage

    with session_scope() as s:
        r = coverage(s)
    print(f"Kept companies: {r['kept_companies']}")
    print(f"  read directly:        {r['read_directly']}  " +
          ", ".join(f"{m} {n}" for m, n in r["direct_by_method"].items()))
    print(f"  job-site fallback:    {r['on_job_site_fallback']}")
    print(f"  awaiting feed setup:  {r['awaiting_setup']}  (set up automatically every 15 min)")
    empty = r["direct_but_no_matching_openings"]
    if empty["count"]:
        print(f"  readable, no matching openings right now: {empty['count']}  e.g. {', '.join(empty['examples'][:3])}")
    if r["gaps"]:
        print("Gaps (fallback companies, by cause):")
        for g in r["gaps"]:
            print(f"  {g['companies']:>3}  {g['reason']}  — e.g. {', '.join(g['examples'][:3])}")
    if r["build_next"]:
        print("Readers that would close the most gaps: " + ", ".join(r["build_next"]))
    return 0


def pipeline_cmd(refilter: bool) -> int:
    from app.db import session_scope
    from app.modules.m2_jobs.pipeline import refilter_all, run_pipeline

    if refilter:
        with session_scope() as s:
            print(f"Re-applied filters: {dict(refilter_all(s))}")
    print("Processing job postings…")
    _print_counts(run_pipeline())
    return 0


def funnel_cmd() -> int:
    from app.db import session_scope
    from app.modules.m2_jobs.pipeline import funnel

    with session_scope() as s:
        f = funnel(s)
    print(f"Open postings: {f['open_postings']}  (job-site/HN/WaaS leads among them: {f['open_leads']})")
    for status, n in sorted(f["by_filter_status"].items()):
        print(f"  {status:<8} {n}")
    if f["passed_by_description"]:
        print("Passed — description: " + ", ".join(f"{k} {v}" for k, v in sorted(f["passed_by_description"].items())))
    if f["top_filter_reasons"]:
        print("Most common reasons filtered out:")
        for reason, n in f["top_filter_reasons"]:
            print(f"  {n:>5}  {reason}")
    print(f"Sibling clusters (same role, several req IDs): {f['sibling_clusters']}")
    if f["ai_read_by_outcome"]:
        print("AI read (one per role): " + ", ".join(f"{k} {v}" for k, v in sorted(f["ai_read_by_outcome"].items())))
        for reason, n in f["top_ai_reject_reasons"]:
            print(f"  {n:>5}  {reason}")
    return 0


def assess_cmd(limit: int | None, force: bool) -> int:
    from app.modules.m2_jobs.assess import run_assessments

    print("Reading job descriptions (AI) and applying your rules…")
    _print_counts(run_assessments(limit=limit, force=force))
    return 0


def assessed_cmd(outcome: str | None, domain: str | None, last: int) -> int:
    import textwrap

    from sqlalchemy import select

    from app.db import session_scope
    from app.models import Company, JobAssessment, JobPosting

    with session_scope() as s:
        q = (select(JobAssessment, JobPosting, Company).join(JobPosting, JobPosting.id == JobAssessment.job_id)
             .join(Company, Company.id == JobPosting.company_id))
        if outcome:
            q = q.where(JobAssessment.outcome == outcome)
        if domain:
            q = q.where(Company.domain == domain.lower())
        rows = s.execute(q.order_by(JobAssessment.updated_at.desc()).limit(last)).all()
        for a, j, c in rows:
            mark = "PASS  " if a.outcome == "passed" else "REJECT"
            level = a.level.get("job") or "?"
            comp = a.comp.get("verdict", "?")
            print(f"{mark} {c.name}: {j.title} | {j.location or '—'}")
            print(f"       {a.read.get('one_liner', '')}  [level {level}, pay {comp}, ${a.cost_usd:.4f}]")
            for line in a.reasons + [f"flag: {x}" for x in a.flags] + [f"penalty: {p['points']} {p['name']}" for p in a.penalties]:
                print(textwrap.fill(line, 100, initial_indent="         - ", subsequent_indent="           "))
        print(f"{len(rows)} shown")
    return 0


def brand_cmd(domain: str, level: str, reason: str) -> int:
    from sqlalchemy import select

    from app.db import session_scope
    from app.models import Company
    from app.modules.m2_jobs.brand import set_brand

    with session_scope() as s:
        company = s.scalar(select(Company).where(Company.domain == domain.lower()))
        if company is None:
            print(f"No company with domain {domain}")
            return 1
        n = set_brand(s, company, level, reason)
        print(f"{company.name}: brand recorded as '{level}' (saved as a precedent). "
              f"{n} assessed role(s) will be re-checked for free on the next round.")
    return 0


def score_cmd(limit: int | None, force: bool) -> int:
    from app.modules.m2_jobs.scoring import run_scoring

    print("Scoring roles (similarity for all, AI grade for the top ones)…")
    _print_counts(run_scoring(grade_limit=limit, force=force))
    return 0


BAND_LABEL = {"fast_lane": "FAST LANE", "inbox": "INBOX", "maybe": "MAYBE", "archive": "ARCHIVE"}


def shortlist_cmd(band: str | None, limit: int) -> int:
    import textwrap

    from sqlalchemy import select

    from app.db import session_scope
    from app.models import Company, JobAssessment, JobCluster, JobPosting, JobScore

    with session_scope() as s:
        q = (select(JobScore, JobAssessment, JobPosting, Company, JobCluster)
             .join(JobAssessment, JobAssessment.id == JobScore.assessment_id)
             .join(JobPosting, JobPosting.id == JobAssessment.job_id)
             .join(Company, Company.id == JobPosting.company_id)
             .join(JobCluster, JobCluster.id == JobScore.cluster_id))
        q = q.where(JobScore.band == band) if band else q.where(JobScore.band != "archive")
        q = q.where(Company.status != "blocked")              # e.g. your current / a past employer
        rows = s.execute(q.order_by(JobScore.score.desc()).limit(limit)).all()
        for sc, a, j, c, cl in rows:
            graded = "AI-graded" if sc.graded else "similarity only"
            siblings = f" · {cl.size} req IDs" if cl.size > 1 else ""
            print(f"[{sc.score:>3}] {BAND_LABEL[sc.band]:<9} {c.name}: {j.title} | {j.location or '—'}{siblings}")
            print(f"        {sc.grade.get('one_liner') or a.read.get('one_liner', '')}  ({graded}, cluster {cl.id})")
            for r in sc.grade.get("reasons", [])[:3]:
                print(textwrap.fill(r, 100, initial_indent="          + ", subsequent_indent="            "))
            if sc.grade.get("missing"):
                print(textwrap.fill("gaps: " + ", ".join(sc.grade["missing"][:5]), 100,
                                    initial_indent="          - ", subsequent_indent="            "))
            notes = [*a.flags, *(f"{p['points']} {p['name']}" for p in sc.breakdown.get("penalties", [])),
                     *sc.breakdown.get("caps", [])]
            if notes:
                print(textwrap.fill("notes: " + "; ".join(notes), 100,
                                    initial_indent="          · ", subsequent_indent="            "))
            if j.url:
                print(f"          {j.url}")
        print(f"{len(rows)} shown")
    return 0


def feedback_cmd(cluster_id: int, action: str, reason: str | None, why: list[str] | None = None) -> int:
    from sqlalchemy import select

    from app.db import session_scope
    from app.models import JobScore
    from app.modules.m2_jobs.scoring import record_feedback
    from app.modules.m5_profile.reasons import explain

    with session_scope() as s:
        row = s.scalar(select(JobScore).where(JobScore.cluster_id == cluster_id))
        if row is None:
            print(f"No scored role with cluster id {cluster_id} (see `shortlist`)")
            return 1
        try:
            fb = record_feedback(s, row, action, reason, chips=why)
        except ValueError as e:
            print(e)
            return 1
        print(f"Recorded '{action}'. Role now: {row.score} → {row.band}")
        if action in ("skip", "thumbs_down"):
            print(f"  Reason — {explain(fb.chips, fb.inferred)}")
            if not fb.chips:
                print("  Not right? Run it again with --why too_junior|too_senior|not_my_field|location|company|pay|hours_travel")
    return 0


def rates_cmd(refresh: bool) -> int:
    from app.core.http import PoliteClient
    from app.db import session_scope
    from app.modules.m2_jobs.rates import factors, refresh_from_world_bank

    if refresh:
        with PoliteClient() as http, session_scope() as s:
            print(f"World Bank refresh: {refresh_from_world_bank(s, http)}")
    with session_scope() as s:
        for cur, (ppp, fx, source) in factors(s).items():
            print(f"  {cur}  PPP {ppp:>8.3f} per intl $   FX {fx:>8.3f} per US$   ({source})")
    return 0


def passed_cmd(domain: str | None, limit: int) -> int:
    from sqlalchemy import select

    from app.db import session_scope
    from app.models import Company, JobPosting

    with session_scope() as s:
        q = (select(JobPosting, Company).join(Company, Company.id == JobPosting.company_id)
             .where(JobPosting.status == "open", JobPosting.filter_status == "passed"))
        if domain:
            q = q.where(Company.domain == domain.lower())
        rows = s.execute(q.order_by(JobPosting.first_seen_at.desc()).limit(limit)).all()
        for j, c in rows:
            where = "India" if j.is_india else ("remote" if j.is_remote else "abroad")
            age = f"posted {j.posted_at:%d %b}" if j.posted_at else "date unknown"
            tag = " · lead" if j.lead_only else ""
            print(f"- {c.name}: {j.title} | {j.location or '—'} [{where}, {j.employment_type}, {age}, "
                  f"description {j.description_status}{tag}]")
        print(f"{len(rows)} shown (passed the free filters; the AI read comes in Part 2b)")
    return 0


# --- Module 3 Part 3a: contacts ---------------------------------------------------------------

def contacts_cmd(cluster_id: int | None, limit: int | None) -> int:
    from app.modules.m3_outreach.pipeline import run_contacts

    print("Finding contacts for pursued roles (no email is sent)…")
    _print_counts(run_contacts(cluster_ids=[cluster_id] if cluster_id else None, limit=limit))
    return 0


def _print_bench(s, bench) -> None:
    from app.modules.m3_outreach.addresses import best_address
    from app.modules.m3_outreach.bench import members, open_slots

    rows = members(s, bench)
    print(f"  bench '{bench.key}' ({bench.shape}): {len(rows)} people, {open_slots(s, bench)} open slot(s)")
    for m, c in rows:
        addr = best_address(s, c)
        tag = "FROZEN (same-name profiles)" if m.co_suspects else c.status if c.status != "active" else ""
        where = f" · {c.location}" if c.location else ""
        print(f"   {m.rank:>2}. {c.full_name} — {c.title or c.kind}{where}  [{m.slot_kind}] {tag}")
        if addr:
            extra = " · provider vouches" if addr.vouched else ""
            print(f"       {addr.email}  ({addr.confidence}, {addr.source}{extra})")
        else:
            print("       no address yet")
        if c.linkedin_url:
            print(f"       {c.linkedin_url}")


def channel_cmd(cluster_id: int) -> int:
    from app.db import session_scope
    from app.models import Bench, Company, RoleChannel

    with session_scope() as s:
        ch = s.get(RoleChannel, cluster_id)
        if ch is None:
            print(f"No channel yet for role {cluster_id} — run: python -m app.cli contacts --cluster {cluster_id}")
            return 1
        company = s.get(Company, ch.company_id)
        print(f"{company.name} · role {cluster_id}: {ch.mode.upper()}" + (" (set by you)" if ch.manual else ""))
        if ch.note:
            print(f"  {ch.note}")
        if ch.designated_email:
            print(f"  address: {ch.designated_email}")
        if ch.designated_context:
            print(f"  “{ch.designated_context[:200]}”")
        if ch.next_retry_at:
            print(f"  next look: {ch.next_retry_at:%d %b %Y}")
        if ch.bench_id and ch.mode != "designated":
            _print_bench(s, s.get(Bench, ch.bench_id))
    return 0


def bench_cmd(domain: str) -> int:
    from sqlalchemy import select

    from app.db import session_scope
    from app.models import Bench

    ids = _company_ids(domain)
    with session_scope() as s:
        benches = s.scalars(select(Bench).where(Bench.company_id == ids[0])).all()
        if not benches:
            print("No bench yet (benches are filled when a role there is pursued).")
        for b in benches:
            _print_bench(s, b)
    return 0


def designate_cmd(cluster_id: int, email: str, reason: str) -> int:
    from app.db import session_scope
    from app.modules.m3_outreach.pipeline import designate

    with session_scope() as s:
        try:
            ch = designate(s, cluster_id, None if email == "auto" else email, reason)
        except ValueError as e:
            print(e)
            return 1
        print(f"Role {cluster_id}: " + (f"only {ch.designated_email} will be used." if ch.manual
                                        else "back to automatic (decided on the next round)."))
    return 0


def add_contact_cmd(domain: str, name: str, email: str | None, title: str | None, kind: str) -> int:
    from sqlalchemy import select

    from app.db import session_scope
    from app.models import Company
    from app.modules.m3_outreach.pipeline import add_contact

    with session_scope() as s:
        company = s.scalar(select(Company).where(Company.domain == domain.lower()))
        if company is None:
            print(f"No company with domain {domain}")
            return 1
        try:
            c = add_contact(s, company, name, email, title, kind)
        except ValueError as e:
            print(e)
            return 1
        print(f"Added {c.full_name} at {company.name}; the next bench fill can use them.")
    return 0


def sweep_cmd() -> int:
    from app.modules.m3_outreach.pipeline import sweep

    print("Re-checking people on benches (are they still there?)…")
    _print_counts(sweep())
    return 0


def lookups_cmd() -> int:
    """Which contact services are set up, and how much of each free tier is used."""
    from app.config_loader import get_config
    from app.core.quota import used_this_month, used_today
    from app.core.search import search_available
    from app.modules.m3_outreach.verify import smtp_blocked

    rules = get_config().outreach.contacts
    st = get_settings()
    e, v = rules.enrichment, rules.verification

    def line(name, on, used, cap, period):
        state = f"{used}/{cap} this {period}" if on else "not set up (optional)"
        print(f"  {name:<28} {state}")

    print("Contact services:")
    line("Brave search (people)", search_available(), used_this_month("brave:contacts"), rules.searches_per_month, "month")
    line("Hunter email finder", bool(st.hunter_api_key), used_this_month("hunter:finder"), e.hunter_finder_per_month, "month")
    line("Hunter domain pattern", bool(st.hunter_api_key), used_this_month("hunter:domain"), e.hunter_domain_search_per_month, "month")
    line("Hunter verifier", bool(st.hunter_api_key), used_this_month("hunter:verifier"), v.hunter_verifier_per_month, "month")
    line("Apollo person match", bool(st.apollo_api_key), used_this_month("apollo:match"), e.apollo_per_month, "month")
    line("MyEmailVerifier", bool(st.myemailverifier_api_key), used_today("myemailverifier"), v.myemailverifier_per_day, "day")
    if not v.smtp_handshake:
        smtp = "off in config"
    elif smtp_blocked():
        smtp = "blocked on this network (port 25) — using the services above instead"
    else:
        smtp = "on (tried first)"
    print(f"  {'Local mailbox check':<28} {smtp}")
    return 0


# --- Module 3 Part 3b: drafts ------------------------------------------------------------------

def drafts_run_cmd(cluster_id: int | None, force: bool) -> int:
    from app.db import session_scope
    from app.modules.m3_outreach.campaigns import prepare_campaigns
    from app.modules.m3_outreach.drafting import run_drafts

    print("Writing outreach drafts (nothing is sent)…")
    ids = [cluster_id] if cluster_id else None
    _print_counts(run_drafts(cluster_ids=ids, force=force))
    with session_scope() as s:
        counts = prepare_campaigns(s, ids)
    if counts["prepared"] or counts["refreshed"]:
        print(f"  ready for your review: {counts['prepared'] + counts['refreshed']} campaign(s) — see: python -m app.cli review")
    return 0


def draft_cmd(cluster_id: int) -> int:
    from sqlalchemy import select

    from app.db import session_scope
    from app.models import Bench, OutreachDraft, RoleChannel
    from app.modules.m3_outreach.bench import usable_members
    from app.modules.m3_outreach.drafting import attachment, render_for
    from app.modules.m3_outreach.template import load_template

    t = load_template()
    with session_scope() as s:
        d = s.scalar(select(OutreachDraft).where(OutreachDraft.cluster_id == cluster_id))
        if d is None:
            print(f"No draft yet for role {cluster_id} — run: python -m app.cli drafts --cluster {cluster_id}")
            return 1
        ch = s.get(RoleChannel, cluster_id)
        if ch and ch.mode == "bench" and ch.bench_id:
            recipients = [(c.full_name, a.email, a.confidence) for _, c, a in usable_members(s, s.get(Bench, ch.bench_id))]
        elif ch and ch.designated_email:
            recipients = [(None, ch.designated_email, ch.mode)]
        else:
            recipients = []
        first = recipients[0] if recipients else (None, "(no recipient yet)", "")
        subject, body = render_for(d, t, first[0])
        status = "READY" if d.status == "ready" else "FLAGGED — read carefully before approving"
        print(f"Draft v{d.version} for role {cluster_id} · {d.scenario} · {status}")
        truth = d.truth or {}
        last = (truth.get("rounds") or [{}])[-1]
        print(f"Truth check: {last.get('claims', 0)} claims checked, {len(truth.get('rounds', [])) - 1} rewrite(s), "
              f"{len(truth.get('cut', []))} sentence(s) cut")
        for sentence in truth.get("cut", []):
            print(f"  cut: “{sentence}”")
        print("-" * 72)
        print(f"To:      {first[1]}")
        print(f"Subject: {subject}")
        att = attachment()
        if d.attach_resume:
            print(f"Attach:  {att[0]} ({att[1] // 1024} KB)" if att else "Attach:  resume missing — add config/resume.pdf")
        print("-" * 72)
        print(body)
        print("-" * 72)
        if len(recipients) > 1:
            print("Same email to the others on the bench (only the greeting changes):")
            for name, email, conf in recipients[1:]:
                print(f"  {render_for(d, t, name)[1].splitlines()[0]:<24} {email} ({conf})")
    return 0


# --- Module 3 Part 3c: Gmail, review, sending -------------------------------------------------

def gmail_cmd(action: str) -> int:
    from app.db import session_scope
    from app.modules.m3_outreach import gmail

    with session_scope() as s:
        acct = gmail.account(s)
        if action == "connect":
            if not gmail.configured():
                print("First add GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET and APP_SECRET_KEY to .env "
                      "(README: Connect Gmail), then run: docker compose up -d")
                return 1
            print("Open this in your browser (on the laptop running the agent) and sign in with your Gmail:")
            print(f"  {get_settings().public_base_url.rstrip('/')}/api/gmail/connect")
            print("Tick both permissions (send, and read). Then run: python -m app.cli gmail status")
            return 0
        if action == "status":
            if acct is None:
                print("Gmail: not connected" + ("" if gmail.configured() else " (credentials not in .env yet)"))
                return 1
            print(f"Gmail: {acct.email} — {'connected' if acct.status == 'ok' else 'needs reconnecting'}")
            return 0 if acct.status == "ok" else 1
        if action == "test":
            if acct is None:
                print("Gmail isn't connected yet.")
                return 1
            msg = gmail.build_message(sender=acct.email, sender_name=None, to=acct.email,
                                      subject="Job Agent test", body="Sending works. This went from you to you.",
                                      attachment=None)
            message_id, _ = gmail.send(s, msg)
            print(f"Test email sent to yourself ({acct.email}); check your inbox. id {message_id}")
            return 0
    return 1


def _campaign_or_exit(s, campaign_id: int):
    from app.models import OutreachCampaign

    c = s.get(OutreachCampaign, campaign_id)
    if c is None:
        raise SystemExit(f"No campaign {campaign_id} (see: python -m app.cli review)")
    return c


def review_cmd(action: str, campaign_id: int | None, email_pos: int | None, all_: bool, allow_low: bool) -> int:
    from pathlib import Path

    from sqlalchemy import select

    from app.db import session_scope
    from app.models import Company, OutreachCampaign
    from app.modules.m3_outreach import campaigns as C
    from app.modules.m3_outreach.drafting import attachment
    from app.modules.m3_outreach.template import load_template

    with session_scope() as s:
        if action == "list":
            rows = s.scalars(select(OutreachCampaign).where(OutreachCampaign.status.in_(C.ACTIVE))
                             .order_by(OutreachCampaign.id)).all()
            if not rows:
                print("No campaigns waiting or running.")
            for c in rows:
                es = C.emails(s, c)
                pending = sum(e.decision == "pending" for e in es)
                print(f"[{c.id}] {s.get(Company, c.company_id).name} · role {c.cluster_id} · {c.status.upper()}"
                      + (f" · {pending} to review" if pending else "")
                      + f" · {sum(e.status == 'sent' for e in es)}/{sum(e.decision == 'approved' for e in es)} sent"
                      + (f" · {c.status_reason}" if c.status_reason else ""))
            return 0

        c = _campaign_or_exit(s, campaign_id)
        t = load_template()
        es = C.emails(s, c)
        if action == "show":
            print(f"Campaign {c.id} · {s.get(Company, c.company_id).name} · role {c.cluster_id} · {c.status.upper()}"
                  + (" · edited by you" if c.edited else ""))
            att = attachment()
            for e in es:
                subject, body = C.render_email(t, c, e)
                flag = " · NEEDS YOUR GO-AHEAD (--allow-low)" if C.needs_go_ahead(e) and e.decision == "pending" else ""
                print("=" * 72)
                print(f"Email {e.position} of {len(es)} · day {e.wave + 1} · {e.decision.upper()} · {e.status}{flag}")
                print(f"To:      {e.to_name + ' <' + e.to_email + '>' if e.to_name else e.to_email}"
                      f"   (address: {e.confidence}{', provider vouches' if e.vouched else ''})")
                print(f"Subject: {subject}")
                if c.attach_resume:
                    print(f"Attach:  {att[0]}" if att else "Attach:  resume missing — add config/resume.pdf")
                print("-" * 72)
                print(body)
            print("=" * 72)
            print("Next: review approve|skip <campaign> --email N (or --all); review export <campaign> to edit.")
            return 0
        if action in ("approve", "skip"):
            targets = es if all_ else [e for e in es if e.position == email_pos]
            if not targets:
                print("Say which email: --email N, or --all")
                return 1
            for e in targets:
                if e.decision != "pending":
                    continue
                try:
                    C.decide(s, c, e, action, allow_low=allow_low)
                    print(f"  {action}d: {e.to_email}")
                except C.ReviewError as err:
                    print(f"  not {action}d: {err}")
            print(f"Campaign {c.id}: {c.status}" + (" — the waves start now (within sending hours and limits)"
                                                   if c.status == "scheduled" else ""))
            return 0
        if action == "redraft":
            from app.core.llm import LLM

            if not LLM.available():
                print("Needs ANTHROPIC_API_KEY in .env")
                return 1
            try:
                C.redraft(s, c, LLM())
            except C.ReviewError as err:
                print(f"Not redrafted: {err}")
                return 1
            print(f"Campaign {c.id}: a fresh draft replaced the text (your edits stay saved as style examples). "
                  "Approvals were reset: review show again.")
            return 0
        if action == "export":
            path = Path("/review") / f"campaign-{c.id}.txt"
            path.write_text(C.export_text(c), encoding="utf-8")
            print(f"Written to review/campaign-{c.id}.txt in your project folder. Edit it, save, then run:")
            print(f"  python -m app.cli review import {c.id}")
            return 0
        if action == "import":
            path = Path("/review") / f"campaign-{c.id}.txt"
            if not path.is_file():
                print(f"No review/campaign-{c.id}.txt — run: python -m app.cli review export {c.id}")
                return 1
            try:
                subject, body = C.import_text(path.read_text(encoding="utf-8"))
                C.edit(s, c, subject, body)
            except C.ReviewError as err:
                print(f"Not saved: {err}")
                return 1
            print(f"Saved your edit to campaign {c.id} (kept as a style example for future drafts). "
                  "Approvals were reset: review it again with `review show`.")
            return 0
    return 1


def outreach_cmd(action: str, domain: str | None) -> int:
    from datetime import datetime, timezone

    from sqlalchemy import select

    from app.config_loader import get_config
    from app.core import audit
    from app.core.redis_client import get_redis
    from app.db import session_scope
    from app.models import Company
    from app.modules.m3_outreach import campaigns as C
    from app.modules.m3_outreach import sender as S

    with session_scope() as s:
        if action == "status":
            rules = get_config().outreach.sending
            now = datetime.now(timezone.utc)
            b = S.breaker(s)
            nxt = get_redis().get(S.NEXT_SEND_KEY)
            print(f"Sending: {'PAUSED by you' if S.paused() else 'on'} · bounce breaker: {b.state}"
                  + (f" ({b.reason})" if b.reason else ""))
            print(f"Today: {S._sent_since(s, S.start_of_day_utc(now))}/{rules.global_per_day} sent"
                  + (f" · next send not before {datetime.fromisoformat(nxt):%H:%M} UTC" if nxt and datetime.fromisoformat(nxt) > now else ""))
            due = S.next_candidates(s, now)
            print(f"Approved and due now: {len(due)} email(s)")
            return 0
        if action in ("pause", "unpause"):
            S.set_paused(action == "pause")
            print("All sending paused." if action == "pause" else "Sending back on (limits and quiet hours still apply).")
            return 0
        if action == "reset-breaker":
            b = S.breaker(s)
            b.state, b.reason, b.reset_at = "ok", None, datetime.now(timezone.utc)
            audit.record(s, "m3", "breaker_reset", "You reset the bounce breaker")
            print("Bounce breaker reset; sending can continue.")
            return 0
        if not domain:
            print("Give the company's domain, e.g. lilly.com")
            return 1
        company = s.scalar(select(Company).where(Company.domain == domain.lower()))
        if company is None:
            print(f"No company with domain {domain}")
            return 1
        if action == "hold":
            print(f"{company.name}: {C.stop_company(s, company.id, 'held by you', pause=True)} unsent email(s) held.")
        elif action == "release":
            print(f"{company.name}: {C.resume_company(s, company.id)} campaign(s) released.")
        elif action == "stop":
            print(f"{company.name}: {C.stop_company(s, company.id, 'stopped by you', outcome='stopped')} unsent email(s) cancelled.")
        return 0


# --- Module 3 Part 3d: replies and notifications ------------------------------------------------

def replies_cmd(last: int) -> int:
    from sqlalchemy import select

    from app.db import session_scope
    from app.models import Company, OutreachCampaign, OutreachEmail
    from app.modules.m3_outreach.gmail import thread_link

    with session_scope() as s:
        rows = s.execute(select(OutreachEmail, OutreachCampaign).join(
            OutreachCampaign, OutreachCampaign.id == OutreachEmail.campaign_id)
            .where(OutreachEmail.reply_category.is_not(None)).order_by(OutreachEmail.id.desc()).limit(last)).all()
        if not rows:
            print("No replies yet.")
        for e, c in rows:
            print(f"[{c.id}] {s.get(Company, c.company_id).name}: {e.reply_category.upper()} from {e.reply_from}")
            print(f"     {e.reply_summary}")
            print(f"     {thread_link(e.gmail_thread_id)}")
    return 0


def pings_cmd(all_: bool, mark_read: bool) -> int:
    from datetime import datetime, timezone

    from sqlalchemy import select

    from app.db import session_scope
    from app.models import Notification

    with session_scope() as s:
        q = select(Notification).order_by(Notification.id.desc()).limit(50)
        if not all_:
            q = q.where(Notification.read_at.is_(None))
        rows = s.scalars(q).all()
        if not rows:
            print("Nothing new.")
        for n in rows:
            tag = {"ping": "PING", "needs_you": "NEEDS YOU", "update": "update"}.get(n.kind, n.kind)
            print(f"{n.created_at:%d %b %H:%M} [{tag}] {n.title}")
            if n.detail:
                print(f"     {n.detail}")
            if n.link:
                print(f"     {n.link}")
            if mark_read and n.read_at is None:
                n.read_at = datetime.now(timezone.utc)
        if mark_read and rows:
            print(f"({len(rows)} marked as read)")
    return 0


# --- Module 4: apply tracker ------------------------------------------------------------------

def apply_cmd() -> int:
    from app.config_loader import get_config
    from app.db import session_scope
    from app.modules.m4_apply.tracker import apply_list

    with session_scope() as s:
        full, folded = apply_list(s)
        if not full and not folded:
            print("Nothing on your apply list. (Fast-lane roles and roles you mark `act` land here.)")
        for app, cluster, company in full:
            print(f"[{cluster.id}] {company.name}: {cluster.title}  (on your list since {app.queued_at:%d %b})")
            for link in app.links or []:
                state = {True: "live", False: f"BROKEN — {link.get('why')}", None: "not checked yet"}.get(link.get("ok"))
                print(f"     {link.get('req_id') or ''} {link['url']}  [{state}]" + (" (job-site lead)" if link.get("lead") else ""))
            if not app.links:
                print("     no apply link found yet")
        if folded:
            print(f"…and still pending: {folded} older role(s) (over {get_config().apply.full_card_days} days).")
        print("After applying: python -m app.cli applied <role id>   ·   not interested: apply-skip <role id> \"<reason>\"")
    return 0


def applied_cmd(cluster_id: int, note: str | None) -> int:
    from app.db import session_scope
    from app.modules.m4_apply.tracker import mark_applied

    with session_scope() as s:
        try:
            app = mark_applied(s, cluster_id, note)
        except ValueError as e:
            print(e)
            return 1
        print(f"Role {cluster_id} marked applied ({app.applied_at:%d %b %H:%M}). Status emails will be tracked from here.")
    return 0


def apply_skip_cmd(cluster_id: int, reason: str) -> int:
    from app.db import session_scope
    from app.modules.m4_apply.tracker import skip

    with session_scope() as s:
        try:
            skip(s, cluster_id, reason)
        except ValueError as e:
            print(e)
            return 1
        print(f"Role {cluster_id} taken off your apply list.")
    return 0


def applications_cmd(status: str | None) -> int:
    from sqlalchemy import select

    from app.db import session_scope
    from app.models import Application, Company, JobCluster

    with session_scope() as s:
        q = select(Application, JobCluster, Company).join(JobCluster, JobCluster.id == Application.cluster_id) \
            .join(Company, Company.id == Application.company_id).order_by(Application.status_at.desc())
        if status:
            q = q.where(Application.status == status)
        rows = s.execute(q.limit(100)).all()
        if not rows:
            print("No applications tracked yet.")
        for app, cluster, company in rows:
            extra = f" · applied {app.applied_at:%d %b} ({app.applied_how})" if app.applied_at else ""
            tag = " (outside application)" if app.outside else ""
            print(f"[{cluster.id}] {app.status.upper():<12} {company.name}: {cluster.title}{tag}{extra}")
            if app.receipt_link:
                print(f"     receipt: {app.receipt_link}")
    return 0


def story_cmd(cluster_id: int) -> int:
    from app.db import session_scope
    from app.models import Company, JobCluster
    from app.modules.m4_apply.tracker import story

    with session_scope() as s:
        cluster = s.get(JobCluster, cluster_id)
        if cluster is None:
            print(f"No role {cluster_id}")
            return 1
        print(f"{s.get(Company, cluster.company_id).name}: {cluster.title}")
        events = story(s, cluster_id)
        if not events:
            print("  nothing yet")
        for at, kind, text in events:
            print(f"  {at:%d %b %H:%M}  {'✉' if kind == 'outreach' else '•'} {text}")
    return 0


def track_cmd() -> int:
    from app.modules.m4_apply.tracker import run_tracking

    print("Updating your apply list, checking links, reading hiring-system emails…")
    _print_counts(run_tracking())
    return 0


def assign_cmd(no: int, cluster_id: int | None, other: str | None, ignore: bool) -> int:
    from app.db import session_scope
    from app.modules.m4_apply.inbox import AssignError, assign

    with session_scope() as s:
        try:
            outcome = assign(s, no, cluster_id=cluster_id, other_title=other, ignore=ignore)
        except AssignError as e:
            print(e)
            return 1
        print("Ignored." if outcome == "ignored" else
              f"Done: the email's news was recorded on {'role ' + str(cluster_id) if cluster_id else repr(other)}.")
    return 0


# --- Module 5a: your profile ------------------------------------------------------------------

def _profile_confirm() -> int:
    from app.db import session_scope
    from app.modules.m5_profile import profile as P
    from app.modules.m5_profile import store

    if not P.REVIEW_FILE.is_file():
        print("No review/profile.yaml yet — run: python -m app.cli profile read")
        return 1
    text = P.REVIEW_FILE.read_text(encoding="utf-8")
    with session_scope() as s:
        draft = store.latest_draft(s)
        if draft is None:
            print("No draft waiting — run: profile read (new resume) or profile edit (change an answer)")
            return 1
        if P.draft_id_in(text) != draft.id:
            print("This file is older than the agent's latest draft — run: profile export, check it, then confirm")
            return 1
        try:
            row = P.confirm(s, P.parse_review(text), row=draft)
        except P.ProfileError as e:
            print(f"Not confirmed: {e}")
            return 1
        print(f"Confirmed. Every module now uses this profile (version {row.id}).")
        print("Your pay and notice period are marked as confirmed today.")
    return 0


def profile_cmd(action: str) -> int:
    from app.core.llm import LLM
    from app.db import session_scope
    from app.modules.m5_profile import profile as P
    from app.modules.m5_profile import store

    if action == "confirm":
        code = _profile_confirm()
        store.invalidate()  # after the commit: this process sees the new profile at once
        return code
    with session_scope() as s:
        if action in ("read", "export"):
            draft = store.latest_draft(s)
            fresh = draft is not None and draft.resume_hash == P.file_hash(P.resume_path())
            if action == "export" or fresh:
                if draft is None:
                    print("No draft waiting — run: python -m app.cli profile read")
                    return 1
                row = draft                       # the daily check already read this resume: no second AI read
            else:
                if not LLM.available():
                    print("Needs ANTHROPIC_API_KEY in .env")
                    return 1
                try:
                    row = P.make_draft(s, LLM())
                except ValueError as e:
                    print(e)
                    return 1
            P.export(row)
            print("Check review/profile.yaml in your project folder (Notepad is fine), fix anything,")
            print("save it, then run: python -m app.cli profile confirm")
            if (row.notes or {}).get("gaps"):
                print("Still to fill in: " + "; ".join(g.split(":")[0] for g in row.notes["gaps"]))
            return 0
        if action == "edit":
            try:
                row = P.edit_current(s)
            except P.ProfileError as e:
                print(e)
                return 1
            P.export(row)
            print("Your current profile is in review/profile.yaml. Change what you like, save, then: profile confirm")
            return 0
        if action == "show":
            row = store.load_confirmed(s)
            draft = store.latest_draft(s)
            if row is None:
                print("No confirmed profile yet — the agent is using config/owner_stub.yaml.")
                print("Start with: python -m app.cli profile read")
            else:
                d = row.data
                cr = d.get("current_role") or {}
                years = store.experience_years(d.get("employment_history")) or d.get("total_experience_years")
                print(f"{d.get('name')} · {cr.get('title')} at {cr.get('company')} since {cr.get('started')}"
                      f" · {years} years (from your job dates) · confirmed {row.confirmed_at:%d %b %Y}")
                for f in P.TIME_SENSITIVE:
                    when = (row.confirmed_fields or {}).get(f)
                    stale = " — PLEASE RE-CONFIRM (profile edit)" if f in P.stale_fields(row) else ""
                    print(f"  {f}: last confirmed {when or 'never'}{stale}")
            if draft is not None:
                print(f"A draft is waiting for you (from {draft.created_at:%d %b}): check review/profile.yaml, then profile confirm")
            return 0
    return 1


def direction_cmd(action: str, suggestion_id: int | None) -> int:
    from app.core.llm import LLM
    from app.db import session_scope
    from app.modules.m5_profile import direction as D
    from app.modules.m5_profile import store

    with session_scope() as s:
        if action == "refresh":
            if not LLM.available():
                print("Needs ANTHROPIC_API_KEY in .env")
                return 1
            print(D.refresh(s, LLM()))
        elif action in ("yes", "no"):
            if suggestion_id is None:
                print("Say which suggestion: direction yes <id>")
                return 1
            try:
                sug = D.answer(s, suggestion_id, action == "yes")
            except D.DirectionError as e:
                print(e)
                return 1
            print(("Done: " if action == "yes" else "Noted, not changed: ") + D.describe(sug))
        elif action == "edit":
            try:
                D.export(s)
            except D.DirectionError as e:
                print(e)
                return 1
            print("Your lists are in review/direction.yaml. Edit, save, then: direction confirm")
            return 0
        elif action == "confirm":
            if not D.REVIEW_FILE.is_file():
                print("No review/direction.yaml yet — run: direction edit")
                return 1
            try:
                D.confirm_edit(s, D.REVIEW_FILE.read_text(encoding="utf-8"))
            except D.DirectionError as e:
                print(f"Not saved: {e}")
                return 1
            print("Saved. The agent looks for jobs with your new lists from the next round.")
    if action in ("show", "refresh", "yes", "no", "confirm"):
        store.invalidate()
        with session_scope() as s:
            info = D.explained(s)
        if not info:
            print("No confirmed profile yet — run: profile read, then profile confirm")
            return 1
        if action != "show":
            return 0
        if info.get("position"):
            print(f"Where you are: {info['position']}")
            print(f"Heading: {info['trajectory']}")
        else:
            print("The agent hasn't worked out its view yet — run: direction refresh (about 3 cents)")
        for lst, label in (("progression", "Step up"), ("lateral", "Sideways"), ("excluded", "Excluded")):
            print(f"{label}:")
            for f in info[lst] or [{"family": "—", "why": "", "condition": ""}]:
                extra = f" [{f['condition']}]" if f.get("condition") else ""
                print(f"  - {f['family']}{extra}" + (f" — {f['why']}" if f["why"] else ""))
        print("Target roles (searched on job sites):")
        for t in info["target_roles"] or [{"title": "—", "why": ""}]:
            print(f"  - {t['title']}" + (f" — {t['why']}" if t["why"] else ""))
        print(f"Sideways appetite: {info['lateral_appetite']} · track: {info['track']}"
              + (" (open to managing)" if info["open_to_management_track"] else ""))
        if info["suggestions"]:
            print("Waiting for your yes / no:")
            for sug in info["suggestions"]:
                print(f"  [{sug['id']}] {sug['text']}")
            print("Answer: direction yes <id>  ·  direction no <id>")
    return 0


def memory_cmd(action: str, arg: str | None, kind: str | None, include_archived: bool) -> int:
    from app.core.llm import LLM
    from app.db import session_scope
    from app.modules.m5_profile import memory as M

    with session_scope() as s:
        if action == "believes":
            if not LLM.available():
                print("Needs ANTHROPIC_API_KEY in .env")
                return 1
            topics = M.beliefs(s, LLM()).topics
            if not topics:
                print("The agent hasn't learned anything about you yet.")
            for t in topics:
                print(t.topic)
                for line in t.statements:
                    print(f"  - {line}")
            return 0
        if action == "list":
            rows = M.listing(s, kind=kind, include_archived=include_archived)
            if not rows:
                print("No memories yet.")
            for e in rows:
                flags = " · PINNED" if e.pinned else ""
                flags += " · needs re-confirming" if e.status == "needs_reconfirm" else ""
                flags += " · archived" if e.status == "archived" else ""
                flags += " · MAY CLASH with your new resume" if (e.content or {}).get("clash") else ""
                print(f"[{e.id}] ({e.kind}) {e.meaning}{flags}")
            return 0
        if action == "search":
            if not arg:
                print("What to search for? e.g. memory search \"too much travel\"")
                return 1
            for e, sim in M.search_text(s, arg):
                print(f"[{e.id}] ({e.kind}, {sim:.0%} similar) {e.meaning}")
            return 0
        if action == "undo":
            if not arg:
                print("Which one? The code is in the note, e.g. memory undo a1b2c3")
                return 1
            n = M.undo_set_aside(s, arg.strip())
            print(f"Undone: {n} earlier skip(s) are used as examples again." if n else "Nothing to undo with that code.")
            return 0
        if not arg or not arg.isdigit():
            print(f"Which memory? e.g. memory {action} 12")
            return 1
        try:
            e = M.act(s, int(arg), action)
        except M.MemoryError as err:
            print(err)
            return 1
        print({"pin": "Pinned (never archived)", "unpin": "Unpinned", "forget": "Forgotten (archived; `memory restore`"
               " brings it back)", "restore": "Restored", "keep": "Kept as it is"}[action] + f": {e.meaning[:100]}")
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
    sub.add_parser("coverage", help="how kept companies' jobs are read; which reader to build next")
    p_pipe = sub.add_parser("pipeline", help="Module 2: process job postings now")
    p_pipe.add_argument("--refilter", action="store_true", help="re-apply filters to all open postings first")
    sub.add_parser("funnel", help="Module 2: where postings stand and why they were filtered out")
    p_pass = sub.add_parser("passed", help="Module 2: postings that passed the free filters")
    p_pass.add_argument("--domain")
    p_pass.add_argument("--limit", type=int, default=40)
    p_assess = sub.add_parser("assess", help="Module 2: AI-read job descriptions now (daily limit applies)")
    p_assess.add_argument("--limit", type=int)
    p_assess.add_argument("--force", action="store_true", help="ignore the daily limit")
    p_assd = sub.add_parser("assessed", help="Module 2: AI-read roles with reasons and flags")
    g = p_assd.add_mutually_exclusive_group()
    g.add_argument("--passed", action="store_true")
    g.add_argument("--rejected", action="store_true")
    p_assd.add_argument("--domain")
    p_assd.add_argument("--last", type=int, default=20)
    p_brand = sub.add_parser("brand", help="record your own view of a company's brand vs your career")
    p_brand.add_argument("domain")
    p_brand.add_argument("level", choices=["up", "lateral", "down"])
    p_brand.add_argument("reason")
    p_score = sub.add_parser("score", help="Module 2: similarity + AI grades now (daily limit applies)")
    p_score.add_argument("--limit", type=int)
    p_score.add_argument("--force", action="store_true")
    p_short = sub.add_parser("shortlist", help="Module 2: scored roles, best first")
    p_short.add_argument("--band", choices=["fast_lane", "inbox", "maybe", "archive"])
    p_short.add_argument("--limit", type=int, default=25)
    p_fb = sub.add_parser("feedback", help="Module 2: act / skip / thumbs_up / thumbs_down / maybe_rescue a role")
    p_fb.add_argument("cluster_id", type=int)
    p_fb.add_argument("action", choices=["act", "skip", "thumbs_up", "thumbs_down", "maybe_rescue"])
    p_fb.add_argument("reason", nargs="?")
    p_fb.add_argument("--why", action="append", choices=["too_junior", "too_senior", "not_my_field", "location", "company",
                                                         "pay", "hours_travel", "other"],
                      help="one-tap reason(s) for a skip; repeat for several")
    p_rates = sub.add_parser("rates", help="Module 2: pay-conversion factors (PPP + exchange rates)")
    p_rates.add_argument("--refresh", action="store_true", help="fetch the latest from the World Bank")
    p_ct = sub.add_parser("contacts", help="Module 3: find contacts for pursued roles now (sends nothing)")
    p_ct.add_argument("--cluster", type=int, help="one role (any band), e.g. from `shortlist`")
    p_ct.add_argument("--limit", type=int)
    p_ch = sub.add_parser("channel", help="Module 3: how outreach for one role would reach the company")
    p_ch.add_argument("cluster_id", type=int)
    p_bn = sub.add_parser("bench", help="Module 3: the people on a company's benches")
    p_bn.add_argument("domain")
    p_dg = sub.add_parser("designate", help="Module 3: make one address the only channel for a role")
    p_dg.add_argument("cluster_id", type=int)
    p_dg.add_argument("email", help="the address, or 'auto' to go back to automatic")
    p_dg.add_argument("reason")
    p_ac = sub.add_parser("add-contact", help="Module 3: add a person you know at a company")
    p_ac.add_argument("domain")
    p_ac.add_argument("name")
    p_ac.add_argument("--email")
    p_ac.add_argument("--title")
    p_ac.add_argument("--kind", choices=["founder", "recruiting", "hiring_manager"], default="recruiting")
    sub.add_parser("sweep", help="Module 3: re-check people on benches now")
    sub.add_parser("lookups", help="Module 3: which contact services are set up, and their free-tier use")
    p_dr = sub.add_parser("drafts", help="Module 3: write outreach drafts now (sends nothing)")
    p_dr.add_argument("--cluster", type=int, help="one role")
    p_dr.add_argument("--force", action="store_true", help="rewrite even if nothing changed")
    p_d1 = sub.add_parser("draft", help="Module 3: show a role's draft exactly as recipients would get it")
    p_d1.add_argument("cluster_id", type=int)
    p_gm = sub.add_parser("gmail", help="Module 3: connect your Gmail (Google sign-in), check it, send yourself a test")
    p_gm.add_argument("action", choices=["connect", "status", "test"])
    p_rv = sub.add_parser("review", help="Module 3: review outreach emails: approve / edit / skip each one")
    p_rv.add_argument("action", nargs="?", default="list", choices=["list", "show", "approve", "skip", "export", "import", "redraft"])
    p_rv.add_argument("campaign", nargs="?", type=int)
    p_rv.add_argument("--email", type=int, help="which email in the campaign (1, 2, ...)")
    p_rv.add_argument("--all", action="store_true", help="every email still waiting in the campaign")
    p_rv.add_argument("--allow-low", action="store_true", help="your go-ahead for addresses that couldn't be proven")
    p_ou = sub.add_parser("outreach", help="Module 3: sending status; pause / unpause; hold / release / stop a company")
    p_ou.add_argument("action", choices=["status", "pause", "unpause", "hold", "release", "stop", "reset-breaker"])
    p_ou.add_argument("domain", nargs="?")
    p_rp = sub.add_parser("replies", help="Module 3: replies to your outreach and how each was sorted")
    p_rp.add_argument("--last", type=int, default=20)
    p_pg = sub.add_parser("pings", help="what the agent wants you to know (unread first)")
    p_pg.add_argument("--all", action="store_true", help="include ones already read")
    p_pg.add_argument("--read", action="store_true", help="mark the shown ones as read")
    sub.add_parser("apply", help="Module 4: your apply list (links to apply to, live-checked)")
    p_ad = sub.add_parser("applied", help="Module 4: mark a role as applied (for systems that send no confirmation)")
    p_ad.add_argument("cluster_id", type=int)
    p_ad.add_argument("note", nargs="?")
    p_as = sub.add_parser("apply-skip", help="Module 4: take a role off your apply list")
    p_as.add_argument("cluster_id", type=int)
    p_as.add_argument("reason")
    p_ap = sub.add_parser("applications", help="Module 4: where each application stands")
    p_ap.add_argument("--status", choices=["queued", "applied", "confirmed", "in_review", "interviewing", "offer",
                                         "rejected", "silent", "skipped", "closed"])
    p_st = sub.add_parser("story", help="one role's whole story: outreach + application")
    p_st.add_argument("cluster_id", type=int)
    sub.add_parser("track", help="Module 4: update the apply list, links and hiring-system emails now")
    p_sg = sub.add_parser("assign", help="Module 4: answer 'which role is this email about?' (number from `pings`)")
    p_sg.add_argument("no", type=int, help="the email's number, shown in the question")
    p_sg.add_argument("cluster_id", type=int, nargs="?", help="the role it's about")
    p_sg.add_argument("--other", help="a job not in the list (tracked as an outside application)")
    p_sg.add_argument("--ignore", action="store_true", help="not about any of your applications")
    p_pf = sub.add_parser("profile", help="Module 5: your profile, read from your resume and confirmed by you")
    p_pf.add_argument("action", choices=["read", "export", "confirm", "show", "edit"])
    p_dir = sub.add_parser("direction", help="Module 5: your career direction — families, target roles, suggestions")
    p_dir.add_argument("action", nargs="?", default="show", choices=["show", "refresh", "yes", "no", "edit", "confirm"])
    p_dir.add_argument("suggestion_id", nargs="?", type=int)
    p_emp = sub.add_parser("employers", help="your current / past employers — never watched or contacted")
    p_emp.add_argument("action", nargs="?", default="list", choices=["list", "fix"])
    p_emp.add_argument("name", nargs="?", help="fix: the employer's name as in your profile")
    p_emp.add_argument("domain", nargs="?", help="fix: its website, e.g. zs.com — or none")
    p_mem = sub.add_parser("memory", help="Module 5: what the agent remembers about you — and your control over it")
    p_mem.add_argument("action", nargs="?", default="believes",
                       choices=["believes", "list", "search", "pin", "unpin", "forget", "restore", "keep", "undo"])
    p_mem.add_argument("arg", nargs="*", help="a memory number, or words to search for")
    p_mem.add_argument("--kind", choices=["answer", "precedent", "taste_signal"])
    p_mem.add_argument("--all", action="store_true", help="include archived memories")
    p_wd = sub.add_parser("probe-workday", help="diagnose a Workday board (tenant + site)")
    p_wd.add_argument("tenant")
    p_wd.add_argument("site")
    p_wd.add_argument("--server", type=int, help="test one server number only, e.g. 115")
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
    if args.command == "coverage":
        sys.exit(coverage_cmd())
    if args.command == "pipeline":
        sys.exit(pipeline_cmd(args.refilter))
    if args.command == "funnel":
        sys.exit(funnel_cmd())
    if args.command == "passed":
        sys.exit(passed_cmd(args.domain, args.limit))
    if args.command == "assess":
        sys.exit(assess_cmd(args.limit, args.force))
    if args.command == "assessed":
        sys.exit(assessed_cmd("passed" if args.passed else "rejected" if args.rejected else None, args.domain, args.last))
    if args.command == "rates":
        sys.exit(rates_cmd(args.refresh))
    if args.command == "brand":
        sys.exit(brand_cmd(args.domain, args.level, args.reason))
    if args.command == "score":
        sys.exit(score_cmd(args.limit, args.force))
    if args.command == "shortlist":
        sys.exit(shortlist_cmd(args.band, args.limit))
    if args.command == "feedback":
        sys.exit(feedback_cmd(args.cluster_id, args.action, args.reason, args.why))
    if args.command == "contacts":
        sys.exit(contacts_cmd(args.cluster, args.limit))
    if args.command == "channel":
        sys.exit(channel_cmd(args.cluster_id))
    if args.command == "bench":
        sys.exit(bench_cmd(args.domain))
    if args.command == "designate":
        sys.exit(designate_cmd(args.cluster_id, args.email, args.reason))
    if args.command == "add-contact":
        sys.exit(add_contact_cmd(args.domain, args.name, args.email, args.title, args.kind))
    if args.command == "sweep":
        sys.exit(sweep_cmd())
    if args.command == "lookups":
        sys.exit(lookups_cmd())
    if args.command == "drafts":
        sys.exit(drafts_run_cmd(args.cluster, args.force))
    if args.command == "draft":
        sys.exit(draft_cmd(args.cluster_id))
    if args.command == "gmail":
        sys.exit(gmail_cmd(args.action))
    if args.command == "review":
        if args.action != "list" and args.campaign is None:
            parser.error("review show|approve|skip|export|import|redraft needs a campaign number")
        sys.exit(review_cmd(args.action, args.campaign, args.email, args.all, args.allow_low))
    if args.command == "outreach":
        sys.exit(outreach_cmd(args.action, args.domain))
    if args.command == "replies":
        sys.exit(replies_cmd(args.last))
    if args.command == "pings":
        sys.exit(pings_cmd(args.all, args.read))
    if args.command == "apply":
        sys.exit(apply_cmd())
    if args.command == "applied":
        sys.exit(applied_cmd(args.cluster_id, args.note))
    if args.command == "apply-skip":
        sys.exit(apply_skip_cmd(args.cluster_id, args.reason))
    if args.command == "applications":
        sys.exit(applications_cmd(args.status))
    if args.command == "story":
        sys.exit(story_cmd(args.cluster_id))
    if args.command == "track":
        sys.exit(track_cmd())
    if args.command == "assign":
        sys.exit(assign_cmd(args.no, args.cluster_id, args.other, args.ignore))
    if args.command == "profile":
        sys.exit(profile_cmd(args.action))
    if args.command == "direction":
        sys.exit(direction_cmd(args.action, args.suggestion_id))
    if args.command == "employers":
        sys.exit(employers_cmd(args.action, args.name, args.domain))
    if args.command == "memory":
        sys.exit(memory_cmd(args.action, " ".join(args.arg) or None, args.kind, args.all))
    if args.command == "probe-workday":
        sys.exit(probe_workday_cmd(args.tenant, args.site, args.server))


if __name__ == "__main__":
    main()
