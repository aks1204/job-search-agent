"""Part 6e: the Audit log, readable (spec §6.4). Every consequential decision: what the agent did, why, and
what it affected — in plain words, with the role or company it touched one click away. The raw record stays
under "More detail". Judgment calls (a company kept or dropped, a website found, a reply read) carry their
correction right on the entry; your correction is kept in Memory so the agent learns from it.

The Audit log never notifies you: nothing here reaches the mail, the email alerts or Pings."""

from datetime import datetime, timedelta, timezone

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app.models import AuditLog, Company, JobCluster, OutreachCampaign

# Areas = the filter buttons. `None` = everything else (scheduled upkeep, maintenance).
AREAS: dict[str, tuple[str, tuple[str, ...] | None]] = {
    "companies": ("Companies", ("m1",)), "jobs": ("Jobs", ("m2",)), "outreach": ("Outreach", ("m3",)),
    "applications": ("Applications", ("m4",)), "profile": ("Profile & memory", ("m5",)), "mail": ("Mail", ("m6",)),
    "housekeeping": ("Housekeeping", None),
}
MODULES = {m: label for label, mods in AREAS.values() if mods for m in mods}
WHEN = {"7": ("Last 7 days", 7), "30": ("Last 30 days", 30), "all": ("Everything", None)}
CONDENSED = "monthly_summary"           # one line standing for a month of old entries (see maintenance/audit_compact)

# What each kind of entry is, in a few words (anything not listed reads from its own name).
KINDS = {
    "company_verdict": "Judged a company", "verdict_overridden": "You corrected a company judgment",
    "domain_resolved": "Found a company's website", "company_corrected": "You corrected a company",
    "company_dropped": "Dropped a company", "company_merged": "Merged two records of one company",
    "company_requeued": "Queued a company to be judged again", "company_restored": "Restored a company",
    "employer_website": "Your employer's website", "feed_activated": "Set up a job feed",
    "feed_stale": "A job feed went quiet", "hn_rebuild": "Rebuilt the Hacker News companies",
    "name_confirmed": "You confirmed a company's name", "profile_site_repair": "Repaired profile-site websites",
    "brand_set": "Brand compared with your employer", "job_feedback": "Your reaction to a role",
    "job_feedback_undone": "You took back a reaction",
    "breaker_tripped": "Sending stopped by the safety brake", "breaker_reset": "Safety brake reset",
    "campaign_prepared": "Outreach drafted, to review", "campaign_armed": "Outreach began",
    "campaign_edited": "You edited an outreach email", "campaign_redrafted": "Outreach redrafted",
    "campaign_stopped": "Outreach stopped", "campaign_closed": "Outreach ended",
    "channel_designated": "Chose who to write to", "channel_override": "You chose who to write to",
    "contact_added": "Added a person to write to", "email_sent": "Sent an outreach email",
    "email_bounced": "An outreach email bounced", "email_undecided": "You took back an approval",
    "email_approve": "You approved an email", "email_skip": "You skipped a person",
    "sending_switch": "The Sending switch", "reply_resorted": "You re-sorted a reply",
    "marked_applied": "Marked as applied",
    "direction_changed": "Career direction changed", "direction_declined": "You declined a direction change",
    "direction_refreshed": "Career direction re-worked", "direction_memory_set_aside": "A memory set aside",
    "memory_undo": "You undid a memory change", "profile_confirmed": "Profile confirmed",
    "profile_edited": "You edited your profile", "profile_read": "Read your résumé",
    "alert_sent": "Sent an email alert", "mail_switch": "A mail switch", "summary_sent": "Sent the summary mail",
    CONDENSED: "A month, condensed",
}
REPLY_KINDS = {"interested": "interested", "forwarded": "forwarded it on", "question": "a question",
               "rejection": "a no", "ooo": "out of office", "redirect": "not the right person",
               "auto_other": "an automatic message", "unsure": "unclear"}
DETAIL_NAMES = {"cost_usd": "AI cost (USD)", "verdict_id": "Verdict no.", "precedent_id": "Memory entry no.",
                "thread_id": "Gmail thread", "message_id": "Gmail message"}
DETAIL_SKIP = ("checks", "judgment")        # long machine output: summarised as "why" instead
DETAIL_HIDE = ("pay", "salary", "ctc", "notice", "current_comp", "comp", "compensation")   # never shown (pay stays on Profile)


