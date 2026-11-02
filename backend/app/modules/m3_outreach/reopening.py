"""The reopening matrix (spec §2.2, owner's decisions 2026-11-01): when a company has turned you down or gone
quiet, what may be sent next — and what the draft is told about it.

- Silence: the same posting is never emailed again (that would be a follow-up). A role whose campaign went
  silent gets one new campaign when it opens again: a new posting (new job ID) or a repost.
- Rejection: SIMILAR roles (same role family) at that company wait 30 days, or 90 days if you had reached
  interviews. While waiting they're hidden — no card, no apply link, no outreach (revised 2026-11-03) — and
  appear as a new match when the wait ends. Other families aren't held. Only roles the agent tracked count:
  an application made outside the agent starts no wait.
- Drafts never mention the past — not the earlier email, the rejection or the interview.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.config_loader import get_config
from app.models.application import Application, ApplicationEvent
from app.models.assessment import JobAssessment
from app.models.job import JobCluster, JobPosting
from app.models.outreach import OutreachCampaign, OutreachEmail


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _family(session: Session, cluster_id: int) -> str | None:
    a = session.scalar(select(JobAssessment).where(JobAssessment.cluster_id == cluster_id))
    family = ((a.read or {}).get("role_family") or "").strip().lower() if a else ""
    return family if family and family != "?" else None


def _interviewed(session: Session, cluster_id: int) -> bool:
    """You reached interviews for this role through the agent (an outside application's interview doesn't count)."""
    app = session.scalar(select(Application).where(Application.cluster_id == cluster_id))
    return app is not None and not app.outside and session.scalar(select(ApplicationEvent.id).where(
        ApplicationEvent.application_id == app.id, ApplicationEvent.kind == "interviewing").limit(1)) is not None


@dataclass
class Rejection:
    cluster_id: int
    at: datetime
    interviewed: bool


def rejections(session: Session, company_id: int) -> list[Rejection]:
    """Every rejection at this company on a role the agent tracked: an application marked rejected (from the
    hiring system's email, or by you), or a reply to your outreach sorted as a rejection. Oldest first."""
    found: dict[int, datetime] = {}
    for app in session.scalars(select(Application).where(Application.company_id == company_id,
                                                         Application.status == "rejected",
                                                         Application.outside.is_(False))).all():
        found[app.cluster_id] = app.status_at
    for c in session.scalars(select(OutreachCampaign).where(OutreachCampaign.company_id == company_id,
                                                            OutreachCampaign.outcome == "rejected")).all():
        at = c.outcome_at or c.ended_at or c.created_at
        found[c.cluster_id] = max(found.get(c.cluster_id, at), at)
    return sorted((Rejection(cid, at, _interviewed(session, cid)) for cid, at in found.items()), key=lambda r: r.at)


def outreach_wait(session: Session, cluster_id: int, now: datetime | None = None) -> tuple[datetime, str] | None:
    """(until, why) while outreach for this role waits after a rejection for a similar role at the company;
    None when it may go ahead. A role you were turned down for yourself gets no new outreach at all."""
    now = now or _now()
    cluster = session.get(JobCluster, cluster_id)
    if cluster is None:
        return None
    rules = get_config().outreach.reopening
    family = _family(session, cluster_id)
    longest: tuple[datetime, str] | None = None
    for r in rejections(session, cluster.company_id):
        if r.cluster_id == cluster_id:
            return datetime.max.replace(tzinfo=timezone.utc), f"you were turned down for this role on {r.at:%d %b %Y}"
        if family is None or _family(session, r.cluster_id) != family:
            continue                          # another family (or unknown): not held
        days = rules.wait_days_after_interview if r.interviewed else rules.wait_days
        until = r.at + timedelta(days=days)
        if until > now and (longest is None or until > longest[0]):
            stage = " after an interview" if r.interviewed else ""
            longest = (until, f"turned down{stage} for a similar role ({family}) on {r.at:%d %b %Y}")
    return longest


def visible(session: Session, rows: list, limit: int | None = None) -> list:
    """Your matches without the roles hidden by a wait (owner decision 2026-11-03: a similar role at a company
    that just turned you down is hidden until the wait ends, then appears as a new match). `rows` start with a
    JobScore; at most `limit` are kept."""
    out = []
    for row in rows:
        if limit is not None and len(out) >= limit:
            break
        if hidden_until(session, row[0].cluster_id) is None:
            out.append(row)
    return out


def hidden_until(session: Session, cluster_id: int, now: datetime | None = None) -> datetime | None:
    """When a role hidden by a wait reappears; None if it isn't hidden. (The role you were turned down for
    yourself isn't hidden — it just gets no new outreach; its application shows as rejected.)"""
    wait = outreach_wait(session, cluster_id, now)
    return wait[0] if wait and wait[0].year != 9999 else None


def new_opening_since(session: Session, cluster_id: int, since: datetime) -> bool:
    """The role opened again after `since`: a new posting (job ID) in it, or a closed posting that came back."""
    return session.scalar(select(JobPosting.id).where(
        JobPosting.cluster_id == cluster_id, JobPosting.status == "open", JobPosting.lead_only.is_(False),
        or_(JobPosting.first_seen_at > since, JobPosting.reopened_at > since)).limit(1)) is not None


def may_start_again(session: Session, current: OutreachCampaign) -> bool:
    """One campaign per posting; after a silent one, one more when the role opens again."""
    if current.outcome != "silent" or current.status not in ("done", "cancelled"):
        return False
    return new_opening_since(session, current.cluster_id, silent_since(current))


def silent_since(c: OutreachCampaign) -> datetime:
    """When a campaign went silent: openings after this are new."""
    return c.outcome_at or c.ended_at or c.created_at


def history_scenario(session: Session, cluster_id: int) -> str | None:
    """What the draft is told about the company's history with you — strongest first; every note says never
    to mention it. None: no history."""
    cluster = session.get(JobCluster, cluster_id)
    if cluster is None:
        return None
    past = rejections(session, cluster.company_id)
    if any(r.interviewed for r in past):
        return "post_interview"
    if past:
        return "post_rejection"
    for c in session.scalars(select(OutreachCampaign).where(OutreachCampaign.company_id == cluster.company_id,
                                                            OutreachCampaign.outcome == "silent")).all():
        # this role's own silent campaign counts only once it opened again (otherwise nothing new is sent)
        if c.cluster_id != cluster_id or new_opening_since(session, cluster_id, silent_since(c)):
            return "reopening"
    return None


def previously_emailed(session: Session, cluster_id: int, before_campaign_id: int | None = None) -> dict[str, datetime]:
    """address → when it was emailed about this role in an earlier campaign (shown on the Review screen)."""
    q = (select(OutreachEmail.to_email, OutreachEmail.sent_at)
         .join(OutreachCampaign, OutreachCampaign.id == OutreachEmail.campaign_id)
         .where(OutreachCampaign.cluster_id == cluster_id, OutreachEmail.sent_at.is_not(None)))
    if before_campaign_id is not None:
        q = q.where(OutreachCampaign.id != before_campaign_id)
    out: dict[str, datetime] = {}
    for address, at in session.execute(q).all():
        key = address.lower()
        out[key] = max(out.get(key, at), at)
    return out
