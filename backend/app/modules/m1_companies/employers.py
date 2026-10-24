"""Owner rule: your current and past employers are never watched or contacted.

Who they are comes from your profile (current role + job history), so a new job on your resume joins the
list by itself. Each employer's website is found once — a company the agent already knows by that name,
else one AI guess that must fit the name and either be a website the agent already knows or carry the name
on its homepage — because the name alone can miss: ZS is stored under its site title "Strategy &
Transformation Consulting", but its website zs.com matches. Without a website the match falls back to the
exact name (never for generic one-word names like "Global", and never when both sides have different
websites), and you're asked to set the website yourself.

A matched company is blocked, its unsent emails are cancelled, its roles leave your lists, and you get one
note so a wrong match is easy to spot. Nothing automatic ever un-blocks it. To correct a website:
`employers fix "<name>" <domain>` — only a company blocked under the old, wrong website goes back for a
fresh look. To keep one employer anyway: `override <domain> keep "<reason>"` after it was blocked.
"""

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config_loader import get_config
from app.core import audit
from app.core.hosts import domain_label, is_junk, normalize_domain
from app.core.notify import notify
from app.models.company import STATUS_BLOCKED, STATUS_CANDIDATE, Company
from app.models.employer import OwnerEmployer
from app.models.verdict import CompanyVerdict
from app.modules.m1_companies.identity import normalize_name

log = logging.getLogger(__name__)

CHECK = "former_employer"
TRIGGER = "employer_rule"
RECHECK_AFTER = timedelta(days=30)      # an employer whose website wasn't found is looked up again monthly

# Job-history entries that aren't an employer at all.
NOT_EMPLOYERS = {"self employed", "freelance", "freelancer", "freelancing", "stealth", "stealth startup",
                 "stealth mode startup", "confidential", "career break", "sabbatical", "various", "independent",
                 "independent consultant", "unemployed", "student", "none", "na", "n a", "personal project",
                 "personal projects", "own business", "family business"}
# One-word names too common to match on the name alone ("Global Technologies Ltd" → "global").
GENERIC = {"global", "tech", "digital", "data", "analytics", "consulting", "solutions", "capital", "first",
           "general", "united", "american", "national", "international", "current", "previous", "company",
           "systems", "services", "partners", "ventures", "group", "media", "health", "finance", "bank",
           "insurance", "energy", "labs", "studio", "agency", "networks", "software", "cloud", "ai"}


@dataclass
class Employer:
    name: str
    normalized: str
    current: bool
    domain: str | None

    @property
    def why(self) -> str:
        return "your current employer" if self.current else "a past employer"

    @property
    def name_matchable(self) -> bool:
        return " " in self.normalized or self.normalized not in GENERIC


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _from_profile() -> list[tuple[str, bool]]:
    """(name, is current) for every employer in your profile, one per company. Nothing from a sample profile."""
    owner = get_config().owner
    if owner is None or owner.placeholder_fields():
        return []
    out: dict[str, tuple[str, bool]] = {}
    current_key = normalize_name(owner.current_role.company)
    for name, is_current in [(owner.current_role.company, True),
                             *((j.company, j.end is None) for j in owner.employment_history)]:
        key = normalize_name(name)
        if not key or key in NOT_EMPLOYERS or key.startswith(("stealth", "self employed")):
            continue
        is_current = is_current or key == current_key
        if key not in out or (is_current and not out[key][1]):
            out[key] = (name.strip(), is_current)
    return list(out.values())


def employers(session: Session) -> list[Employer]:
    rows = {r.normalized: r for r in session.scalars(select(OwnerEmployer)).all()}
    out = []
    for name, current in _from_profile():
        key = normalize_name(name)
        row = rows.get(key)
        out.append(Employer(name, key, current, row.domain if row else None))
    return out


def _latest(session: Session, company_id: int) -> CompanyVerdict | None:
    return session.scalars(select(CompanyVerdict).where(CompanyVerdict.company_id == company_id)
                           .order_by(CompanyVerdict.created_at.desc(), CompanyVerdict.id.desc()).limit(1)).first()


