"""Operator commands.

    python -m app.cli selftest     # end-to-end check of the whole stack; exit code 1 on failure
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

    passed = sum(results)
    print(f"\n{passed}/{len(results)} checks passed")
    return 0 if all(results) else 1


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("selftest", help="end-to-end check of the whole stack")
    args = parser.parse_args()

    if args.command == "selftest":
        sys.exit(selftest())


if __name__ == "__main__":
    main()
