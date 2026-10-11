"""Deterministic company checks — free, no AI. Each takes the fact card and returns a Check."""

import re
from dataclasses import asdict, dataclass

from app.config_schemas import CompanyRules
from app.modules.m1_companies.facts import UNKNOWN, value

# Which rules section each check belongs to (for targeted re-evaluation on rule changes).
SECTION_OF = {
    "avoid_list": "shared",
    "geography": "shared",
    "category_blocklist": "startup",
    "viability": "startup",
    "novelty": "startup",
    "brand": "established",
}

# Feeder tags that mark a B2B business even when a blocklist keyword also appears
# ("e-commerce" on a tool that serves e-commerce companies is not an e-commerce company).
B2B_TAGS = ("b2b", "saas", "developer tools", "infrastructure", "enterprise", "api")


@dataclass
class Check:
    name: str
    outcome: str  # pass | fail
    reason: str
    on_unknown: bool = False  # decided by the rule's unknown-policy rather than a known fact
    permanent: bool = False

    @property
    def failed(self) -> bool:
        return self.outcome == "fail"

    def as_dict(self) -> dict:
        return asdict(self)


def _unknown_policy(name: str, policy: str, what: str) -> Check:
    return Check(name, "pass" if policy == "pass" else "fail", f"{what} unknown → {policy} (rule policy)", on_unknown=True)


def avoid_list(card: dict, rules: CompanyRules) -> Check:
    model = value(card, "business_model")
    if model in rules.shared.avoid_business_models:
        return Check("avoid_list", "fail", f"business model is {model.replace('_', ' ')} (avoid list)")
    return Check("avoid_list", "pass", f"business model: {model}")


def geography(card: dict, rules: CompanyRules) -> Check:
    signals = {k: value(card, k) for k in ("india_presence", "hires_remote_from_india", "sponsors_visa")}
    yes = [k for k, v in signals.items() if v == "yes"]
    if yes:
        return Check("geography", "pass", "reachable from India: " + ", ".join(k.replace("_", " ") for k in yes))
    if all(v == "no" for v in signals.values()):
        return Check("geography", "fail", "no India presence, no remote hiring from India, no visa sponsorship")
    return _unknown_policy("geography", rules.shared.geography_unknown, "India reachability")


def _keyword_hit(text: str, keywords: list[str]) -> str | None:
    for kw in keywords:
        if re.search(rf"(?<![a-z0-9]){re.escape(kw.lower())}(?![a-z0-9])", text):
            return kw
    return None


def category_blocklist(card: dict, rules: CompanyRules, *, feeder_tags: list[str]) -> Check:
    """Keyword match on feeder tags first (no AI at all), then the website-reading step's classification."""
    tags = " | ".join(t.lower() for t in feeder_tags)
    if tags and not _keyword_hit(tags, list(B2B_TAGS)):
        for category, keywords in rules.startup.category_blocklist.items():
            if kw := _keyword_hit(tags, keywords):
                return Check("category_blocklist", "fail", f"{category.replace('_', ' ')} (tag “{kw}”)", permanent=True)
    classified = value(card, "blocklisted_category")
    if classified not in (UNKNOWN, "none") and classified in rules.startup.category_blocklist:
        return Check("category_blocklist", "fail", f"{classified.replace('_', ' ')} (from website)", permanent=True)
    return Check("category_blocklist", "pass", "not a blocklisted category")


def viability(card: dict, rules: CompanyRules) -> Check:
    distress = value(card, "distress")
    if distress == "yes":
        detail = value(card, "distress_detail")
        return Check("viability", "fail", f"distress signals: {detail}" if detail != UNKNOWN else "distress signals")
    hiring, raised = value(card, "actively_hiring"), value(card, "raised_within_window")
    if hiring == "yes" or raised == "yes":
        why = "actively hiring" if hiring == "yes" else f"raised within {rules.startup.funding_window_months} months"
        return Check("viability", "pass", why)
    if hiring == "no" and raised == "no":
        return Check("viability", "fail", f"not hiring and no funding in {rules.startup.funding_window_months} months")
    return _unknown_policy("viability", rules.startup.viability_unknown, "hiring/funding status")


def needs_news(card: dict) -> bool:
    """Search only when it could change the viability answer (saves the search budget)."""
    return value(card, "actively_hiring") != "yes" and value(card, "raised_within_window") == UNKNOWN
