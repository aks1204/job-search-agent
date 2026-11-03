"""Module 6a: the app's screens, read-only (spec §6.3; UI reference: job-agent-console.html).

Server-rendered from the same data the API and CLI use, so every screen is tested by pytest and every screen
has its own address (the summary mail's buttons link to them). Text from postings and websites is always
escaped (Jinja autoescape). Nothing loads from the internet. Buttons come in 6b, the Review actions in 6c.
"""

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config_loader import get_config
from app.db import get_session
from app.models import (
    Application, Company, CompanyFeed, JobAssessment, JobCluster, JobFeedback, JobPosting, JobScore, Notification,
    OutreachCampaign,
)

router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

BANDS = {"fast": "fast_lane", "inbox": "inbox", "maybe": "maybe"}
APPLIED = ("applied", "confirmed", "in_review", "interviewing", "offer", "rejected", "silent")
KEPT = ("passed", "watching", "dormant")
MAYBE_CARDS = 25
AUDIT_GROUPS = {"verdicts": "m1", "jobs": "m2", "outreach": "m3", "applications": "m4", "profile": "m5"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _local(dt: datetime | None, fmt: str = "%d %b %Y, %H:%M") -> str:
    if dt is None:
        return "—"
    from zoneinfo import ZoneInfo

    return dt.astimezone(ZoneInfo(get_config().app.timezone)).strftime(fmt)


def _href(url: str | None) -> str:
    """Only web addresses become links: a posting's URL comes from someone else's site (never javascript:…)."""
    return url if isinstance(url, str) and url.strip().lower().startswith(("http://", "https://")) else "#"


_OLD_PAY_TEXT = (  # text saved before 2026-11-03 (role reasons, audit entries) carried your pay: today's wording
    (re.compile(r"below the floor you set \(about (\d+) vs \d+ LPA\)"),
     r"below the lowest pay you set (this job: about \1 LPA)"),
    (re.compile(r"about \d+% of your current pay"), "well below your current pay"),
    (re.compile(r"Only consider roles paying at least ~\d+ LPA\?"),
     "Set the lowest pay you'd take? (the level is on Profile)"),
    (re.compile(r"paying below ~\d+ LPA"), "paying below a certain level"),
    (re.compile(r"Q: What is your current pay\? A: .*"), "your current pay (kept private, see Profile)"),
)


def _private(text: str) -> str:
    """Your pay appears on the Profile screen only."""
    for pattern, repl in _OLD_PAY_TEXT:
        text = pattern.sub(repl, text)
    return text


templates.env.filters["private"] = _private
from app.web.text import rich_text  # noqa: E402  (job descriptions: paragraphs, headings, lists)
from app.web.labels import CSS, role_chips  # noqa: E402  (the same words on every screen and in the mail)

templates.env.filters["rich"] = rich_text
templates.env.filters["local"] = _local
templates.env.filters["day"] = lambda dt: _local(dt, "%d %b %Y")
templates.env.filters["href"] = _href
templates.env.globals["chip_css"] = CSS          # the labels' colours (web/labels.py)


def _nav(session: Session) -> dict:
    """Sidebar badges: blue = matches from the last `new_badge_hours` (owner: one week), red = emails to review."""
    from app.api.jobs import listed_query, not_pursued
    from app.modules.m3_outreach.reopening import visible

    since = _now() - timedelta(hours=get_config().app.new_badge_hours)
    fresh = session.execute(listed_query(JobScore).where(JobScore.band.in_(("fast_lane", "inbox")),
                                                         JobScore.banded_at >= since, not_pursued())).all()
    pending = session.scalar(select(func.count()).select_from(OutreachCampaign)
                             .where(OutreachCampaign.status == "pending_review")) or 0
    last_sync = session.scalar(select(func.max(CompanyFeed.last_sync_at)))
    pings = session.scalar(select(func.count()).select_from(Notification).where(Notification.read_at.is_(None))) or 0
    return {"new": len(visible(session, fresh)), "review": pending, "last_sync": last_sync,
            "next_mail": _next_mail(), "pings": pings}


def _next_mail() -> str:
    from zoneinfo import ZoneInfo

    from app.modules.m6_mail import switches

    if not switches.is_on("summary"):
        return "off (Settings)"
    cfg = get_config().app
    tz = ZoneInfo(cfg.timezone)
    now = _now().astimezone(tz)
    times = sorted(cfg.summary_mail_times)
    for t in times:
        if (t.hour, t.minute) > (now.hour, now.minute):
            return f"{t:%H:%M}"
    return f"tomorrow {times[0]:%H:%M}" if times else "—"


def _page(request: Request, session: Session, template: str, screen: str, **context) -> HTMLResponse:
    """Every screen: the sidebar, this browser's button code (6b), and the line saying what a button just did."""
    from app.web.actions import FLASH_COOKIE, read_flash
    from app.web.safety import form_token, remember_token

    here = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    flash = read_flash(request)
    response = templates.TemplateResponse(request, template, {
        "screen": screen, "nav": _nav(session), "token": form_token(request), "here": here, "flash": flash,
        **context})
    remember_token(request, response)
    if flash is not None:
        response.delete_cookie(FLASH_COOKIE, path="/")
    return response


def _chips() -> dict:
    """The one-tap reasons for Skip / 👎 (Part 5c)."""
    from app.modules.m5_profile.reasons import CHIPS

    return CHIPS


@router.get("/")
def home() -> RedirectResponse:
    return RedirectResponse("/jobs")


# --- Jobs -------------------------------------------------------------------------------------------------

@router.get("/jobs", response_class=HTMLResponse)
def jobs(request: Request, tab: str = "inbox", session: Session = Depends(get_session)):
    from app.api.jobs import listed_query, not_pursued, shortlist
    from app.modules.m3_outreach.reopening import visible

    if tab not in (*BANDS, "pursuing"):
        tab = "pursuing" if tab == "applied" else "inbox"           # (the tab's old name)
    # exact counts, one query for all tabs (the cards below stop at 500, with "+N more"); pursued roles have
    # their own tab
    shown = listed_query(JobScore).where(JobScore.band.in_(tuple(BANDS.values())), not_pursued())
    listed = visible(session, session.execute(shown).all())
    counts = {t: sum(1 for (s,) in listed if s.band == b) for t, b in BANDS.items()}
    pursuing = _pursuing(session)
    counts["pursuing"] = len(pursuing)
    if tab == "pursuing":
        cards, more = pursuing, 0
    else:
        limit = MAYBE_CARDS if tab == "maybe" else 500
        cards = shortlist(band=BANDS[tab], limit=limit, undecided=True, cluster_id=None, session=session)
        more = max(counts[tab] - len(cards), 0)
        cards = _with_kept_card(request, session, cards)
    from app.web.actions import read_flash

    flash = read_flash(request)
    inline = bool(tab != "pursuing" and flash and flash["card"]
                  and any(c["cluster_id"] == flash["card"] for c in cards))
    return _page(request, session, "jobs.html", "jobs", tab=tab, counts=counts, cards=cards, more=more,
                 chips=_chips(), flash_inline=inline)       # the result line sits on the card itself


def _with_kept_card(request: Request, session: Session, cards: list[dict]) -> list[dict]:
    """The role you just pressed Pursue / Skip on stays in its place for this one view, marked, with Undo;
    reload and it's gone (6b, owner 2026-11-03)."""
    from app.api.jobs import shortlist
    from app.web.actions import read_flash

    flash = read_flash(request)
    if not flash or not flash["card"] or any(c["cluster_id"] == flash["card"] for c in cards):
        return cards
    kept = shortlist(band=None, limit=1, undecided=False, cluster_id=flash["card"], session=session) or \
        shortlist(band="archive", limit=1, undecided=False, cluster_id=flash["card"], session=session)
    if not kept:
        return cards
    card = {**kept[0], "kept": True}
    at = next((i for i, c in enumerate(cards) if c["score"] < card["score"]), len(cards))
    return [*cards[:at], card, *cards[at:]]


def _pursuing(session: Session) -> list[dict]:
    """Roles you pursued, roles the agent put on your apply list itself (the Fast lane: marked "added
    automatically", with Skip), and every application you made: what to do next first (apply), then the rest
    by date. Each with its apply links, how the application stands, and how its outreach is going."""
    from sqlalchemy import and_, or_

    from app.modules.m3_outreach.reopening import hidden_until

    acted = set(session.scalars(select(JobScore.cluster_id).where(JobScore.owner_action == "act")).all())
    apps = {a.cluster_id: a for a in session.scalars(select(Application).where(
        or_(Application.cluster_id.in_(acted), Application.status.in_(APPLIED),
            and_(Application.status == "queued", Application.on_list.is_(True))))).all()}
    ids = acted | set(apps)
    if not ids:
        return []
    campaigns = {c.cluster_id: c for c in session.scalars(select(OutreachCampaign).where(
        OutreachCampaign.cluster_id.in_(ids)).order_by(OutreachCampaign.id)).all()}     # the latest wins
    out = []
    for cl, co in session.execute(select(JobCluster, Company).join(Company, Company.id == JobCluster.company_id)
                                  .where(JobCluster.id.in_(ids))).all():
        app = apps.get(cl.id)
        to_apply = app is None or app.status == "queued"
        held = hidden_until(session, cl.id) if to_apply else None     # a wait after a rejection: no apply link
        if to_apply and co.status in ("blocked", "rejected"):
            continue                                                   # e.g. it turned out to be your employer
        camp = campaigns.get(cl.id)
        out.append({"cluster_id": cl.id, "title": cl.title, "company": co.name, "app": app, "held": held,
                    "to_apply": to_apply, "auto": to_apply and cl.id not in acted,
                    "outreach": _outreach_state(session, camp),
                    "chips": role_chips(app, camp.status if camp else None, to_apply),
                    "at": (app.status_at if app and app.status_at else None)})
    far = datetime.min.replace(tzinfo=timezone.utc)
    return sorted(out, key=lambda r: (not r["to_apply"], -(r["at"] or far).timestamp()))


def _outreach_state(session: Session, c) -> str:
    from app.models import OutreachEmail

    if c is None:
        return "contacts and a draft come on the next round"
    if c.status == "pending_review":
        return "draft ready: see Review"
    if c.status in ("scheduled", "running"):
        emails = session.scalars(select(OutreachEmail).where(OutreachEmail.campaign_id == c.id)).all()
        emails = [e for e in emails if e.status != "cancelled"]
        return f"sending: {sum(1 for e in emails if e.sent_at)} of {len(emails)} sent"
    if c.status == "paused":
        return "outreach on hold"
    if c.status == "cancelled":
        return "outreach stopped"
    return f"outreach finished{': ' + c.outcome.replace('_', ' ') if c.outcome else ''}"


# --- Role detail ------------------------------------------------------------------------------------------

@router.get("/roles/{cluster_id}", response_class=HTMLResponse)
def role(request: Request, cluster_id: int, session: Session = Depends(get_session)):
    from app.api.contacts import role_channel, role_draft
    from app.modules.m3_outreach.drafting import sibling_req_ids
    from app.modules.m3_outreach.reopening import hidden_until, outreach_wait
    from app.modules.m4_apply.tracker import role_links, story

    cluster = session.get(JobCluster, cluster_id)
    if cluster is None:
        raise HTTPException(404, "no such role")
    company = session.get(Company, cluster.company_id)
    score = session.scalar(select(JobScore).where(JobScore.cluster_id == cluster_id))
    assessment = session.scalar(select(JobAssessment).where(JobAssessment.cluster_id == cluster_id))
    job = session.get(JobPosting, assessment.job_id) if assessment else session.scalar(
        select(JobPosting).where(JobPosting.cluster_id == cluster_id).order_by(JobPosting.id))
    postings = session.scalars(select(JobPosting).where(JobPosting.cluster_id == cluster_id)
                               .order_by(JobPosting.first_seen_at)).all()
    try:
        channel = role_channel(cluster_id, session)
    except HTTPException:
        channel = None
    try:
        draft = role_draft(cluster_id, session)
    except HTTPException:
        draft = None
    app = session.scalar(select(Application).where(Application.cluster_id == cluster_id))
    # a role hidden by a wait shows why, and no apply or posting links until the wait ends (owner, 2026-11-03)
    wait = outreach_wait(session, cluster_id)
    hidden = {"until": wait[0], "why": wait[1]} if wait and hidden_until(session, cluster_id) else None
    stands = None                    # where it stands, as on the Pursuing tab (only for a role you're pursuing)
    if app is not None or (score is not None and score.owner_action == "act"):
        camp = session.scalar(select(OutreachCampaign).where(OutreachCampaign.cluster_id == cluster_id)
                              .order_by(OutreachCampaign.id.desc()).limit(1))
        stands = role_chips(app, camp.status if camp else None, app is None or app.status == "queued")
    return _page(request, session, "role.html", "jobs", cluster=cluster, company=company, score=score,
                 assessment=assessment, job=job, postings=postings, req_ids=sibling_req_ids(session, cluster_id),
                 links=[] if hidden else role_links(session, cluster_id), channel=channel, draft=draft, app=app,
                 story=story(session, cluster_id), hidden=hidden, chips=_chips(), stands=stands,
                 reaction=session.scalar(select(JobFeedback).where(JobFeedback.cluster_id == cluster_id)
                                         .order_by(JobFeedback.id.desc())))


# --- Review (read-only until 6c) ----------------------------------------------------------------------------

@router.get("/review", response_class=HTMLResponse)
def review(request: Request, session: Session = Depends(get_session)):
    from app.web.actions import read_flash
    from app.web.review import page_context

    ctx = page_context(session)
    flash = read_flash(request)
    inline = bool(flash and flash["card"] and any(c["id"] == flash["card"] for c in ctx["waiting"] + ctx["under_way"]))
    return _page(request, session, "review.html", "review", flash_inline=inline, **ctx)   # the line sits on its card


# --- Companies --------------------------------------------------------------------------------------------

@router.get("/companies", response_class=HTMLResponse)
def companies(request: Request, status: str = "kept", q: str = "", session: Session = Depends(get_session)):
    from app.api.feeds import coverage_report
    from app.api.verdicts import list_verdicts

    stmt = select(Company, CompanyFeed).outerjoin(CompanyFeed, CompanyFeed.company_id == Company.id)
    if status == "kept":
        stmt = stmt.where(Company.status.in_(KEPT))
    elif status != "all":
        stmt = stmt.where(Company.status == status)
    if q.strip():
        like = f"%{q.strip().lower()}%"
        stmt = stmt.where(func.lower(Company.name).like(like) | Company.domain.like(like))
    rows = session.execute(stmt.order_by(Company.name).limit(500)).all()
    statuses = dict(session.execute(select(Company.status, func.count()).group_by(Company.status)).all())
    return _page(request, session, "companies.html", "companies", rows=rows, status=status, q=q, statuses=statuses,
                 coverage=coverage_report(session),
                 verdicts=list_verdicts(passed=None, decided_by=None, limit=15, offset=0, session=session),
                 employers=_employers(session))


def _employers(session: Session) -> list[dict]:
    """Your current and past employers (never watched or contacted), each with what it keeps off the list."""
    from app.modules.m1_companies import employers as E

    return [{"name": e.name, "domain": e.domain, "current": e.current,
             "blocked": [c.name for c in E.blocked_for(session, e)]} for e in E.employers(session)]


@router.get("/companies/{company_id}", response_class=HTMLResponse)
def company(request: Request, company_id: int, session: Session = Depends(get_session)):
    from app.api.companies import get_company
    from app.api.contacts import company_benches
    from app.modules.maintenance.sources import fits_website
    from app.web.actions import CONTACT_KINDS
    from app.modules.m3_outreach.reopening import visible

    data = get_company(company_id, session)
    roles = session.execute(
        select(JobScore, JobCluster).join(JobCluster, JobCluster.id == JobScore.cluster_id)
        .where(JobCluster.company_id == company_id, JobScore.band != "archive")
        .order_by(JobScore.score.desc())).all()
    return _page(request, session, "company.html", "companies", c=data,
                 benches=company_benches(company_id, session), roles=visible(session, roles, 50),
                 name_fits=fits_website(session.get(Company, company_id)), kinds=CONTACT_KINDS)


# --- Profile ----------------------------------------------------------------------------------------------

@router.get("/profile", response_class=HTMLResponse)
def profile(request: Request, session: Session = Depends(get_session)):
    from app.api.profile import get_direction, get_profile
    from app.modules.m5_profile import store
    from app.modules.m5_profile.profile import TIME_SENSITIVE

    prof = get_profile(session)
    confirmed = prof["confirmed"]
    data = confirmed["data"] if confirmed else (get_config().owner.model_dump(mode="json") if get_config().owner else {})
    years = store.experience_years(data.get("employment_history")) if data else None
    from app.modules.m2_jobs.profile_text import resume_path

    resume = resume_path()
    return _page(request, session, "profile.html", "profile", prof=prof, data=data, years=years,
                 time_sensitive=TIME_SENSITIVE, direction=get_direction(session),
                 resume={"name": resume.name, "exists": resume.is_file(),
                         "updated": datetime.fromtimestamp(resume.stat().st_mtime, timezone.utc)
                         if resume.is_file() else None})


# --- Memory -----------------------------------------------------------------------------------------------

@router.get("/memory", response_class=HTMLResponse)
def memory(request: Request, q: str = "", archived: int = 0, session: Session = Depends(get_session)):
    from app.api.profile import list_memory, search_memory

    entries = search_memory(q.strip(), session) if q.strip() else list_memory(kind=None, archived=bool(archived),
                                                                              session=session)
    groups = {k: [e for e in entries if e["kind"] == k] for k in ("answer", "precedent", "taste_signal")}
    return _page(request, session, "memory.html", "memory", groups=groups, q=q, archived=bool(archived))


# --- Audit log --------------------------------------------------------------------------------------------

@router.get("/audit", response_class=HTMLResponse)
def audit(request: Request, group: str = "all", page: int = 1, session: Session = Depends(get_session)):
    from app.models import AuditLog

    stmt = select(AuditLog)
    if group in AUDIT_GROUPS:
        stmt = stmt.where(AuditLog.module == AUDIT_GROUPS[group])
    elif group == "housekeeping":
        stmt = stmt.where(AuditLog.module.not_in(tuple(AUDIT_GROUPS.values())))
    else:
        group = "all"
    page = max(page, 1)
    rows = session.scalars(stmt.order_by(AuditLog.created_at.desc()).limit(51).offset((page - 1) * 50)).all()
    return _page(request, session, "audit.html", "audit", rows=rows[:50], more=len(rows) > 50, group=group,
                 page=page, groups=["all", *AUDIT_GROUPS, "housekeeping"])


# --- Settings ---------------------------------------------------------------------------------------------

# Your rule files, shown read-only. Never your profile file (it holds your pay) or anything with keys.
RULE_FILES = ("app.yaml", "company_rules.yaml", "job_rules.yaml", "scouts.yaml", "watch.yaml", "outreach.yaml",
              "apply.yaml", "llm.yaml")


@router.get("/settings", response_class=HTMLResponse)
def settings(request: Request, session: Session = Depends(get_session)):
    from app.api.outreach import gmail_status, sending_state
    from app.core.quota import used_this_month, used_today
    from app.modules.m1_companies.watch.careers import CAREERS_SEARCH_BUDGET
    from app.settings import get_settings

    cfg, st = get_config(), get_settings()
    contacts = cfg.outreach.contacts

    def service(name, on, counter=None, cap=None, period="month"):
        """(name, set up?, used — None = not counted, "?" = the counter store can't be reached, cap, period).
        Settings must open even when Redis is down: it's where you'd look to see what's wrong."""
        used = None
        if on and counter:
            try:
                used = (used_today if period == "day" else used_this_month)(counter)
            except Exception:
                used = "?"
        return name, on, used, cap, period

    brave = bool(st.brave_api_key)
    services = [
        service("Anthropic (AI) — required", bool(st.anthropic_api_key)),
        service("Brave search (company websites)", brave, "brave:resolver", cfg.scouts.resolver.brave_monthly_budget),
        service("Brave search (careers pages)", brave, "brave:careers", CAREERS_SEARCH_BUDGET),
        service("Brave search (startup news)", brave, "brave:enrichment",
                cfg.rules.evaluation.search_monthly_budget),
        service("Brave search (people)", brave, "brave:contacts", contacts.searches_per_month),
        service("Hunter email finder", bool(st.hunter_api_key), "hunter:finder",
                contacts.enrichment.hunter_finder_per_month),
        service("Apollo person match", bool(st.apollo_api_key), "apollo:match", contacts.enrichment.apollo_per_month),
        service("MyEmailVerifier", bool(st.myemailverifier_api_key), "myemailverifier",
                contacts.verification.myemailverifier_per_day, "day"),
        service("Adzuna (job listings)", bool(st.adzuna_app_id and st.adzuna_app_key)),
        service("Jooble (job listings)", bool(st.jooble_api_key)),
    ]
    rules = []
    for name in RULE_FILES:
        path = Path(st.config_dir) / name
        if path.is_file():
            rules.append((name, path.read_text(encoding="utf-8")))
    try:                          # the switch lives in Redis too: unknown, not an error page, when it's down
        sending = sending_state(session)
    except Exception:
        sending = {"on": None, "breaker": "unknown", "breaker_reason": "the switch store (Redis) isn't reachable"}
    return _page(request, session, "settings.html", "settings", app=cfg.app, sending=sending,
                 gmail=gmail_status(session), services=services, rules=rules, mail=_mail_state(session))


def _mail_state(session: Session) -> dict:
    """The summary mail and alert switches (6d), and when each last went out."""
    from app.models import MailLog
    from app.modules.m6_mail import switches

    def last(kind):
        return session.scalar(select(func.max(MailLog.sent_at)).where(MailLog.kind == kind, MailLog.preview.is_(False)))

    return {"switches": [(k, label, switches.is_on(k)) for k, label in switches.SWITCHES.items()],
            "last_summary": last("summary"), "last_alert": last("alert")}


# --- Maintenance ------------------------------------------------------------------------------------------

@router.get("/maintenance", response_class=HTMLResponse)
def maintenance(request: Request, session: Session = Depends(get_session)):
    from app.api.feeds import coverage_report, list_feeds
    from app.api.maintenance import sources
    from app.api.scouts import list_scouts
    from app.web import runs

    feeds = list_feeds(health=None, method=None, limit=1, session=session)["summary"]
    failing = list_feeds(health="failing", method=None, limit=50, session=session)["items"]
    return _page(request, session, "maintenance.html", "maintenance", coverage=coverage_report(session),
                 feeds=feeds, failing=failing, report=sources(days=90, all_time=False, session=session),
                 scouts=list_scouts(session), jobs=[(k, runs.JOBS[k][0]) for k in runs.MAINTENANCE_JOBS])


# --- Pings ------------------------------------------------------------------------------------------------

@router.get("/pings", response_class=HTMLResponse)
def pings(request: Request, everything: int = 0, session: Session = Depends(get_session)):
    """What the agent wants you to know: things that need you first, then news. Unread only, unless asked.
    Each ping opens where you act on it; simple answers (Yes / No, which role, Apply) happen right here."""
    from app.models import InboxMessage
    from app.modules.m3_outreach.reopening import hidden_until
    from app.modules.m5_profile import direction as D

    q = select(Notification).order_by(Notification.kind != "needs_you", Notification.id.desc()).limit(100)
    if not everything:
        q = q.where(Notification.read_at.is_(None))
    rows = session.scalars(q).all()
    emails, applies = {}, {}                   # "which role is this email about?" · "Apply: …" pings
    for n in rows:
        if n.read_at is not None or not (n.subject_id or "").isdigit():
            continue
        if n.subject_type == "inbox":
            m = session.scalar(select(InboxMessage).where(InboxMessage.no == int(n.subject_id)))
            if m is not None and m.outcome == "ambiguous":
                emails[n.id] = {"no": m.no, "roles": [r for cid in m.candidates
                                                      if (r := session.get(JobCluster, cid)) is not None]}
        elif n.subject_type == "job_cluster":
            app = session.scalar(select(Application).where(Application.cluster_id == int(n.subject_id)))
            if app is not None and app.status == "queued" and not hidden_until(session, app.cluster_id):
                applies[n.id] = {"cluster_id": app.cluster_id, "links": (app.links or [])[:2]}
    asks = any(n.subject_type == "direction" and n.read_at is None for n in rows)
    suggestions = ((D.explained(session) or {}).get("suggestions") or []) if asks else []
    return _page(request, session, "pings.html", "pings", rows=rows, emails=emails, applies=applies,
                 suggestions=suggestions, targets={n.id: ping_target(session, n) for n in rows},
                 everything=bool(everything))


def ping_target(session: Session, n: Notification) -> str | None:
    """Where a ping's Open button goes: the screen where you act on it."""
    kind, sid = n.subject_type or "", n.subject_id or ""
    if kind == "direction" or (kind == "profile" and sid == "direction"):
        return "/profile#direction"
    if kind == "profile" and sid == "draft":
        return "/profile/edit"                           # your new résumé reading, as a form to check
    if kind == "profile" and sid in ("current_compensation", "notice_period_days"):
        return "/profile/edit#pay"
    if kind == "profile":
        return "/profile"
    if kind == "job_cluster" and sid.isdigit():
        if (n.title or "").startswith("Apply:"):
            return f"/jobs?tab=pursuing#c{sid}"          # where its apply link, Mark applied and Skip are
        return f"/roles/{sid}"
    if kind == "job" and sid.isdigit():
        cluster = session.scalar(select(JobPosting.cluster_id).where(JobPosting.id == int(sid)))
        return f"/roles/{cluster}" if cluster else None
    if kind == "company" and sid.isdigit():
        return f"/companies/{sid}"
    if kind == "campaign" and sid.isdigit():
        return f"/review#c{sid}"
    if kind == "memory" and not sid.startswith("undo:"):
        return "/memory"
    if kind == "owner_employer":
        return "/companies#employers"
    if kind in ("run", "source_warning"):
        return "/maintenance"
    return None


# Terminal instructions in a ping's text: the screen has buttons for them (the terminal's `pings` still shows them)
_CLI_HINTS = re.compile(r"\s*(?:·\s*)?(?:Pick one: assign \d+ <role id>|another job: assign \d+ --other \"Job title\""
                        r"|not yours: assign \d+ --ignore|\(or: [^)]*\))\.?")


def _ping_lines(text: str | None) -> list[str]:
    """A ping's detail as separate lines, without the terminal instructions."""
    if not text:
        return []
    text = _CLI_HINTS.sub("", _private(text))
    return [p.strip(" ·") for p in re.split(r"\s·\s|;\s(?=[A-Z])", text) if p.strip(" ·.")]


templates.env.filters["ping_lines"] = _ping_lines
