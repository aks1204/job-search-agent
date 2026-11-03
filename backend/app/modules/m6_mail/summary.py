"""The twice-daily summary mail (spec §6.1): the app's one regular message to you.

Five sections, each only when it has something: ⚡ Needs you · 📥 New matches · 📤 What happened (per role) ·
📊 Pipeline · 🔧 Housekeeping. Every card is the same shape (title, one detail line, a status chip, at most two
buttons, a coloured stripe: red = needs you, blue = new, green = done, grey = tracking). Buttons open the exact
screen in the app (owner 2026-11-04: links never change anything by themselves).

"New" and "what happened" mean: since the last summary. A day with nothing new sends nothing. Roles hidden by
a wait after a rejection never appear (`visible`), and your pay never does.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.config_loader import get_config
from app.core import audit
from app.models import (
    Application, Company, DirectionSuggestion, JobAssessment, JobCluster, JobPosting, JobScore, MailLog, Notification,
    OutreachCampaign, OutreachEmail,
)

TOP = 5                         # cards per section before "+N more in the app"
FIRST_LOOKBACK = timedelta(hours=12)
STRIPES = {"red": "#B3382C", "blue": "#2F5FB3", "green": "#2E7D4F", "gray": "#CFD4D0"}
CHIPS = {"red": ("#B3382C", "#F9ECEA"), "blue": ("#2F5FB3", "#EBF0F8"), "green": ("#2E7D4F", "#EAF3EC"),
         "gray": ("#5E6763", "#EEF0ED"), "amber": ("#9A6A12", "#F7F0E0")}


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Card:
    title: str
    detail: str = ""
    chip: str = ""
    color: str = "gray"                       # the stripe and the chip
    buttons: list[tuple[str, str]] = field(default_factory=list)   # (label, address) — at most two
    lines: list[tuple[str, str | None]] = field(default_factory=list)   # events under a role: (text, link)


@dataclass
class Section:
    key: str
    heading: str
    cards: list[Card] = field(default_factory=list)
    more: int = 0                             # left out: "+N more in the app"
    more_url: str = ""
    note: str = ""                            # one grey line (e.g. "Maybe: 4 more")


def url(path: str) -> str:
    return get_config().app.app_url.rstrip("/") + path


def _web(link: str | None) -> str | None:
    return link if isinstance(link, str) and link.lower().startswith(("http://", "https://")) else None


def _top(section: Section, cards: list[Card], more_url: str) -> Section:
    section.cards, section.more, section.more_url = cards[:TOP], max(len(cards) - TOP, 0), url(more_url)
    return section


def last_summary(session: Session) -> datetime | None:
    return session.scalar(select(func.max(MailLog.sent_at)).where(MailLog.kind == "summary",
                                                                 MailLog.preview.is_(False)))


# --- the sections -------------------------------------------------------------------------------------

def _new_matches(session: Session, since: datetime) -> tuple[list[tuple], int]:
    """Fast lane first, then the Inbox, scored since the last summary; and how many went to Maybe."""
    from app.api.jobs import listed_query
    from app.modules.m3_outreach.reopening import visible

    from sqlalchemy import exists

    applied = exists().where(Application.cluster_id == JobScore.cluster_id, Application.status != "queued")
    q = (listed_query(JobScore, JobAssessment, JobPosting, Company, JobCluster)
         .where(JobScore.band.in_(("fast_lane", "inbox")), JobScore.banded_at > since,
                JobScore.owner_action.is_(None), ~applied)        # (a fast-lane role on your apply list is new)
         .order_by(JobScore.band != "fast_lane", JobScore.score.desc()))
    rows = visible(session, session.execute(q).all())
    maybe = visible(session, session.execute(listed_query(JobScore).where(
        JobScore.band == "maybe", JobScore.banded_at > since, JobScore.owner_action.is_(None))).all())
    return rows, len(maybe)


def _match_card(sc, a, j, c, cl) -> Card:
    fast = sc.band == "fast_lane"
    from app.web.pages import _private

    one = _private((sc.grade or {}).get("one_liner") or (a.read or {}).get("one_liner") or "")
    detail = " · ".join(x for x in (j.location, one) if x)
    buttons = [("Open role →", url(f"/roles/{cl.id}"))]
    if fast and _web(j.url):
        buttons.insert(0, ("Open apply link →", j.url))
    return Card(f"{cl.title} · {c.name}", detail, f"{sc.score} · {'fast lane' if fast else 'inbox'}",
                "blue", buttons[:2])


def _needs_you(session: Session, shown: set[int], now: datetime) -> tuple[list[Card], int, list[datetime]]:
    """(cards, older apply links only counted, when each item came up — for "anything new?")."""
    from app.modules.m3_outreach.reopening import hidden_until
    from app.modules.m4_apply.tracker import apply_list, role_links
    from app.web.pages import _ping_lines, _private, ping_target

    cards, born = [], []
    for c, cl, co in session.execute(
            select(OutreachCampaign, JobCluster, Company).join(JobCluster, JobCluster.id == OutreachCampaign.cluster_id)
            .join(Company, Company.id == OutreachCampaign.company_id)
            .where(OutreachCampaign.status == "pending_review").order_by(OutreachCampaign.id)).all():
        if hidden_until(session, cl.id):
            continue                                   # drafted before a rejection for a similar role: it waits too
        n = session.scalar(select(func.count()).select_from(OutreachEmail).where(
            OutreachEmail.campaign_id == c.id, OutreachEmail.decision == "pending")) or 0
        cards.append(Card(f"{cl.title} · {co.name}", f"Outreach ready: {n} email(s) for you to approve, then Begin outreach",
                          "approval", "red", [("Review drafts →", url(f"/review#c{c.id}"))]))
        born.append(c.created_at)
    full, _older = apply_list(session, now)
    full_ids = {cl.id for _, cl, _ in full}
    older = 0
    for app, cl, co in session.execute(
            select(Application, JobCluster, Company).join(JobCluster, JobCluster.id == Application.cluster_id)
            .join(Company, Company.id == Application.company_id)
            .where(Application.status == "queued", Application.on_list.is_(True))).all():
        if co.status in ("blocked", "rejected") or hidden_until(session, cl.id):
            continue                                   # e.g. it turned out to be your employer; or a wait
        if cl.id not in full_ids:
            older += 1                                 # older ones: only counted
            continue
        if cl.id in shown:
            continue                                   # on its card under New matches
        links = [x for x in role_links(session, cl.id) if _web(x["url"])]
        buttons = [("Open apply link →", links[0]["url"])] if links else []
        buttons.append(("Pursuing", url(f"/jobs?tab=pursuing#c{cl.id}")))
        many = f" · {len(links)} openings" if len(links) > 1 else ""
        cards.append(Card(f"{cl.title} · {co.name}", f"Apply manually on their site{many}", "apply", "red", buttons))
        born.append(app.queued_at)
    open_q = session.scalars(select(DirectionSuggestion.created_at).where(DirectionSuggestion.status == "open")).all()
    if open_q:
        cards.append(Card("Your career direction", f"{len(open_q)} suggestion(s) to answer Yes or No", "question",
                          "red", [("Answer →", url("/profile#direction"))]))
        born += list(open_q)
    for n in session.scalars(select(Notification).where(
            Notification.section == "needs_you", Notification.read_at.is_(None),   # (some replies are pings that need you)
            ~Notification.title.startswith("Apply:"), ~Notification.title.startswith("Your career direction"))
            .order_by(Notification.created_at.desc())).all():
        lines = _ping_lines(n.detail)
        cards.append(Card(_private(n.title), lines[0] if lines else "", "for you", "red",
                          [("Open →", url(ping_target(session, n) or "/pings"))]))
        born.append(n.created_at)
    return cards, older, [b for b in born if b]


def _role_of(session: Session, n: Notification) -> int | None:
    kind, sid = n.subject_type or "", n.subject_id or ""
    if not sid.isdigit():
        return None
    if kind == "job_cluster":
        return int(sid)
    if kind == "campaign":
        c = session.get(OutreachCampaign, int(sid))
        return c.cluster_id if c else None
    if kind == "job":
        return session.scalar(select(JobPosting.cluster_id).where(JobPosting.id == int(sid)))
    return None


def _happened(session: Session, since: datetime) -> tuple[list[Card], list[str], int]:
    """(one card per role with its events, other updates' titles, how many events in all)."""
    from app.web.pages import _private

    groups: dict[int, list[tuple[str, str | None]]] = {}
    elsewhere: list[str] = []
    for n in session.scalars(select(Notification).where(
            Notification.created_at > since, Notification.section == "happened",
            Notification.kind.in_(("update", "ping")),
            or_(Notification.subject_type.is_(None), Notification.subject_type != "run"))
            .order_by(Notification.created_at)).all():
        role = _role_of(session, n)
        if role is None:
            elsewhere.append(_private(n.title))
        else:
            groups.setdefault(role, []).append((_private(n.title), _web(n.link)))
    sent = session.execute(select(OutreachCampaign.cluster_id, func.count()).select_from(OutreachEmail)
                           .join(OutreachCampaign, OutreachCampaign.id == OutreachEmail.campaign_id)
                           .where(OutreachEmail.sent_at > since).group_by(OutreachCampaign.cluster_id)).all()
    for cluster_id, k in sent:
        groups.setdefault(cluster_id, []).insert(0, (f"{k} outreach email(s) sent", None))
    cards = []
    for cluster_id, events in groups.items():
        row = session.execute(select(JobCluster, Company).join(Company, Company.id == JobCluster.company_id)
                              .where(JobCluster.id == cluster_id)).first()
        if row is None:
            elsewhere += [t for t, _ in events]
            continue
        cl, co = row
        cards.append(Card(f"{cl.title} · {co.name}", "", f"{len(events)} update(s)", "green",
                          [("Open role →", url(f"/roles/{cl.id}"))], lines=events))
    return cards, elsewhere, sum(len(e) for e in groups.values()) + len(elsewhere)


ORDER = {"red": 0, "green": 1, "blue": 2, "gray": 3}
QUIET_STATES = ("silent", "rejected", "closed", "skipped")


def _pipeline(session: Session) -> tuple[list[Card], int]:
    """Every role in flight, one card: on you (red) · live (green) · tracking (blue) · silent (grey)."""
    from app.web.pages import _pursuing

    cards = []
    for r in _pursuing(session):
        if r["held"]:
            continue                                   # hidden by a wait after a rejection
        a, outreach = r["app"], r["outreach"]
        status = a.status if a else "queued"
        if r["to_apply"] or outreach.startswith("draft ready"):
            color, chip = "red", "on you"
        elif status in QUIET_STATES or "silent" in outreach:
            color, chip = "gray", status.replace("_", " ") if status in QUIET_STATES else "silent"
        elif outreach.startswith("sending") or status in ("in_review", "interviewing", "offer"):
            color, chip = "green", "live"
        else:
            color, chip = "blue", "tracking"
        what = "to apply" if r["to_apply"] else status.replace("_", " ")
        cards.append(Card(f"{r['title']} · {r['company']}", f"{what} · outreach: {outreach}", chip, color,
                          [("Open role →", url(f"/roles/{r['cluster_id']}"))]))
    cards.sort(key=lambda c: ORDER[c.color])
    return cards, sum(1 for c in cards if c.color != "gray")


def _housekeeping(session: Session, since: datetime) -> str:
    from app.models import CompanyFeed

    runs = session.scalar(select(func.count()).select_from(Notification).where(
        Notification.created_at > since, Notification.subject_type == "run")) or 0
    warnings = session.scalar(select(func.count()).select_from(Notification).where(
        Notification.created_at > since, Notification.section == "housekeeping")) or 0
    failing = session.scalar(select(func.count()).select_from(CompanyFeed).where(CompanyFeed.health == "failing")) or 0
    parts = [f"{runs} run(s) you started finished" if runs else "",
             f"{warnings} source warning(s), see Maintenance" if warnings else "",
             f"{failing} job feed(s) failing" if failing else ""]
    return " · ".join(p for p in parts if p) or "All quiet: sources and feeds are fine."


# --- the whole mail -----------------------------------------------------------------------------------

@dataclass
class Summary:
    subject: str
    sections: list[Section]
    housekeeping: str
    counts: dict
    quiet: bool                               # nothing new since the last summary
    since: datetime


def build(session: Session, now: datetime | None = None, since: datetime | None = None) -> Summary:
    now = now or _now()
    if since is None:                         # since the last summary; never more than 12 h before you switched it on
        from app.modules.m6_mail import switches

        on = switches.on_since("summary")
        since = last_summary(session) or now - FIRST_LOOKBACK
        if on is not None:
            since = max(since, on - FIRST_LOOKBACK)
    rows, maybe = _new_matches(session, since)
    shown = {cl.id for *_, cl in rows[:TOP]}
    needs, older, born = _needs_you(session, shown, now)
    happened, elsewhere, events = _happened(session, since)
    pipeline, in_flight = _pipeline(session)

    s_needs = _top(Section("needs_you", "⚡ Needs you"), needs, "/pings")
    if older:
        s_needs.note = f"+{older} older apply link(s) still waiting on your Pursuing tab."
    s_new = _top(Section("new", "📥 New matches"), [_match_card(*r) for r in rows], "/jobs")
    if maybe:
        s_new.note = f"Maybe: {maybe} more, in the app."
    s_happened = _top(Section("happened", "📤 What happened"), happened, "/audit")
    if elsewhere:
        extra = f" (+{len(elsewhere) - 3} more)" if len(elsewhere) > 3 else ""
        s_happened.note = "Elsewhere: " + "; ".join(elsewhere[:3]) + extra
    s_pipe = _top(Section("pipeline", "📊 Pipeline"), pipeline, "/jobs?tab=pursuing")
    sections = [s for s in (s_needs, s_new, s_happened, s_pipe) if s.cards or s.note]

    counts = {"need_you": len(needs) + older, "new": len(rows), "maybe": maybe, "updates": events,
              "in_flight": in_flight}
    parts = [f"{counts['need_you']} need you" if counts["need_you"] else "",
             f"{counts['new']} new match{'es' if counts['new'] != 1 else ''}" if counts["new"] else "",
             f"{events} update{'s' if events != 1 else ''}" if events else "",
             f"{in_flight} in flight"]
    quiet = not rows and not events and not maybe and not any(b > since for b in born)
    return Summary(" · ".join(p for p in parts if p), sections, _housekeeping(session, since), counts, quiet, since)


def render(summary: Summary) -> tuple[str, str]:
    """(plain text, web-page code) of the mail."""
    from app.web.pages import templates

    html = templates.env.get_template("mail/summary.html").render(
        s=summary, stripes=STRIPES, chips=CHIPS, app_url=url("/"), settings_url=url("/settings"))
    lines = [summary.subject, ""]
    for sec in summary.sections:
        lines += [sec.heading.upper(), ""]
        for c in sec.cards:
            lines.append(f"- {c.title}" + (f"  [{c.chip}]" if c.chip else ""))
            if c.detail:
                lines.append(f"  {c.detail}")
            lines += [f"  · {t}" + (f" ({link})" if link else "") for t, link in c.lines]
            lines += [f"  {label} {href}" for label, href in c.buttons]
        if sec.more:
            lines.append(f"+{sec.more} more in the app: {sec.more_url}")
        if sec.note:
            lines.append(sec.note)
        lines.append("")
    lines += [f"🔧 {summary.housekeeping}", "", f"Open the app: {url('/')}"]
    return "\n".join(lines), html


def message(sender: str, subject: str, text: str, html: str) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = formataddr(("Job Agent", sender))
    msg["To"] = sender                                   # to you, from you
    msg["Subject"] = " ".join(subject.split())          # (a posting title may carry a line break)
    msg["Message-ID"] = make_msgid(domain=sender.split("@", 1)[1])
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    return msg


class MailError(ValueError):
    pass


def send_summary(session: Session, *, preview: bool = False, now: datetime | None = None, sender=None) -> dict:
    """Build and send the summary to your own Gmail. A scheduled one is skipped on a day with nothing new;
    a preview ("Send me the summary now") always goes and doesn't move where the next one starts."""
    from app.modules.m3_outreach import gmail

    acct = gmail.account(session)
    if acct is None or acct.status != "ok":
        raise MailError("Gmail isn't connected (Settings → Gmail)")
    now = now or _now()
    summary = build(session, now)
    if summary.quiet and not preview:
        return {"sent": False, "why": "nothing new since the last summary"}
    subject = ("Preview · " if preview else "") + f"Job Agent: {summary.subject}"
    text, html = render(summary)
    (sender or gmail.send)(session, message(acct.email, subject, text, html))
    session.add(MailLog(kind="summary", preview=preview, subject=subject, counts=summary.counts, sent_at=now))
    audit.record(session, "m6", "summary_sent", f"Summary mail sent{' (preview)' if preview else ''}: {summary.subject}")
    return {"sent": True, "subject": subject, **summary.counts}
