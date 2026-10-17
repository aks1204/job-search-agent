"""Part 3a, no database: names → addresses, designated-address detection, search-snippet parsing."""

from types import SimpleNamespace

import pytest

from app.core.search import SearchResult
from app.modules.m3_outreach import people as P
from app.modules.m3_outreach.designated import find_designated
from app.modules.m3_outreach.names import infer_pattern, is_role_address, name_key, render, split_name


@pytest.mark.parametrize("name, parts", [
    ("Priya Sharma", ("priya", "sharma")),
    ("Dr. Priya K. Sharma", ("priya", "sharma")),
    ("Priya Sharma, SHRM-CP", ("priya", "sharma")),
    ("José Álvarez (He/Him)", ("jose", "alvarez")),
    ("Madonna", None),
])
def test_split_name(name, parts):
    assert split_name(name) == parts


def test_render_and_infer_are_inverse():
    for pattern in ("{first}.{last}", "{f}{last}", "{first}", "{first}_{last}", "{last}.{first}"):
        local = render(pattern, "priya", "sharma")
        assert infer_pattern(f"{local}@acme.com", "Priya Sharma") == pattern
    assert infer_pattern("pk.sharma1@acme.com", "Priya Sharma") is None
    assert infer_pattern("jobs@acme.com", "Priya Sharma") is None


def test_name_key_and_role_addresses():
    assert name_key("Dr. Priya Sharma") == "priya sharma"
    assert is_role_address("careers@acme.com") and is_role_address("Jobs@acme.com")
    assert not is_role_address("priya.sharma@acme.com")


@pytest.mark.parametrize("text, expected", [
    ("Interested? Send your CV to talent@acme.io with the role in the subject.", "talent@acme.io"),
    ("To apply, email us at hiring@startup.dev — we read everything.", "hiring@startup.dev"),
    ("If you need an accommodation during the process, email accommodations@acme.com.", None),
    ("For privacy questions contact dpo@acme.com.", None),
    ("We never ask for money. Report fraud to fraud@acme.com", None),
    ("Our office: 12 MG Road, Bengaluru. Equal opportunity employer.", None),
    ("Accessibility requests: help@acme.com. To apply, write to jobs@acme.com", "jobs@acme.com"),
])
def test_designated_address(text, expected):
    hit = find_designated(text)
    assert (hit[0] if hit else None) == expected


def test_profile_url_normalised_and_non_profiles_ignored():
    assert P.profile_url("https://in.linkedin.com/in/Priya-Sharma-123?trk=x") == "https://www.linkedin.com/in/priya-sharma-123"
    assert P.profile_url("https://www.linkedin.com/company/acme") is None
    assert P.profile_url("https://example.com/in/someone") is None


def test_parse_search_result():
    s = P.parse_result(SearchResult("Priya Sharma - Talent Acquisition Lead - Acme | LinkedIn",
                                    "https://www.linkedin.com/in/priya-sharma", "Experience: Acme · Bengaluru"))
    assert s.name == "Priya Sharma" and s.headline.startswith("Talent Acquisition Lead")
    assert P.parse_result(SearchResult("Acme | LinkedIn", "https://www.linkedin.com/in/acme", "")) is None


def test_queries_are_linkedin_scoped():
    co = SimpleNamespace(name="Acme")
    for kind in ("founder", "recruiting", "hiring_manager"):
        q = P.build_query(co, kind, "analytics", "india")
        assert q.startswith('site:linkedin.com/in "Acme"')
    assert "India" in P.build_query(co, "recruiting", "analytics", "india")
    assert "India" not in P.build_query(co, "recruiting", "analytics", "remote")


def test_keyword_fallback_spots_former_employees():
    co = SimpleNamespace(name="Acme")
    now = P._keyword_read(0, P.Snippet("u", "A B", "Recruiter at Acme", "Bengaluru"), co)
    gone = P._keyword_read(0, P.Snippet("u", "A B", "Recruiter at Globex", "ex-Acme"), co)
    assert now.works_there_now == "yes" and now.kind == "recruiting" and now.geo == "india"
    assert gone.works_there_now == "no"
