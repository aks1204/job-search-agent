import pytest

from app.core.hosts import ats_hint, is_junk, normalize_domain
from app.modules.m1_companies.identity import name_matches_domain, normalize_name
from app.modules.m1_companies.resolver import pick_domain
from app.core.search import SearchResult


@pytest.mark.parametrize("value, expected", [
    ("https://www.Acme.co.in/careers", "acme.co.in"),
    ("jobs@acme.ai", "acme.ai"),
    ("acme.io", "acme.io"),
    ("careers.google.com", "google.com"),
    ("http://localhost:8000", None),
    ("", None),
    (None, None),
])
def test_normalize_domain(value, expected):
    assert normalize_domain(value) == expected


@pytest.mark.parametrize("value, junk", [
    ("https://www.linkedin.com/company/acme", True),
    ("https://docs.google.com/forms/d/abc", True),
    ("https://boards.greenhouse.io/acme", True),
    ("https://bit.ly/xyz", True),
    ("https://google.com", False),
    ("https://acme.com/careers", False),
])
def test_is_junk(value, junk):
    assert is_junk(value) is junk


@pytest.mark.parametrize("url, hint", [
    ("https://boards.greenhouse.io/acme/jobs/123", {"vendor": "greenhouse", "slug": "acme"}),
    ("https://job-boards.greenhouse.io/acme", {"vendor": "greenhouse", "slug": "acme"}),
    ("https://jobs.lever.co/acme-inc/abc-123", {"vendor": "lever", "slug": "acme-inc"}),
    ("https://jobs.ashbyhq.com/Acme", {"vendor": "ashby", "slug": "Acme"}),
    ("https://acme.wd5.myworkdayjobs.com/en-US/External",
     {"vendor": "workday", "slug": "acme", "site": "External", "host": "acme.wd5"}),
    ("//boards.greenhouse.io/embed/job_board/js?for=acme", {"vendor": "greenhouse", "slug": "acme"}),
    ("https://jobs.lever.co/embed", None),
    ("https://acme.com/careers", None),
])
def test_ats_hint(url, hint):
    assert ats_hint(url) == hint


def test_ats_hints_in_page_finds_embeds_and_escaped_json():
    from app.core.hosts import ats_hints_in

    page = (
        '<script src="https://boards.greenhouse.io/embed/job_board/js?for=acme"></script>'
        '<a href="https://jobs.lever.co/acme/123">Job</a>'
        '{"careers":"https:\\/\\/jobs.ashbyhq.com\\/Acme"}'
        '<a href="https://boards.greenhouse.io/acme">again</a>'
    )
    assert ats_hints_in(page) == [
        {"vendor": "greenhouse", "slug": "acme"},
        {"vendor": "lever", "slug": "acme"},
        {"vendor": "ashby", "slug": "Acme"},
    ]


@pytest.mark.parametrize("raw, expected", [
    ("Acme Technologies Pvt. Ltd.", "acme"),
    ("McKinsey & Company", "mckinsey"),
    ("Bain & Company", "bain"),
    ("Bristol-Myers Squibb", "bristol myers squibb"),
    ("Zoë Labs Inc", "zoe"),
    ("Labs", "labs"),  # never strip a name down to nothing
])
def test_normalize_name(raw, expected):
    assert normalize_name(raw) == expected


@pytest.mark.parametrize("name, domain, match", [
    ("Acme Labs", "acmelabs.io", True),
    ("Acme AI", "acme.ai", True),
    ("Bristol Myers Squibb", "bms.com", True),
    ("Groww", "groww.in", True),
    ("Zenith", "zenithhq.com", True),
    ("Acme", "zenith.com", False),
])
def test_name_matches_domain(name, domain, match):
    assert name_matches_domain(name, domain) is match


def test_pick_domain_skips_boards_and_requires_name_match():
    results = [
        SearchResult("Acme Corp | LinkedIn", "https://www.linkedin.com/company/acme", ""),
        SearchResult("Random blog", "https://someblog.com/acme-review", ""),
        SearchResult("Acme — Analytics for logistics", "https://acmelogistics.io/", ""),
    ]
    assert pick_domain("Acme", results) == "acmelogistics.io"
    assert pick_domain("Nothing Matches", results) is None
