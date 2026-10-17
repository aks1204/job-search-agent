"""People's names → address local parts, and back (learning a domain's pattern from one real address)."""

import re
import unicodedata

# Words that ride along in names on public profiles but aren't part of the name.
_HONORIFICS = {"mr", "mrs", "ms", "miss", "dr", "prof", "shri", "smt", "sri", "er", "ca", "cfa", "mba", "phd",
               "pmp", "shrm", "cpa", "jr", "sr", "ii", "iii"}


def ascii_fold(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()


def name_tokens(full_name: str) -> list[str]:
    """Lower-case ASCII tokens of a person's name, without honorifics/credentials or bracketed bits."""
    s = re.sub(r"\(.*?\)|\[.*?\]", " ", full_name)
    s = s.split(",")[0]  # "Priya Sharma, SHRM-CP"
    tokens = [re.sub(r"[^a-z]", "", t) for t in ascii_fold(s).lower().replace("-", " ").split()]
    return [t for t in tokens if t and t not in _HONORIFICS]


def split_name(full_name: str) -> tuple[str, str] | None:
    """(first, last) — first and last tokens; None when there aren't two (can't build an address)."""
    tokens = name_tokens(full_name)
    if len(tokens) < 2:
        return None
    return tokens[0], tokens[-1]


def name_key(full_name: str) -> str:
    parts = split_name(full_name)
    return " ".join(parts) if parts else " ".join(name_tokens(full_name))


def render(pattern: str, first: str, last: str) -> str:
    return pattern.format(first=first, last=last, f=first[:1], l=last[:1])


# Every format we know how to recognise, most common first (ambiguous single-token locals resolve
# to the earlier entry).
KNOWN_PATTERNS = ("{first}.{last}", "{first}{last}", "{f}{last}", "{first}_{last}", "{f}.{last}", "{first}",
                  "{last}.{first}", "{first}{l}", "{last}{f}", "{first}-{last}", "{last}", "{last}{first}",
                  "{first}.{l}")


def infer_pattern(email: str, full_name: str) -> str | None:
    """Which format produced this real address for this person? None if it isn't name-shaped."""
    parts = split_name(full_name)
    if not parts or "@" not in email:
        return None
    local = email.split("@", 1)[0].lower()
    first, last = parts
    for pattern in KNOWN_PATTERNS:
        if render(pattern, first, last) == local:
            return pattern
    return None  # e.g. "priya.k.sharma": name-shaped but not reproducible from first+last — don't learn it


ROLE_LOCALS = {"jobs", "careers", "career", "hr", "recruiting", "recruitment", "talent", "hiring", "people",
               "info", "hello", "contact", "team", "apply", "resume", "resumes", "cv", "work", "joinus"}


def is_role_address(email: str) -> bool:
    return email.split("@", 1)[0].lower().replace(".", "").replace("-", "") in ROLE_LOCALS