def _owner_kept(session: Session, company: Company) -> bool:
    """You kept this company yourself AFTER the rule blocked it — your exception wins. (A keep from before
    the rule, for another reason, doesn't count: you'd be told it was blocked and can keep it again.)"""
    last_block = session.scalar(select(func.max(CompanyVerdict.id)).where(
        CompanyVerdict.company_id == company.id, CompanyVerdict.failed_check == CHECK))
    if last_block is None:
        return False
    yours = session.scalars(select(CompanyVerdict).where(   # your latest say since then (later machine re-checks don't undo it)
        CompanyVerdict.company_id == company.id, CompanyVerdict.decided_by == "owner",
        CompanyVerdict.id > last_block).order_by(CompanyVerdict.id.desc()).limit(1)).first()
    return yours is not None and yours.passed


def _which(company: Company, known: list[Employer]) -> Employer | None:
    if company.domain:
        for e in known:
            if e.domain and e.domain == company.domain:
                return e
    names = {company.normalized_name, *(normalize_name(a) for a in (company.aliases or []))}
    for e in known:
        if not e.name_matchable or e.normalized not in names:
            continue
        if e.domain and company.domain and e.domain != company.domain:
            continue                         # same name, different websites: a different company
        return e
    return None


def match(session: Session, company: Company, known: list[Employer] | None = None) -> Employer | None:
    """The employer this company is, if any — by website first, then by name (or any name it was seen as)."""
    known = employers(session) if known is None else known
    if not known:
        return None
    e = _which(company, known)
    return None if e is None or _owner_kept(session, company) else e


# --- finding each employer's website --------------------------------------------------------------------

def _fit(name: str, domain: str) -> str | None:
    """How the website's name fits the employer's: 'full' (acme.com ↔ Acme, mckinsey.com ↔ McKinsey &
    Company, bms.com ↔ Bristol Myers Squibb), 'first' (zs.com ↔ ZS Associates) or None."""
    label = domain_label(domain).replace("-", "")
    words = normalize_name(name).split()
    if not words or not label:
        return None
    if label == "".join(words) or (len(words) > 1 and label == "".join(w[0] for w in words)):
        return "full"
    if len(words) > 1 and label == words[0] and words[0] not in GENERIC:   # never american.com ↔ American X
        return "first"
    return None


def _site_text(http, domain: str) -> str | None:
    from app.modules.m1_companies.enrich import fetch_site_text

    if http is None:
        return None
    site = fetch_site_text(http, domain)
    return site[0] if site else None




def _plain(text: str) -> str:
    """'Bristol-Myers Squibb®' → ' bristol myers squibb ' — so whole-word checks ignore punctuation."""
    return " " + re.sub(r"[^a-z0-9]+", " ", text[:20000].lower()) + " "


def _says(text: str, words: str) -> bool:
    return f" {words} " in _plain(text)


def _ai_domain(session: Session, llm, http, name: str) -> tuple[str | None, bool]:
    """(website, answered). One AI guess — only the employer's name is sent. The website must fit the name,
    and then either already belong to a company the agent knows (ZS is known as zs.com) or carry the name on
    its homepage (so zs.com must say "ZS"). A guess that only fits the first word needs high confidence.
    `answered` is False when the AI or the site couldn't be reached — then it's tried again next run."""
    from app.core.llm import LLMError
    from app.modules.m1_companies.resolver import GUESS_SYSTEM, DomainGuess

    try:
        guess = llm.structured(
            "fast", system=GUESS_SYSTEM, schema=DomainGuess, purpose="m1.employer_domain",
            user=f"Company name: {name}\nSeen as:\n- an employer in a job seeker's own work history",
            session=session,
        ).parsed
    except LLMError:
        return None, False
    domain = normalize_domain(guess.domain)
    if guess.confidence == "low" or not domain or is_junk(domain):
        return None, True
    fit = _fit(name, domain)
    if fit is None:
        return None, True
    if session.scalar(select(Company.id).where(Company.domain == domain).limit(1)) is not None:
        return domain, True                    # fits the name AND is a website the agent already knows
    if fit == "first" and guess.confidence != "high":
        return None, True
    text = _site_text(http, domain)
    if text is None:
        return None, False
    words = normalize_name(name).split()
    return (domain if _says(text, " ".join(words) if fit == "full" else words[0]) else None), True


