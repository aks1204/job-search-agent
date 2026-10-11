"""The evaluator (spec §1.4).

Order: free checks on what scouts already know → enrich → shared checks → profile
checks → at most one AI judgment. Output: pass/fail + structured reasons, logged to
the audit trail, re-runnable as f(fact card, rules version, precedents).
"""

import json
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.config_loader import get_config
from app.core import audit
from app.core.http import PoliteClient
from app.core.memory import search as memory_search
from app.core.search import search_available
from app.models.company import STATUS_BLOCKED, STATUS_PASSED, STATUS_REJECTED, Company
from app.models.verdict import CompanyVerdict
from app.modules.m1_companies import checks as C
from app.modules.m1_companies.enrich import enrich, news_signals
from app.modules.m1_companies.facts import UNKNOWN, apply_feeder_facts, fact_card, value

PRECEDENTS_K = 3
PRECEDENT_MIN_SIMILARITY = 0.65


class NoveltyJudgment(BaseModel):
    verdict: Literal["novel", "borderline", "clone"]
    reasoning: str = Field(description="2-4 plain sentences a non-expert can follow.")
    one_liner: str = Field(description="What the company does, at most 15 words.")


class BrandJudgment(BaseModel):
    direction: Literal["up", "lateral", "down", "unknown"]
    reasoning: str = Field(description="2-4 plain sentences comparing with the owner's employers.")
    one_liner: str = Field(description="What the company does, at most 15 words.")


def _novelty_system(rules) -> str:
    return (
        "You judge whether a startup is worth watching for a job seeker, on NOVELTY only.\n"
        "Other checks (viability, geography, category blocklist) have already passed.\n\n"
        f"Rule: {rules.startup.novelty_guidance}\n\n"
        "Owner rulings on similar companies (precedents) may be included — follow their spirit.\n"
        "Answer novel, borderline or clone, in the requested JSON format."
    )


def _brand_system(rules, owner) -> str:
    history = "\n".join(
        f"- {j.title} at {j.company} ({j.start} – {j.end or 'present'})" for j in owner.employment_history
    )
    return (
        "You judge whether an established company would be a step UP, LATERAL or DOWN for this job seeker's career.\n\n"
        f"Career field: {owner.career_field}\n"
        f"Current role: {owner.current_role.title} at {owner.current_role.company} (since {owner.current_role.started})\n"
        f"Employment history:\n{history}\n\n"
        f"Rule: {rules.established.guidance}\n\n"
        "Owner rulings on similar companies (precedents) may be included — follow their spirit.\n"
        "If you don't know the company well enough, answer unknown. Use the requested JSON format."
    )


def _card_text(company: Company, card: dict) -> str:
    lines = [f"Company: {company.name}", f"Domain: {company.domain}"]
    for key in sorted(card):
        fact = card[key]
        if fact["value"] != UNKNOWN:
            lines.append(f"- {key}: {json.dumps(fact['value'], ensure_ascii=False)} ({fact['confidence']}, {fact['source']})")
    return "\n".join(lines)


def _precedents(session: Session, company: Company, card: dict, profile: str) -> list:
    query = f"{company.name}: {value(card, 'what_they_build')}"
    hits = memory_search(session, "precedent", query, k=PRECEDENTS_K * 3)
    same_profile = [
        (e, sim) for e, sim in hits
        if (e.context or {}).get("module") == "m1" and (e.context or {}).get("profile") == profile
        and sim >= PRECEDENT_MIN_SIMILARITY
    ]
    return [e for e, _ in same_profile[:PRECEDENTS_K]]


def _profile_for(company: Company, card: dict) -> str:
    if company.type in ("startup", "established"):
        return company.type
    stage = value(card, "company_stage")
    if stage in ("startup", "established"):
        return stage
    if value(card, "employee_estimate") in ("1001-10000", "10000+"):
        return "established"
    return "startup"


def _feeder_tags(company: Company) -> list[str]:
    yc = (company.source_data or {}).get("yc") or {}
    tags = []
    for key in ("industry", "subindustry", "industries", "tags"):
        v = yc.get(key)
        tags.extend(v if isinstance(v, list) else [v] if v else [])
    return [str(t) for t in tags]


