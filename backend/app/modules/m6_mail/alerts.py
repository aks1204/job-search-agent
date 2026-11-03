"""The two real-time alerts (spec §6.2: nothing else may interrupt you): a fast-lane find, and someone
replying to your outreach. Each has its own switch (off until you turn it on). Checked every few minutes;
several at once arrive as one email; during quiet hours they wait until the morning. A role is alerted once,
and never while it's hidden by a wait after a rejection (`visible`)."""

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config_loader import get_config
from app.core import audit
from app.models import Company, JobCluster, JobPosting, JobScore, MailLog, Notification
from app.modules.m6_mail import switches
from app.modules.m6_mail.summary import CHIPS, STRIPES, Card, MailError, Section, Summary, _web, message, url

MAX_LOOKBACK = timedelta(days=2)          # a switch turned on long ago: never alert about old finds


def _now() -> datetime:
    return datetime.now(timezone.utc)


def quiet_now(now: datetime) -> bool:
    cfg = get_config().app
    t: time = now.astimezone(ZoneInfo(cfg.timezone)).time()
    start, end = cfg.quiet_hours.start, cfg.quiet_hours.end
    return (start <= t or t < end) if start > end else (start <= t < end)


def _alerted(session: Session) -> set[int]:
    ids: set[int] = set()
    for counts in session.scalars(select(MailLog.counts).where(MailLog.kind == "alert")).all():
        ids.update((counts or {}).get("fast") or [])
    return ids


def fast_finds(session: Session, since: datetime) -> list[tuple]:
    from app.api.jobs import listed_query
    from app.modules.m3_outreach.reopening import visible

    q = (listed_query(JobScore, JobPosting, Company, JobCluster)
         .where(JobScore.band == "fast_lane", JobScore.banded_at > since,
                JobScore.owner_action.is_(None) | (JobScore.owner_action == "act"))
         .order_by(JobScore.score.desc()))
    done = _alerted(session)
    return [r for r in visible(session, session.execute(q).all()) if r[0].cluster_id not in done]


def replies(session: Session, since: datetime) -> list[Notification]:
    return session.scalars(select(Notification).where(Notification.kind == "ping", Notification.delivered_at.is_(None),
                                                      Notification.created_at > since)
                           .order_by(Notification.created_at)).all()


def send_alerts(session: Session, *, now: datetime | None = None, sender=None) -> dict:
    from app.modules.m3_outreach import gmail
    from app.web.pages import _ping_lines, _private, ping_target, templates

    now = now or _now()
    fast_on, reply_on = switches.on_since("fast"), switches.on_since("reply")
    if not fast_on and not reply_on:
        return {"sent": False, "why": "alerts are off"}
    if quiet_now(now):
        return {"sent": False, "why": "quiet hours: they wait until the morning"}
    finds = fast_finds(session, max(fast_on, now - MAX_LOOKBACK)) if fast_on else []
    notes = replies(session, max(reply_on, now - MAX_LOOKBACK)) if reply_on else []
    if not finds and not notes:
        return {"sent": False, "why": "nothing to tell you"}
    acct = gmail.account(session)
    if acct is None or acct.status != "ok":
        raise MailError("Gmail isn't connected (Settings → Gmail)")

    cards = []
    for n in notes:
        lines = _ping_lines(n.detail)
        buttons = [("Open in Gmail →", n.link)] if _web(n.link) else []
        buttons.append(("Open in the app", url(ping_target(session, n) or "/pings")))
        cards.append(Card(_private(n.title), lines[0] if lines else "", "reply", "red", buttons[:2]))
    for sc, j, c, cl in finds:
        buttons = [("Open apply link →", j.url)] if _web(j.url) else []
        buttons.append(("Open role", url(f"/roles/{cl.id}")))
        cards.append(Card(f"{cl.title} · {c.name}", "A fast-lane find: it's on your Pursuing tab. Once people to email are found, "
                          "an outreach draft waits on Review for you (never sent without you).",
                          f"{sc.score} · fast lane", "blue", buttons[:2]))
    if len(cards) == 1:
        subject = (f"New fast-lane role: {finds[0][3].title} · {finds[0][2].name}" if finds else _private(notes[0].title))
    else:
        subject = f"{len(cards)} alerts: " + ", ".join(
            [f"{len(notes)} repl{'ies' if len(notes) != 1 else 'y'}"] * bool(notes)
            + [f"{len(finds)} fast-lane role{'s' if len(finds) != 1 else ''}"] * bool(finds))
    summary = Summary(subject, [Section("alerts", "Right now", cards)], "", {}, False, now)
    html = templates.env.get_template("mail/summary.html").render(
        s=summary, stripes=STRIPES, chips=CHIPS, app_url=url("/"), settings_url=url("/settings"), kicker="ALERT")
    text = "\n".join([subject, ""] + [f"- {c.title}\n  {c.detail}\n" + "\n".join(f"  {a} {h}" for a, h in c.buttons)
                                      for c in cards])
    (sender or gmail.send)(session, message(acct.email, f"Job Agent: {subject}", text, html))
    for n in notes:
        n.delivered_at = now
    counts = {"fast": [sc.cluster_id for sc, *_ in finds], "replies": [n.id for n in notes]}
    session.add(MailLog(kind="alert", subject=subject, counts=counts, sent_at=now))
    audit.record(session, "m6", "alert_sent", f"Alert sent: {subject}")
    return {"sent": True, "subject": subject, "fast": len(finds), "replies": len(notes)}
