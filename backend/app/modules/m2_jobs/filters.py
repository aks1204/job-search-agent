"""Kind-1 hard filters (spec §2.1), free and deterministic, applied before any AI cost.

The title is a hint, not the truth: this is only a broad net to kill obvious noise.
What a role really is (family, rung, years asked for) is read from the description
by the AI in Part 2b.
"""

import re
from dataclasses import dataclass
from datetime import datetime, timezone

from app.config_schemas import JobRules, OwnerStub

# Words that say nothing about the kind of work (seniority, connectors, filler).
_STOP = {
    "senior", "sr", "junior", "jr", "lead", "head", "principal", "staff", "chief", "associate", "assistant",
    "manager", "director", "vp", "i", "ii", "iii", "iv", "of", "and", "the", "a", "an", "for", "to", "in",
    "office", "team", "global", "specialist", "executive",
}


def stem(word: str) -> str:
    w = word.lower().removesuffix("'s").removesuffix("s'")
    if len(w) > 5 and w.endswith("ing"):
        return w[:-3]
    if len(w) > 4 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w


def stems(text: str) -> list[str]:
    return [stem(w) for w in re.findall(r"[a-z0-9']+", (text or "").lower()) if w.strip("'")]


def _contains_phrase(title_stems: list[str], phrase_stems: list[str]) -> bool:
    n = len(phrase_stems)
    return n > 0 and any(title_stems[i:i + n] == phrase_stems for i in range(len(title_stems) - n + 1))


@dataclass(frozen=True)
class TitleNet:
    include: frozenset[str]          # any of these word stems keeps a title in the net
    exclude: tuple[tuple[str, ...], ...]  # any of these stem phrases drops it

    @classmethod
    def build(cls, owner: OwnerStub | None, rules: JobRules) -> "TitleNet":
        include: set[str] = set()
        exclude: list[tuple[str, ...]] = []
        if owner:
            for phrase in [*owner.target_roles, *owner.role_families.progression, *owner.role_families.lateral]:
                include |= {s for s in stems(phrase) if s not in _STOP and len(s) > 2}
            exclude += [tuple(stems(p)) for p in owner.role_families.excluded]
        exclude += [tuple(stems(p)) for p in rules.kind1.noise_titles]
        return cls(frozenset(include), tuple(e for e in exclude if e))

    def check(self, title: str) -> str | None:
        """None when the title is in the net, else the reason it isn't."""
        title_stems = stems(title)
        for phrase in self.exclude:
            if _contains_phrase(title_stems, list(phrase)):
                return f"excluded title: “{' '.join(phrase)}”"
        if self.include and not (set(title_stems) & self.include):
            return "title outside your role families"
        return None


def kind1(job, net: TitleNet, rules: JobRules, now: datetime | None = None) -> list[str]:
    """Reasons a posting fails the hard filters (empty list = passes)."""
    now = now or datetime.now(timezone.utc)
    reasons = []
    if why := net.check(job.title):
        reasons.append(why)
    if job.employment_type != "unknown" and job.employment_type not in rules.kind1.allowed_employment_types:
        reasons.append(f"{job.employment_type.replace('_', '-')} role")
    basis = job.posted_at or job.first_seen_at
    if basis is not None:
        age_days = (now - basis).days
        if age_days > rules.kind1.max_age_days:
            reasons.append(f"posted {age_days} days ago (limit {rules.kind1.max_age_days})")
    return reasons