def evaluate(
    session: Session, company: Company, *, llm, http: PoliteClient, trigger: str = "new"
) -> CompanyVerdict:
    cfg = get_config()
    rules = cfg.rules
    checks: list[C.Check] = []
    judgment: dict = {}
    precedent_ids: list[int] = []
    spent: list[float] = []

    def track(result):
        spent.append(result.cost_usd)
        return result.parsed

    class _Tracking:
        """Wraps the LLM so every call made during this evaluation is costed against the verdict."""

        def structured(self, *a, **kw):
            res = llm.structured(*a, **kw)
            spent.append(res.cost_usd)
            return res

    tracked_llm = _Tracking()

    def decide(passed: bool, profile: str, summary: str, failed: C.Check | None = None) -> CompanyVerdict:
        verdict = CompanyVerdict(
            company_id=company.id, passed=passed, profile=profile,
            failed_check=failed.name if failed else None, summary=summary,
            checks=[c.as_dict() for c in checks], judgment=judgment,
            permanent=bool(failed and failed.permanent), trigger=trigger, decided_by="machine",
            rules_versions={s: cfg.rules_section_version(s) for s in ("shared", "startup", "established")},
            precedent_ids=precedent_ids, cost_usd=round(sum(spent), 6),
        )
        session.add(verdict)
        session.flush()
        company.status = STATUS_PASSED if passed else (STATUS_BLOCKED if verdict.permanent else STATUS_REJECTED)
        company.reeval_reason = None
        if company.type == "unknown":
            company.type, company.type_source = profile, "evaluator"
        audit.record(
            session, "m1", "company_verdict",
            f"{'Kept' if passed else 'Rejected'} {company.name}: {summary}",
            subject_type="company", subject_id=str(company.id), is_judgment=True,
            rules_version=cfg.file_version("company_rules"),
            detail={"verdict_id": verdict.id, "profile": profile, "trigger": trigger,
                    "checks": verdict.checks, "judgment": judgment, "cost_usd": verdict.cost_usd},
        )
        return verdict

    # 1 — free: what the scouts already know.
    apply_feeder_facts(session, company)
    card = fact_card(session, company.id)
    profile = _profile_for(company, card)
    if profile == "startup":
        c = C.category_blocklist(card, rules, feeder_tags=_feeder_tags(company))
        if c.failed:
            checks.append(c)
            return decide(False, profile, c.reason, c)

    # 2 — enrich (one cheap AI read), then re-derive the profile from richer facts.
    enrich(session, company, llm=tracked_llm, http=http, profile=profile)
    card = fact_card(session, company.id)
    profile = _profile_for(company, card)

    # 3 — shared checks.
    for check in (C.avoid_list(card, rules), C.geography(card, rules)):
        checks.append(check)
        if check.failed:
            return decide(False, profile, check.reason, check)

    # 4 — profile checks, deterministic first.
    if profile == "startup":
        c = C.category_blocklist(card, rules, feeder_tags=_feeder_tags(company))
        checks.append(c)
        if c.failed:
            return decide(False, profile, c.reason, c)
        if C.needs_news(card) and search_available():
            news_signals(session, company, llm=tracked_llm, http=http,
                         window_months=rules.startup.funding_window_months,
                         monthly_budget=rules.evaluation.search_monthly_budget)
            card = fact_card(session, company.id)
        c = C.viability(card, rules)
        checks.append(c)
        if c.failed:
            return decide(False, profile, c.reason, c)

    # 5 — the one AI judgment, with owner precedents as examples.
    precedents = _precedents(session, company, card, profile)
    precedent_ids.extend(p.id for p in precedents)
    precedent_text = (
        "\n\nOwner precedents:\n" + "\n".join(f"- {p.meaning}" for p in precedents) if precedents else ""
    )
    subject = ("company", str(company.id))

    if profile == "startup":
        j = track(llm.structured(
            "judge", system=_novelty_system(rules), schema=NoveltyJudgment, purpose="m1.judge_novelty",
            user=_card_text(company, card) + precedent_text, session=session, subject=subject,
        ))
        judgment.update(j.model_dump())
        passed = j.verdict == "novel" or (j.verdict == "borderline" and rules.startup.novelty_borderline == "pass")
        c = C.Check("novelty", "pass" if passed else "fail", f"{j.verdict}: {j.reasoning}")
    else:
        owner = cfg.owner
        if owner is None:
            raise RuntimeError("config/owner_stub.yaml is required to judge established companies")
        j = track(llm.structured(
            "judge", system=_brand_system(rules, owner), schema=BrandJudgment, purpose="m1.judge_brand",
            user=_card_text(company, card) + precedent_text, session=session, subject=subject,
        ))
        judgment.update(j.model_dump())
        if j.direction == "unknown":
            passed = rules.established.unknown == "pass"
        else:
            passed = j.direction in rules.established.keep
        c = C.Check("brand", "pass" if passed else "fail", f"{j.direction}: {j.reasoning}", on_unknown=j.direction == "unknown")
    checks.append(c)

    summary = f"{j.one_liner} — {c.reason}"
    return decide(passed, profile, summary, None if passed else c)
