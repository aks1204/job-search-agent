"""Part 2a, no database: normalising dates/types/locations/pay, and the Kind-1 filters."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.config_schemas import JobRules, OwnerStub
from app.modules.m2_jobs import normalize as N
from app.modules.m2_jobs.filters import TitleNet, kind1, stems

NOW = datetime(2026, 10, 16, 12, tzinfo=timezone.utc)


@pytest.mark.parametrize("value, expected_days_ago", [
    ("Posted Today", 0), ("Posted Yesterday", 1), ("Posted 7 Days Ago", 7), ("Posted 30+ Days Ago", 31),
])
def test_workday_relative_dates(value, expected_days_ago):
    assert (NOW - N.parse_posted(value, NOW)).days == expected_days_ago


def test_absolute_dates_and_epochs():
    assert N.parse_posted("2026-10-08", NOW) == datetime(2026, 10, 8, tzinfo=timezone.utc)
    assert N.parse_posted("2026-10-01T09:30:00Z", NOW).hour == 9
    assert N.parse_posted(1790000000, NOW).year == 2026          # seconds
    assert N.parse_posted(1790000000000, NOW).year == 2026       # milliseconds (Lever)
    assert N.parse_posted("not a date", NOW) is None and N.parse_posted(None, NOW) is None


@pytest.mark.parametrize("raw, title, expected", [
    ("Full-time", "", "full_time"), ("FullTime", "", "full_time"), ("Full time", "", "full_time"),
    ("Regular", "", "full_time"), ("Part-time", "", "part_time"), ("Contract", "", "contract"),
    (None, "Data Analyst Intern", "internship"), (None, "Analyst (Contract)", "contract"),
    (None, "Senior Data Analyst", "unknown"), (None, "Internal Audit Analyst", "unknown"),
])
def test_employment_type(raw, title, expected):
    assert N.employment_type(raw, title) == expected


@pytest.mark.parametrize("location, india, remote", [
    ("IN: Lilly Bengaluru", True, False), ("Hyderabad, Telangana, India", True, False),
    ("Bengaluru, IN / Remote (IN)", True, True), ("US / Remote (US)", False, True),
    ("Sunnyvale, CA, United States", False, False),
    ("Indianapolis, IN, United States", False, False),   # IN = Indiana here, not India
])
def test_location_flags(location, india, remote):
    assert N.location_flags(location) == (india, remote)


def test_fingerprint_ignores_req_ids_and_takes_first_place():
    a = N.fingerprint("Data Analyst – Consultant (R-112054)", "IN: Lilly Bengaluru; US, Indianapolis")
    b = N.fingerprint("Data Analyst - Consultant", "IN: Lilly Bengaluru")
    assert a == b


@pytest.mark.parametrize("text, expected", [
    ("$120K - $200K", (120000, 200000, "USD", "year")),
    ("₹25L - ₹35L", (2500000, 3500000, "INR", "year")),
    ("25-35 LPA", (2500000, 3500000, "INR", "year")),
    ("£45,000 per annum", (45000, 45000, "GBP", "year")),
    ("$6,000 per month", (6000, 6000, "USD", "month")),
    ("$150/hour", (150, 150, "USD", "hour")),
])
def test_parse_salary(text, expected):
    s = N.parse_salary(text)
    assert (s["min"], s["max"], s["currency"], s["period"]) == expected


def test_parse_salary_needs_a_currency():
    assert N.parse_salary("3-5 years experience") is None and N.parse_salary("120000") is None


OWNER = OwnerStub.model_validate({
    "name": "T", "career_field": "analytics",
    "current_role": {"title": "Senior Associate", "company": "Co", "started": "2024-03"},
    "employment_history": [{"company": "Co", "title": "Senior Associate", "start": "2024-03"}],
    "target_roles": ["Senior Data Analyst", "Senior Business Analyst", "Product Analyst", "Strategy Associate",
                     "Founder's Office"],
    "role_families": {"progression": ["analytics manager"], "lateral": [],
                      "excluded": ["data engineering", "software engineering", "sales"]},
})
RULES = JobRules.model_validate({"kind1": {"noise_titles": ["intern", "chemist", "chemistry"]}})
NET = TitleNet.build(OWNER, RULES)


@pytest.mark.parametrize("title, kept", [
    ("Data Analyst – Consultant/Senior Consultant", True),
    ("Strategy & Operations Lead - Supply Growth", True),
    ("Founder's Office - Associate", True),
    ("Manager, Analytics Insights", True),
    ("Senior Software Engineer - Engineer", False),   # excluded family
    ("Data Engineer II", False),                        # excluded family (engineering → engineer)
    ("Sales Manager, Switzerland", False),              # excluded family
    ("Advisor, Analytical Chemistry", False),           # noise + 'analytical' is not 'analytics'
    ("Senior Biologist", False),                        # outside the net
    ("Product Analytics Intern", False),                # noise word
])
def test_title_net(title, kept):
    assert (NET.check(title) is None) is kept


def test_stems_handle_possessives_plurals_and_ing():
    assert stems("Founder's Office") == ["founder", "office"]
    assert stems("Engineering Analysts") == ["engineer", "analyst"]


def _job(**kw):
    base = dict(title="Senior Data Analyst", employment_type="unknown", posted_at=None, first_seen_at=NOW)
    return SimpleNamespace(**{**base, **kw})


def test_kind1_age_and_employment():
    assert kind1(_job(), NET, RULES, NOW) == []
    assert kind1(_job(posted_at=NOW - timedelta(days=25)), NET, RULES, NOW) == ["posted 25 days ago (limit 21)"]
    assert kind1(_job(employment_type="contract"), NET, RULES, NOW) == ["contract role"]
    # No posting date: age counts from when we first saw it.
    assert kind1(_job(first_seen_at=NOW - timedelta(days=30)), NET, RULES, NOW)[0].startswith("posted 30 days")
