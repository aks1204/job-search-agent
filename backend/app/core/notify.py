"""Record something the owner should hear about (delivered by Module 6: pings + summary mail)."""

from sqlalchemy.orm import Session

from app.models.notification import Notification


def notify(session: Session, kind: str, section: str, title: str, *, detail: str | None = None,
           link: str | None = None, subject: tuple[str, str] | None = None) -> Notification:
    n = Notification(kind=kind, section=section, title=title, detail=detail, link=link,
                     subject_type=subject[0] if subject else None, subject_id=subject[1] if subject else None)
    session.add(n)
    return n