def kind_label(action: str) -> str:
    if action.startswith("reply_") and action != "reply_resorted":
        return "Read a reply"
    if action.startswith("memory_") and action not in KINDS:
        return "Memory: " + action.removeprefix("memory_").replace("_", " ")
    return KINDS.get(action) or action.replace("_", " ").capitalize()


def area_label(module: str) -> str:
    return MODULES.get(module, "Housekeeping")


def query(area: str = "all", when: str = "all", q: str = "", judgments: bool = False,
          now: datetime | None = None) -> Select:
    stmt = select(AuditLog)
    if area in AREAS:
        mods = AREAS[area][1]
        stmt = stmt.where(AuditLog.module.in_(mods) if mods else AuditLog.module.not_in(tuple(MODULES)))
    days = WHEN.get(when, WHEN["all"])[1]
    if days:
        stmt = stmt.where(AuditLog.created_at >= (now or datetime.now(timezone.utc)) - timedelta(days=days))
    if q.strip():
        like = "%" + q.strip()[:100].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        stmt = stmt.where(AuditLog.summary.ilike(like, escape="\\"))
    if judgments:
        stmt = stmt.where(AuditLog.is_judgment.is_(True))
    return stmt.order_by(AuditLog.created_at.desc(), AuditLog.id.desc())


def _why(a: AuditLog) -> str | None:
    d = a.detail or {}
    if a.action == CONDENSED:
        return "Entries over 12 months old are kept as one line per kind per month."
    if a.action == "company_verdict":
        failed = [c for c in (d.get("checks") or []) if isinstance(c, dict)
                  and str(c.get("outcome", "")).lower() in ("fail", "failed", "false")]
        rules = f" (version {a.rules_version})" if a.rules_version else ""
        if failed:
            c = failed[0]
            return f"Your company rules{rules}: {c.get('reason') or c.get('name', '').replace('_', ' ')}"
        return f"It passed every check in your company rules{rules}."
    if a.action == "domain_resolved":
        return ("An AI guess, then confirmed: the website names the company." if d.get("method")
                else "The best match in a web search for the company's name.")
    if a.action.startswith("reply_") and a.action != "reply_resorted":
        conf = d.get("confidence")
        sure = f", {conf:.0%} sure" if isinstance(conf, (int, float)) else ""
        return f"The AI read the reply as {REPLY_KINDS.get(d.get('category'), d.get('category'))}{sure}."
    for key in ("reason", "why"):
        if isinstance(d.get(key), str) and d[key].strip():
            return d[key]
    if a.summary.startswith("You "):
        return "You did this."
    return None


def _value(v) -> str:
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, float):
        return f"{v:.2f}".rstrip("0").rstrip(".")
    if isinstance(v, list):
        return ", ".join(_value(x) for x in v[:12]) + (f" … (+{len(v) - 12})" if len(v) > 12 else "") or "none"
    if isinstance(v, dict):
        return "; ".join(f"{k.replace('_', ' ')}: {_value(x)}" for k, x in list(v.items())[:8] if x not in (None, "", []))
    return str(v)


def _hidden(key: str) -> bool:
    k = key.lower()
    return k in DETAIL_HIDE or k.startswith(("pay_", "salary", "ctc", "current_comp", "comp_", "notice"))


def detail_lines(a: AuditLog) -> list[tuple[str, str]]:
    """The raw record, readable: one line per field, long machine output left out, pay never shown."""
    from app.web.pages import _private

    if a.action == CONDENSED:
        d = a.detail or {}
        return [("Month", str(d.get("month", ""))), ("Entries", str(d.get("count", "")))]
    out = []
    for k, v in (a.detail or {}).items():
        if k in DETAIL_SKIP or v in (None, "", [], {}) or _hidden(k):
            continue
        text = _private(_value(v))
        out.append((DETAIL_NAMES.get(k) or k.replace("_", " ").capitalize(), text[:300] + ("…" if len(text) > 300 else "")))
    if a.rules_version:
        out.append(("Rules version", a.rules_version))
    return out


