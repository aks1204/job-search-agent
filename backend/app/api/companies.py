from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.api.verdicts import verdict_dict
from app.db import get_session
from app.models import AuditLog, Company, CompanySighting, CompanyVerdict, ContactLead
from app.modules.m1_companies.facts import fact_card

router = APIRouter()


def _summary(c: Company, sources: list[str]) -> dict:
    return {
        "id": c.id, "name": c.name, "domain": c.domain, "domain_source": c.domain_source,
        "status": c.status, "type": c.type, "sources": sources, "first_seen_at": c.first_seen_at,
    }


@router.get("/companies")
def list_companies(
    status: str | None = None,
    type: str | None = None,
    source: str | None = Query(None, description="scout name, e.g. yc / hn / adzuna"),
    q: str | None = Query(None, description="name or domain contains"),
    limit: int = Query(50, le=500),
    offset: int = 0,
    session: Session = Depends(get_session),
) -> dict:
    sources = (
        select(CompanySighting.company_id, func.array_agg(CompanySighting.scout.distinct()).label("sources"))
        .group_by(CompanySighting.company_id)
        .subquery()
    )
    stmt = select(Company, sources.c.sources).outerjoin(sources, sources.c.company_id == Company.id)
    if status:
        stmt = stmt.where(Company.status == status)
    if type:
        stmt = stmt.where(Company.type == type)
    if source:
        stmt = stmt.where(
            select(CompanySighting.id)
            .where(CompanySighting.company_id == Company.id, CompanySighting.scout == source)
            .exists()
        )
    if q:
        like = f"%{q.lower()}%"
        stmt = stmt.where(or_(func.lower(Company.name).like(like), Company.domain.like(like)))
    total = session.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = session.execute(stmt.order_by(Company.first_seen_at.desc()).limit(limit).offset(offset)).all()
    return {"total": total, "items": [_summary(c, s or []) for c, s in rows]}


@router.get("/companies/{company_id}")
def get_company(company_id: int, session: Session = Depends(get_session)) -> dict:
    c = session.get(Company, company_id)
    if c is None:
        raise HTTPException(404, "company not found")
    sightings = session.scalars(
        select(CompanySighting).where(CompanySighting.company_id == c.id)
        .order_by(CompanySighting.last_seen_at.desc()).limit(25)
    ).all()
    leads = session.scalars(select(ContactLead).where(ContactLead.company_id == c.id)).all()
    verdicts = session.scalars(
        select(CompanyVerdict).where(CompanyVerdict.company_id == c.id)
        .order_by(CompanyVerdict.created_at.desc()).limit(10)
    ).all()
    log = session.scalars(
        select(AuditLog).where(AuditLog.subject_type == "company", AuditLog.subject_id == str(c.id))
        .order_by(AuditLog.created_at.desc()).limit(25)
    ).all()
    return {
        **_summary(c, sorted({s.scout for s in sightings})),
        "aliases": c.aliases,
        "website": c.website,
        "type_source": c.type_source,
        "source_data": c.source_data,
        "sightings": [
            {"scout": s.scout, "name_seen": s.name_seen, "url": s.url,
             "first_seen_at": s.first_seen_at, "last_seen_at": s.last_seen_at}
            for s in sightings
        ],
        "contact_leads": [{"email": l.email, "source": l.source, "context": l.context} for l in leads],
        "facts": fact_card(session, c.id),
        "verdicts": [verdict_dict(v) for v in verdicts],
        "audit": [{"at": a.created_at, "action": a.action, "summary": a.summary, "detail": a.detail} for a in log],
    }