def _known_domain(session: Session, e: Employer) -> str | None:
    """A company the agent already knows under exactly this name (or alias) — only if there's exactly one
    such website and it fits the name ('Nova Labs' and 'Nova Technologies' are both 'nova')."""
    if not e.name_matchable:
        return None
    found = set(session.scalars(select(Company.domain).where(Company.normalized_name == e.normalized,
                                                             Company.domain.is_not(None))))
    for c in session.scalars(select(Company).where(Company.domain.is_not(None),
                                                   func.jsonb_array_length(Company.aliases) > 0)).all():
        if any(normalize_name(a) == e.normalized for a in c.aliases or []):
            found.add(c.domain)
    if len(found) != 1:
        return None
    domain = found.pop()
    return domain if _fit(e.name, domain) is not None else None


ASK_AFTER = 3   # lookups without an answer before you're asked to set the website yourself


def _ask_once(session: Session, row: OwnerEmployer) -> None:
    if row.told_at is not None:
        return
    row.told_at = _now()
    notify(session, "needs_you", "needs_you", f"Couldn't find the website of your employer {row.name}",
           detail=f'Until then it is matched by its exact name only. Set it: python -m app.cli employers fix '
                  f'"{row.name}" <website, e.g. zs.com>', subject=("owner_employer", row.normalized[:64]))


def refresh(session: Session, llm=None, http=None) -> dict:
    """Find websites for employers that don't have one yet, then apply the rule everywhere. Never un-blocks
    anything — only your own `employers fix` can do that."""
    out = {"employers": 0, "websites_found": 0, "blocked": 0}
    rows = {r.normalized: r for r in session.scalars(select(OwnerEmployer)).all()}
    for e in employers(session):
        out["employers"] += 1
        row = rows.get(e.normalized)
        if row is not None and (row.domain or row.source == "manual"
                                or (row.source == "none" and _now() - row.checked_at < RECHECK_AFTER)):
            continue
        if row is None:
            row = OwnerEmployer(normalized=e.normalized, name=e.name[:255], source="pending", attempts=0)
            session.add(row)
        domain, source, answered = _known_domain(session, e), "known", True
        if domain is None:
            if llm is None:
                domain, answered = None, False      # no AI key
            else:
                (domain, answered), source = _ai_domain(session, llm, http, e.name), "ai"
        row.checked_at = _now()
        if not answered:                             # the AI or the site was unreachable: again next run
            row.source, row.attempts = "pending", (row.attempts or 0) + 1
            if row.attempts >= ASK_AFTER:
                _ask_once(session, row)
            continue
        row.domain, row.source = domain, source if domain else "none"
        if domain:
            out["websites_found"] += 1
            audit.record(session, "m1", "employer_website", f"Your employer {e.name}: website {domain} ({source})",
                         is_judgment=source == "ai", detail={"name": e.name, "domain": domain, "source": source})
        else:
            _ask_once(session, row)
    session.flush()
    out["blocked"] = apply(session)
    return out


def fix(session: Session, name: str, domain: str | None) -> tuple[Employer | None, int]:
    """You set (or clear, with None) an employer's website: (the employer, companies released). Only names
    from your profile are accepted. A company blocked under the OLD website goes back for a fresh look.
    Raises ValueError for something that isn't a website."""
    key = normalize_name(name)
    if not any(normalize_name(n) == key for n, _ in _from_profile()):
        return None, 0
    clean = normalize_domain(domain) if domain else None
    if domain and clean is None:
        raise ValueError(f"“{domain}” isn't a website — e.g. zs.com")
    row = session.scalar(select(OwnerEmployer).where(OwnerEmployer.normalized == key))
    if row is None:
        row = OwnerEmployer(normalized=key, name=name[:255], source="manual")
        session.add(row)
    old = row.domain
    row.domain, row.source, row.checked_at = clean, "manual", _now()
    session.flush()
    audit.record(session, "m1", "employer_website", f"You set {name}'s website to {clean or 'none'}",
                 detail={"name": name, "domain": clean, "old_domain": old, "source": "manual"})
    released = _release(session, key, old) if old and old != clean else 0
    return next(e for e in employers(session) if e.normalized == key), released


