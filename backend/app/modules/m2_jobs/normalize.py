"""One canonical shape for every source's job data (spec §2.2): dates, employment type, location, pay."""

import re
import unicodedata
from datetime import datetime, timedelta, timezone

from app.modules.m1_companies.facts import _INDIA

# --- dates ----------------------------------------------------------------------

_RELATIVE = re.compile(r"posted\s+(today|yesterday|(\d+)\+?\s+days?\s+ago)", re.I)


def parse_posted(value, first_seen: datetime) -> datetime | None:
    """ISO strings, dates, epochs (s or ms) and Workday's 'Posted 3 Days Ago' / 'Posted 30+ Days Ago'."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        seconds = value / 1000 if value > 1e11 else value
        return datetime.fromtimestamp(seconds, timezone.utc)
    text = str(value).strip()
    if text.isdigit():
        return parse_posted(int(text), first_seen)
    m = _RELATIVE.search(text)
    if m:
        word = m.group(1).lower()
        days = 0 if word == "today" else 1 if word == "yesterday" else int(m.group(2)) + (1 if "+" in text else 0)
        return first_seen - timedelta(days=days)
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# --- employment type --------------------------------------------------------------

_TYPES = [
    ("internship", re.compile(r"\bintern(ship)?s?\b|\btrainee\b|\bapprentice", re.I)),
    ("part_time", re.compile(r"part[\s_-]?time", re.I)),
    ("contract", re.compile(r"\bcontract(or|ual)?\b|\bfreelance\b|fixed[\s_-]?term|\bconsultant \(contract\)", re.I)),
    ("temporary", re.compile(r"\btemp(orary)?\b|\bseasonal\b", re.I)),
    ("full_time", re.compile(r"full[\s_-]?time|fulltime|\bpermanent\b|\bregular\b", re.I)),
]


def employment_type(raw: str | None, title: str = "") -> str:
    """full_time | part_time | contract | internship | temporary | unknown (the AI read confirms unknowns)."""
    for kind, pattern in _TYPES:
        if raw and pattern.search(raw):
            return kind
    for kind, pattern in _TYPES[:4]:  # titles only reliably say "Intern", "(Contract)", "Part-time"
        if pattern.search(title or ""):
            return kind
    return "unknown"


# --- location ---------------------------------------------------------------------

# "IN:" (Workday) and "(IN)" (Work at a Startup) mean India. A bare ", IN" does NOT: in US addresses it's
# Indiana ("Indianapolis, IN" — Lilly's own HQ).
_INDIA_CODE = re.compile(r"(^|\s)IN:|\(IN\)|\bIND\b")
_REMOTE = re.compile(r"\bremote\b|work from home|\bwfh\b|\banywhere\b", re.I)


def location_flags(location: str | None) -> tuple[bool, bool]:
    """(mentions India, mentions remote)."""
    text = location or ""
    return bool(_INDIA.search(text) or _INDIA_CODE.search(text)), bool(_REMOTE.search(text))


def location_key(location: str | None) -> str:
    """The first named place, used in the dedupe fingerprint ('IN: Lilly Bengaluru; US…' → 'in lilly bengaluru')."""
    if not location:
        return ""
    first = re.split(r"[;|/]| or ", location)[0]
    return _norm_text(first)[:80]


# --- titles & fingerprints ------------------------------------------------------------

_REQ_ID = re.compile(r"\b(r|jr|req)[-_ ]?\d{4,}\b|\(m/f/d\)|\(f/m/d\)|\(m/w/d\)", re.I)


def _norm_text(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def normalize_title(title: str) -> str:
    return _norm_text(_REQ_ID.sub(" ", title or ""))


def fingerprint(title: str, location: str | None) -> str:
    """Same company + same normalised title + same first location = the same role (siblings cluster)."""
    return f"{normalize_title(title)}|{location_key(location)}"[:300]


# --- pay ------------------------------------------------------------------------------

_CURRENCY = [
    ("INR", re.compile(r"₹|\binr\b|\brs\.?|\blpa\b|\blakhs?\b|\blacs?\b|\bcrores?\b|\bcr\b", re.I)),
    ("USD", re.compile(r"\$|\busd\b", re.I)),
    ("GBP", re.compile(r"£|\bgbp\b", re.I)),
    ("EUR", re.compile(r"€|\beur\b", re.I)),
    ("SGD", re.compile(r"\bsgd\b|s\$", re.I)),
    ("AED", re.compile(r"\baed\b", re.I)),
]
_AMOUNT = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(k|m|l|lpa|lakhs?|lacs?|cr|crores?)?\b", re.I)
_MULT = {"k": 1e3, "m": 1e6, "l": 1e5, "lpa": 1e5, "lakh": 1e5, "lakhs": 1e5, "lac": 1e5, "lacs": 1e5,
         "cr": 1e7, "crore": 1e7, "crores": 1e7}


def parse_salary(text: str | None) -> dict | None:
    """'$120K - $200K' → {'min': 120000, 'max': 200000, 'currency': 'USD', 'period': 'year', 'raw': …}."""
    if not text or not re.search(r"\d", str(text)):
        return None
    raw = str(text)
    currency = next((code for code, pat in _CURRENCY if pat.search(raw)), None)
    if currency is None:
        return None  # a bare number isn't pay we can compare
    matches = list(_AMOUNT.finditer(raw))
    amounts = []
    for i, m in enumerate(matches):
        unit = (m.group(2) or "").lower()
        if not unit and i + 1 < len(matches) and matches[i + 1].group(2):
            between = raw[m.end():matches[i + 1].start()]
            if re.fullmatch(r"\s*(-|–|to)\s*[$₹£€]?\s*", between, re.I):
                unit = matches[i + 1].group(2).lower()  # "25-35 LPA": the unit is shared
        value = float(m.group(1).replace(",", "")) * _MULT.get(unit, 1)
        if value >= 100:  # skip stray small numbers ("2 years", "Level 3")
            amounts.append(value)
    if not amounts:
        return None
    period = "hour" if re.search(r"/\s*h(ou)?r|per hour|hourly", raw, re.I) else \
        "month" if re.search(r"/\s*mo(nth)?|per month|monthly", raw, re.I) else "year"
    return {"min": min(amounts), "max": max(amounts), "currency": currency, "period": period, "raw": raw[:120]}


def salary_from(payload: dict | None) -> dict | None:
    """Stated pay carried in a raw posting's payload (Work at a Startup, Remotive, HN…), if any."""
    salary = (payload or {}).get("salary")
    return parse_salary(salary) if isinstance(salary, str) else None
