"""Part 6e: the Audit log keeps about 12 months in full (spec §6.4). Older entries are condensed to one line per
kind per month ("Mar 2026 · 212 × Judged a company"); entries you corrected stay in full, because your
corrections in Memory point back to them. Monthly, and from the Maintenance screen. Running it twice changes
nothing the second time."""

from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.models import AuditLog

KEEP_DAYS = 365
CONDENSED = "monthly_summary"


def _old(cutoff: datetime):
    return (AuditLog.created_at < cutoff, AuditLog.overridden_at.is_(None), AuditLog.action != CONDENSED)


def compact(session: Session, now: datetime | None = None, keep_days: int = KEEP_DAYS) -> dict:
    from app.web.audit_view import kind_label

    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=keep_days)
    month = func.date_trunc("month", AuditLog.created_at)
    groups = session.execute(
        select(AuditLog.module, AuditLog.action, month.label("m"), func.count(), func.min(AuditLog.created_at))
        .where(*_old(cutoff)).group_by(AuditLog.module, AuditLog.action, month)).all()
    if not groups:
        return {"condensed": 0, "lines": 0}
    lines = 0
    for module, action, m, n, first in groups:
        key = f"{m:%Y-%m}"
        line = session.scalars(select(AuditLog).where(
            AuditLog.action == CONDENSED, AuditLog.module == module,
            AuditLog.detail["of"].astext == action, AuditLog.detail["month"].astext == key)).first()
        if line is None:
            line = AuditLog(module=module, action=CONDENSED, summary="", created_at=first,
                            detail={"of": action, "month": key, "count": 0})
            session.add(line)
            lines += 1
        count = int((line.detail or {}).get("count") or 0) + n
        line.detail = {**(line.detail or {}), "count": count}
        line.summary = f"{m:%b %Y} · {count} × {kind_label(action)}"
    session.flush()
    done = session.execute(delete(AuditLog).where(*_old(cutoff)).execution_options(synchronize_session=False))
    return {"condensed": done.rowcount, "lines": lines}
