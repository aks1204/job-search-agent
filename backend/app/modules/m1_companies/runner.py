"""Runs one scout: isolated failure, incremental state, a health row per run."""

import copy
import logging
from collections import Counter
from datetime import datetime, timezone

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.config_loader import get_config
from app.core.http import PoliteClient
from app.db import get_session_factory, session_scope
from app.models.scout import ScoutRun, ScoutState
from app.modules.m1_companies.candidate import Candidate
from app.modules.m1_companies.identity import intake
from app.modules.m1_companies.scouts import SCOUTS, ScoutContext
from app.settings import redact

log = logging.getLogger(__name__)

COMMIT_EVERY = 50


def _intake_safely(session: Session, cand: Candidate) -> str:
    """Two scouts can race to create the same company; on a unique-key clash, retry once."""
    for attempt in (1, 2):
        try:
            with session.begin_nested():
                return intake(session, cand)
        except IntegrityError:
            if attempt == 2:
                raise
    raise AssertionError("unreachable")


def _finish(run_id: int, status: str, counts: dict, error: str | None) -> None:
    with session_scope() as s:
        run = s.get(ScoutRun, run_id)
        run.status = status
        run.counts = counts
        run.error = redact(error)[:4000] if error else None
        run.finished_at = datetime.now(timezone.utc)


def run_scout(name: str, *, http: PoliteClient | None = None) -> dict:
    if name not in SCOUTS:
        raise ValueError(f"unknown scout {name!r}; known: {', '.join(SCOUTS)}")
    cfg = get_config()
    scout = SCOUTS[name](cfg)

    with session_scope() as s:
        run = ScoutRun(scout=name, status="running", counts={})
        s.add(run)
        s.flush()
        run_id = run.id

    reason = None if scout.settings.enabled else "disabled in config/scouts.yaml"
    reason = reason or scout.not_ready_reason()
    if reason:
        _finish(run_id, "skipped", {}, reason)
        return {"scout": name, "status": "skipped", "reason": reason}

    counts: Counter = Counter()
    client = http or PoliteClient()
    status, error = "ok", None
    session = get_session_factory()()
    try:
        state_row = session.get(ScoutState, name)
        if state_row is None:
            state_row = ScoutState(scout=name, state={})
            session.add(state_row)
        state = copy.deepcopy(state_row.state)
        ctx = ScoutContext(http=client, config=cfg, state=state)

        def save_state() -> None:
            state_row.state = copy.deepcopy(state)
            flag_modified(state_row, "state")
            session.commit()

        for i, cand in enumerate(scout.discover(ctx), 1):
            counts[_intake_safely(session, cand)] += 1
            if i % COMMIT_EVERY == 0:
                save_state()
        save_state()
    except Exception as e:
        session.rollback()
        log.exception("scout %s failed", name)
        status, error = "failed", f"{type(e).__name__}: {e}"
    finally:
        session.close()
        if http is None:
            client.close()

    _finish(run_id, status, dict(counts), error)
    result = {"scout": name, "status": status, "counts": dict(counts)}
    if error:
        result["error"] = redact(error)
    return result