def _subjects(session: Session, rows: list[AuditLog]) -> dict[tuple[str, str], tuple[str, str]]:
    """(subject type, id) → (what it is, where it opens). Read in one go per kind, not per entry."""
    ids: dict[str, set[int]] = {}
    for a in rows:
        if a.subject_type and (a.subject_id or "").isdigit():
            ids.setdefault(a.subject_type, set()).add(int(a.subject_id))
    out: dict[tuple[str, str], tuple[str, str]] = {}
    camps = {c.id: c for c in session.scalars(select(OutreachCampaign).where(
        OutreachCampaign.id.in_(list(ids.get("campaign", set())))))} if ids.get("campaign") else {}
    cluster_ids = ids.get("job_cluster", set()) | {c.cluster_id for c in camps.values()}
    clusters = {c.id: c for c in session.scalars(select(JobCluster).where(JobCluster.id.in_(list(cluster_ids))))} \
        if cluster_ids else {}
    company_ids = (ids.get("company", set()) | {c.company_id for c in clusters.values()}
                   | {c.company_id for c in camps.values()})
    contact_company = {}
    if ids.get("contact"):
        from app.models import Contact

        contact_company = dict(session.execute(select(Contact.id, Contact.company_id)
                                               .where(Contact.id.in_(list(ids["contact"])))).all())
        company_ids |= set(contact_company.values())
    names = dict(session.execute(select(Company.id, Company.name).where(Company.id.in_(list(company_ids)))).all()) \
        if company_ids else {}

    def role(cid):
        cl = clusters.get(cid)
        return (f"{cl.title} · {names.get(cl.company_id, '?')}", f"/roles/{cid}") if cl else None

    for i in ids.get("company", ()):
        if i in names:
            out[("company", str(i))] = (names[i], f"/companies/{i}")
    for i in ids.get("job_cluster", ()):
        if (r := role(i)):
            out[("job_cluster", str(i))] = r
    for i, c in camps.items():
        r = role(c.cluster_id)
        out[("campaign", str(i))] = ((f"Outreach for {r[0]}", r[1]) if r
                                     else (f"Outreach to {names.get(c.company_id, '?')}", f"/companies/{c.company_id}"))
    for i, company_id in contact_company.items():
        if company_id in names:
            out[("contact", str(i))] = (f"A person at {names[company_id]}", f"/companies/{company_id}")
    for a in rows:
        if a.subject_type == "profile":
            out[("profile", a.subject_id or "")] = ("Your profile", "/profile")
        elif a.subject_type == "memory":
            out[("memory", a.subject_id or "")] = ("Your memory", "/memory")
    return out


def _latest_verdicts(session: Session, rows: list[AuditLog]) -> set[int]:
    """The newest verdict entry of each company shown: only it offers Keep / Reject (an older one was replaced
    when the company was judged again)."""
    companies = {a.subject_id for a in rows if a.action == "company_verdict" and a.subject_id}
    if not companies:
        return set()
    return set(session.scalars(select(func.max(AuditLog.id)).where(
        AuditLog.action == "company_verdict", AuditLog.subject_type == "company",
        AuditLog.subject_id.in_(list(companies))).group_by(AuditLog.subject_id)).all())


def _fix(a: AuditLog, latest: set[int]) -> dict | None:
    """The correction this entry offers, if it's a judgment call you haven't corrected yet."""
    if not a.is_judgment or a.overridden_at or not (a.subject_id or "").isdigit():
        return None
    if a.action == "company_verdict":
        if a.id not in latest:
            return None
        kept = a.summary.startswith("Kept")
        return {"kind": "verdict", "company": int(a.subject_id), "to": "reject" if kept else "keep",
                "label": "Reject it instead" if kept else "Keep it instead"}
    if a.action == "domain_resolved":
        return {"kind": "website", "company": int(a.subject_id)}
    if a.action.startswith("reply_") and (a.detail or {}).get("thread_id"):
        return {"kind": "reply", "now": (a.detail or {}).get("category")}
    return None


def entries(session: Session, rows: list[AuditLog]) -> list[dict]:
    from app.web.pages import _private

    subjects, latest = _subjects(session, rows), _latest_verdicts(session, rows)
    out = []
    for a in rows:
        if a.overridden_at:
            state = ("ch-gray", "corrected by you")
        elif a.is_judgment:
            state = ("ch-amber", "judgment call")
        elif a.action == CONDENSED:
            state = ("ch-gray", "condensed")
        else:
            state = None
        out.append({"id": a.id, "at": a.created_at, "area": area_label(a.module), "kind": kind_label(a.action),
                    "summary": _private(a.summary), "why": _private(w) if (w := _why(a)) else None,
                    "affected": subjects.get((a.subject_type or "", a.subject_id or "")),
                    "state": state, "fix": _fix(a, latest), "lines": detail_lines(a), "overridden_at": a.overridden_at})
    return out