def blocked_for(session: Session, e: Employer) -> list[Company]:
    """Companies currently blocked because they are this employer."""
    out = []
    for company in session.scalars(select(Company).where(Company.status == STATUS_BLOCKED)).all():
        latest = _latest(session, company.id)
        if latest is not None and latest.failed_check == CHECK and (latest.checks or [{}])[0].get("employer") == e.normalized:
            out.append(company)
    return out


def _release(session: Session, key: str, old_domain: str) -> int:
    """After you replaced a wrong website: companies blocked as this employer UNDER THAT OLD WEBSITE, which no
    longer match, go back for a fresh look."""
    known = employers(session)
    n = 0
    for company in session.scalars(select(Company).where(Company.status == STATUS_BLOCKED,
                                                         Company.domain == old_domain)).all():
        latest = _latest(session, company.id)
        if latest is None or latest.failed_check != CHECK or latest.decided_by != "machine":
            continue
        why = (latest.checks or [{}])[0]
        if why.get("employer") != key or why.get("domain") != old_domain or _which(company, known) is not None:
            continue
        company.status, company.reeval_reason = STATUS_CANDIDATE, "rules_change"
        audit.record(session, "m1", "company_requeued",
                     f"Re-evaluating {company.name}: you corrected your employer's website",
                     subject_type="company", subject_id=str(company.id), detail={"trigger": TRIGGER})
        n += 1
    return n



# --- applying the rule ----------------------------------------------------------------------------------

def block(session: Session, company: Company, e: Employer, *, trigger: str = TRIGGER) -> CompanyVerdict:
    """Never watched again: blocked, unsent emails cancelled, waiting apply-list entries skipped, one note."""
    from app.models.application import Application
    from app.modules.m3_outreach.campaigns import stop_company
    from app.modules.m4_apply.tracker import set_status

    reason = f"{e.why} ({e.name})"
    verdict = CompanyVerdict(
        company_id=company.id, passed=False, profile=company.type if company.type != "unknown" else "established",
        failed_check=CHECK, summary=f"Not watched: {reason}",
        checks=[{"name": CHECK, "outcome": "fail", "reason": reason, "on_unknown": False, "permanent": True,
                 "employer": e.normalized, "domain": e.domain}],
        judgment={}, permanent=True, trigger=trigger, decided_by="machine", rules_versions={}, precedent_ids=[],
    )
    session.add(verdict)
    company.status, company.reeval_reason = STATUS_BLOCKED, None
    cancelled = stop_company(session, company.id, f"not contacted: {reason}")
    skipped = 0
    for app in session.scalars(select(Application).where(Application.company_id == company.id,
                                                         Application.status == "queued")).all():
        skipped += set_status(session, app, "skipped", source="agent", summary=f"not applied: {reason}")
    session.flush()
    where = f" ({company.domain})" if company.domain else ""
    notify(session, "update", "happened", f"Not watching {company.name}{where}: {reason}",
           detail="Wrong match? python -m app.cli employers", subject=("company", str(company.id)))
    audit.record(session, "m1", "company_verdict", f"Rejected {company.name}: {reason} — never watched",
                 subject_type="company", subject_id=str(company.id),
                 detail={"verdict_id": verdict.id, "check": CHECK, "emails_cancelled": cancelled,
                         "apply_list_skipped": skipped})
    return verdict


def apply(session: Session) -> int:
    """Block every known company that is one of your employers (and not already blocked or decided by you)."""
    known = employers(session)
    if not known:
        return 0
    n = 0
    for company in session.scalars(select(Company).where(Company.status != STATUS_BLOCKED)).all():
        e = _which(company, known)
        if e is None or _owner_kept(session, company):
            continue
        latest = _latest(session, company.id)
        if latest is not None and latest.decided_by == "owner" and not latest.passed:
            continue                           # you already rejected it yourself
        block(session, company, e)
        n += 1
    return n
