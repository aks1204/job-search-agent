from sqlalchemy.orm import Session

from app.models.audit import AuditLog


def record(
    session: Session,
    module: str,
    action: str,
    summary: str,
    *,
    subject_type: str | None = None,
    subject_id: str | None = None,
    detail: dict | None = None,
    is_judgment: bool = False,
    rules_version: str | None = None,
) -> AuditLog:
    entry = AuditLog(
        module=module,
        action=action,
        summary=summary,
        subject_type=subject_type,
        subject_id=subject_id,
        detail=detail or {},
        is_judgment=is_judgment,
        rules_version=rules_version,
    )
    session.add(entry)
    return entry
