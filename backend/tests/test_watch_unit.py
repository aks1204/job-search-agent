"""Part 1c without a database: readers, careers discovery, ownership. All HTTP is mocked."""

from types import SimpleNamespace

import httpx
import pytest
import respx

from app.core.http import PoliteClient
from app.modules.m1_companies.watch.careers import discover, scan_page
from app.modules.m1_companies.watch.ownership import verify_ownership
from app.modules.m1_companies.watch.readers import (
    FeedNotFound, FeedSpec, FetchResult, RawJob, read_ashby, read_greenhouse, read_lever,
    read_recruitee, read_smartrecruiters, read_workable, read_workday,
)

ACME = SimpleNamespace(id=1, name="Acme Analytics", domain="acme-test.io", aliases=[])


@pytest.fixture(autouse=True)
def no_web_search(monkeypatch):
    monkeypatch.setattr("app.modules.m1_companies.watch.careers.search_available", lambda: False)


@pytest.fixture
def http():
    with PoliteClient(default_interval=0, use_redis=False) as client, respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=r"https://[^/]+/robots\.txt").respond(404)
        yield client, mock


HOME = r"https://acme-test\.io/?$"


def html(body: str) -> dict:
    return {"headers": {"content-type": "text/html"}, "html": f"<html><body>{body}</body></html>"}


# --- readers ---------------------------------------------------------------

def test_greenhouse(http):
    client, mock = http
    mock.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs").respond(json={"jobs": [
        {"id": 7, "title": "Product Analyst", "location": {"name": "Bengaluru"}, "absolute_url": "https://acme-test.io/jobs/7",
         "content": "&lt;p&gt;Own our &lt;b&gt;metrics&lt;/b&gt;&lt;/p&gt;", "departments": [{"name": "Data"}],
         "updated_at": "2026-10-01T00:00:00Z"},
    ]})
    mock.get("https://boards-api.greenhouse.io/v1/boards/acme").respond(json={"name": "Acme Analytics"})
    r = read_greenhouse(client, FeedSpec("greenhouse", "acme"), [])
    job = r.jobs[0]
    assert (job.external_id, job.title, job.location, job.department) == ("7", "Product Analyst", "Bengaluru", "Data")
    assert job.description == "Own our metrics"
    assert r.board_name == "Acme Analytics"


def test_greenhouse_unknown_board_is_not_found(http):
    client, mock = http
    mock.get("https://boards-api.greenhouse.io/v1/boards/nope/jobs").respond(404)
    with pytest.raises(FeedNotFound):
        read_greenhouse(client, FeedSpec("greenhouse", "nope"), [])


def test_lever_and_ashby(http):
    client, mock = http
    mock.get("https://api.lever.co/v0/postings/acme").respond(json=[
        {"id": "abc", "text": "Senior Data Analyst", "categories": {"location": "Remote - India", "team": "Data",
         "commitment": "Full-time"}, "hostedUrl": "https://jobs.lever.co/acme/abc", "createdAt": 1790000000000,
         "descriptionPlain": "Analyse things"},
    ])
    mock.get("https://api.ashbyhq.com/posting-api/job-board/acme").respond(json={"jobs": [
        {"id": "x1", "title": "Strategy Associate", "location": "Mumbai", "jobUrl": "https://jobs.ashbyhq.com/acme/x1",
         "isListed": True, "descriptionPlain": "Strategy"},
        {"id": "x2", "title": "Hidden", "isListed": False},
    ]})
    lever = read_lever(client, FeedSpec("lever", "acme"), []).jobs[0]
    assert (lever.title, lever.employment_type, lever.posted_at[:4]) == ("Senior Data Analyst", "Full-time", "2026")
    assert [j.title for j in read_ashby(client, FeedSpec("ashby", "acme"), []).jobs] == ["Strategy Associate"]


def test_smartrecruiters_empty_account_counts_as_not_found(http):
    client, mock = http
    mock.get("https://api.smartrecruiters.com/v1/companies/ghost/postings").respond(json={"content": []})
    with pytest.raises(FeedNotFound):
        read_smartrecruiters(client, FeedSpec("smartrecruiters", "ghost"), [])


def test_workable_and_recruitee(http):
    client, mock = http
    mock.get("https://apply.workable.com/api/v1/widget/accounts/acme").respond(json={
        "name": "Acme Analytics", "jobs": [{"shortcode": "AB12", "title": "BA", "city": "Pune", "country": "India",
                                            "url": "https://apply.workable.com/j/AB12"}]})
    mock.get("https://acme.recruitee.com/api/offers/").respond(json={"offers": [
        {"id": 5, "title": "Founder's Office", "location": "Delhi", "careers_url": "https://acme.recruitee.com/o/fo",
         "company_name": "Acme Analytics", "description": "<p>Work with founders</p>"}]})
    w = read_workable(client, FeedSpec("workable", "acme"), [])
    assert (w.board_name, w.jobs[0].location) == ("Acme Analytics", "Pune, India")
    r = read_recruitee(client, FeedSpec("recruitee", "acme"), [])
    assert r.jobs[0].description == "Work with founders"


def test_workday_searches_with_roles_and_dedupes(http):
    client, mock = http
    calls = []

    def respond(request):
        calls.append(request.content)
        return httpx.Response(200, json={"jobPostings": [
            {"title": "Senior Data Analyst", "externalPath": "/job/Bengaluru/SDA_R1", "locationsText": "Bengaluru",
             "bulletFields": ["R1"], "postedOn": "Posted Today"}]})

    mock.post("https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/External/jobs").mock(side_effect=respond)
    r = read_workday(client, FeedSpec("workday", "acme", "External", "acme.wd5"), ["Data Analyst", "Product Analyst"])
    assert len(calls) == 2  # one search per role; short page → no second page
    assert [j.external_id for j in r.jobs] == ["R1"]
    assert r.jobs[0].url == "https://acme.wd5.myworkdayjobs.com/External/job/Bengaluru/SDA_R1"


def test_oracle_hint_and_vendor_detection():
    from app.core.hosts import ats_hint, detect_vendors

    url = "https://iaziqy.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/UberCareers/my-profile/sign-in"
    assert ats_hint(url) == {"vendor": "oracle", "slug": "UberCareers", "host": "iaziqy.fa.ocs.oraclecloud.com"}
    page = ('<a href="https://acme.icims.com/jobs/123">Apply</a>'
            '<script src="https://cdn.phenompeople.com/CareerConnectResources/x.js"></script>'
            '<a href="https://acme.wd3.myworkdayjobs.com/External">Jobs</a>')
    assert detect_vendors(page) == {"icims", "phenom", "workday"}


def test_oracle_reader_resolves_site_number_and_searches_roles(http):
    client, mock = http
    host = "pod.fa.ocs.oraclecloud.com"
    mock.get(f"https://{host}/hcmUI/CandidateExperience/en/sites/AcmeCareers/jobs").respond(
        200, html='<div data-fahosturl="" data-sitenumber="CX_7" data-x="y"></div>')
    import re

    searches = mock.get(url__regex=rf"https://{re.escape(host)}/hcmRestApi/resources/latest/recruitingCEJobRequisitions.*").respond(
        json={"items": [{"TotalJobsCount": 1, "requisitionList": [
            {"Id": "303068", "Title": "Data Analyst II, Tech", "PrimaryLocation": "Hyderabad, Telangana, India",
             "PostedDate": "2026-10-08", "JobFamily": "Data", "WorkplaceTypeCode": "ORA_ONSITE"}]}]})
    from app.modules.m1_companies.watch.readers import read_oracle

    r = read_oracle(client, FeedSpec("oracle", "AcmeCareers", host=host), ["Data Analyst", "Product Analyst"])

    assert [(j.external_id, j.title, j.location) for j in r.jobs] == [
        ("303068", "Data Analyst II, Tech", "Hyderabad, Telangana, India")]
    assert r.jobs[0].url == f"https://{host}/hcmUI/CandidateExperience/en/sites/AcmeCareers/job/303068"
    assert searches.call_count == 2  # one search per role
    sent = str(searches.calls[0].request.url)
    assert "siteNumber=CX_7" in sent and "findReqs;" in sent and "%22Data%20Analyst%22" in sent


def test_template_snippets_are_not_vendor_links_but_vanity_oracle_is():
    """Amex: a stale JS template mentions eightfold; the real board is Oracle on careers.americanexpress.com."""
    from app.core.hosts import ats_hints_in, detect_vendors

    page = ('<script>const u = `https://aexp.eightfold.ai/careers?location=${country}&query=${Title}`;</script>'
            '<div data-apibaseurl="https://egug.fa.us2.oraclecloud.com:443" data-sitenumber="CX_1"></div>')
    assert "eightfold" not in detect_vendors(page) and "oracle" in detect_vendors(page)
    assert ats_hints_in(page) == [{"vendor": "oracle", "slug": "CX_1", "site": "CX_1", "host": "egug.fa.us2.oraclecloud.com"}]


def test_eightfold_hint():
    from app.core.hosts import ats_hint

    assert ats_hint("https://morganstanley.eightfold.ai/careers") == {"vendor": "eightfold", "slug": "morganstanley"}


def test_eightfold_reader_searches_india_then_anywhere(http):
    client, mock = http
    calls = []

    def respond(request):
        calls.append(dict(request.url.params))
        pid = 1 if request.url.params["location"] == "India" else 2
        return httpx.Response(200, json={"status": 200, "data": {"count": 1, "positions": [
            {"id": 549796856000 + pid, "name": f"Data Analyst {pid}", "locations": ["Mumbai, India"],
             "department": "Data", "positionUrl": f"/careers/job/{549796856000 + pid}", "postedTs": 1790000000}]}})

    mock.get("https://morganstanley.eightfold.ai/api/pcsx/search").mock(side_effect=respond)
    from app.modules.m1_companies.watch.readers import read_eightfold

    r = read_eightfold(client, FeedSpec("eightfold", "morganstanley", "morganstanley.com"), ["Data Analyst"])

    assert [c["location"] for c in calls] == ["India", ""]  # short pages → no second India page
    assert all(c["domain"] == "morganstanley.com" for c in calls)
    assert [j.url for j in r.jobs] == ["https://morganstanley.eightfold.ai/careers/job/549796856001",
                                       "https://morganstanley.eightfold.ai/careers/job/549796856002"]


def test_eightfold_tenant_without_job_search_is_not_a_board(http):
    client, mock = http
    mock.get("https://bcg.eightfold.ai/api/pcsx/search").respond(json={"message": "PCSX is not enabled for this user."})
    from app.modules.m1_companies.watch.readers import read_eightfold

    with pytest.raises(FeedNotFound):
        read_eightfold(client, FeedSpec("eightfold", "bcg", "bcg.com"), ["Data Analyst"])


def test_oracle_reader_with_known_site_number_skips_the_board_page(http):
    import re

    client, mock = http
    host = "egug.fa.us2.oraclecloud.com"
    searches = mock.get(url__regex=rf"https://{re.escape(host)}/hcmRestApi/.*").respond(json={"items": [
        {"requisitionList": [{"Id": "26014446", "Title": "SrAnalyst Data Governance&Mgmt",
                              "PrimaryLocation": "Bengaluru, KA, India"}]}]})
    from app.modules.m1_companies.watch.readers import read_oracle

    r = read_oracle(client, FeedSpec("oracle", "CX_1", "CX_1", host), ["Data Analyst"])
    assert r.jobs[0].title == "SrAnalyst Data Governance&Mgmt" and searches.call_count == 1


# --- careers discovery -----------------------------------------------------

def test_scan_page_finds_job_links_and_ats():
    raw = ('<a href="/careers/data-analyst-123">Data Analyst</a><a href="/careers/pm-456">PM</a>'
           '<a href="/careers/ba-789">BA</a><a href="mailto:x@y.z">mail</a>'
           '<iframe src="https://boards.greenhouse.io/embed/job_board?for=acme"></iframe>')
    scan = scan_page(raw, "https://acme-test.io/careers")
    assert len(scan.job_links) == 3 and scan.is_listing
    assert scan.ats_hints == [{"vendor": "greenhouse", "slug": "acme"}]


def test_discover_ats_link_on_homepage(http):
    client, mock = http
    mock.get(url__regex=HOME).respond(200, **html('<a href="https://jobs.lever.co/acme">Careers</a>'))
    d = discover(client, ACME)
    assert d.on_company_site and d.scan.ats_hints == [{"vendor": "lever", "slug": "acme"}]


def test_discover_follows_nav_link_one_level_deeper(http):
    client, mock = http
    mock.get(url__regex=HOME).respond(200, **html('<a href="/about">About</a><a href="/join">Join us</a>'))
    mock.get("https://acme-test.io/join").respond(200, **html('<a href="/join/open-roles">See open roles</a>'))
    mock.get("https://acme-test.io/join/open-roles").respond(
        200, **html('<script src="https://boards.greenhouse.io/embed/job_board/js?for=acme"></script>'))
    d = discover(client, ACME)
    assert d.careers_url == "https://acme-test.io/join/open-roles"
    assert d.scan.ats_hints[0]["vendor"] == "greenhouse"


def test_discover_js_careers_site_via_sitemap(http):
    """Lilly-shaped: the careers landing page is a JS app; a sitemap job page names the Workday board."""
    client, mock = http  # robots.txt answers 404 here, so the default /sitemap.xml location is used
    mock.get(url__regex=HOME).respond(200, **html('<a href="https://careers.acme-test.io/us/en">Careers</a>'))
    mock.get("https://careers.acme-test.io/us/en").respond(200, **html('<div id="app">Loading…</div>'))
    mock.get("https://careers.acme-test.io/sitemap.xml").respond(
        200, text="<sitemapindex><sitemap><loc>https://careers.acme-test.io/us/en/sitemap1.xml</loc></sitemap></sitemapindex>")
    mock.get("https://careers.acme-test.io/us/en/sitemap1.xml").respond(
        200, text="<urlset><url><loc>https://careers.acme-test.io/us/en/job/R-1/Data-Analyst</loc></url></urlset>")
    mock.get("https://careers.acme-test.io/us/en/job/R-1/Data-Analyst").respond(200, **html(
        '<script>var ddo = {"applyUrl":"acme.wd115.myworkdayjobs.com\\/ACM\\/job\\/IN-Bengaluru\\/DA_R-1\\/apply"}</script>'))
    mock.get(url__regex=r"https://(acme-test\.io/(careers|jobs|sitemap\.xml)|jobs\.acme-test\.io|careers\.acme-test\.io/?$).*").respond(404)

    d = discover(client, ACME)

    assert d is not None and d.on_company_site and d.how == "job page found via sitemap"
    assert d.scan.ats_hints == [{"vendor": "workday", "slug": "acme", "site": "ACM", "host": "acme.wd115"}]


def test_sitemap_lines_are_read_from_robots():
    from app.modules.m1_companies.watch.careers import _SITEMAP_LINE

    robots = "User-agent: *\nDisallow: */apply\nSitemap: https://careers.lilly.com/us/en/sitemap_index.xml\nsitemap: https://x/y.xml"
    assert _SITEMAP_LINE.findall(robots) == ["https://careers.lilly.com/us/en/sitemap_index.xml", "https://x/y.xml"]


def test_link_heavy_landing_page_does_not_hide_the_real_board(http):
    """Amex: the careers landing page has dozens of /careers/… links; its 'Jobs' link leads to the Oracle board."""
    client, mock = http
    nav = "".join(f'<a href="/careers/section-{i}/">Section {i}</a>' for i in range(10))
    mock.get(url__regex=HOME).respond(200, **html('<a href="/careers/">Careers</a>'))
    mock.get("https://acme-test.io/careers/").respond(200, **html(
        nav + '<a href="https://careers.acme-test.io/en/sites/CX_1/jobs?intlink=nav">Jobs</a>'))
    mock.get("https://careers.acme-test.io/en/sites/CX_1/jobs?intlink=nav").respond(200, **html(
        '<div data-apibaseurl="https://pod.fa.us2.oraclecloud.com:443" data-sitenumber="CX_1"></div>'))

    d = discover(client, ACME)

    assert d.scan.ats_hints == [{"vendor": "oracle", "slug": "CX_1", "site": "CX_1", "host": "pod.fa.us2.oraclecloud.com"}]
    assert d.on_company_site and "Jobs" in d.how


def test_discover_rejects_offsite_redirects(http):
    client, mock = http
    mock.get(url__regex=HOME).respond(200, **html('<a href="/careers">Careers</a>'))
    mock.get("https://acme-test.io/careers").respond(302, headers={"location": "https://www.linkedin.com/company/acme/jobs"})
    mock.get("https://www.linkedin.com/company/acme/jobs").respond(200, **html("lots of jobs"))
    mock.get(url__regex=r"https://(acme-test\.io/(jobs|sitemap\.xml)|careers\.acme-test\.io|jobs\.acme-test\.io).*").respond(404)
    assert discover(client, ACME) is None


# --- ownership ---------------------------------------------------------------

def test_ownership_rules():
    def result(board=None, url="https://boards.greenhouse.io/x/1", desc="We are hiring"):
        return FetchResult([RawJob("1", "Analyst", url=url, description=desc)], board)

    assert verify_ownership(ACME, result(), linked_from_site=True, careers_url="u")[0]
    assert verify_ownership(ACME, result(board="Acme Analytics Inc"), linked_from_site=False, careers_url=None)[0]
    assert verify_ownership(ACME, result(url="https://acme-test.io/jobs/1"), linked_from_site=False, careers_url=None)[0]
    assert verify_ownership(ACME, result(desc="Join Acme Analytics"), linked_from_site=False, careers_url=None)[0]
    ok, why = verify_ownership(ACME, result(board="Acme Plumbing Supplies"), linked_from_site=False, careers_url=None)
    assert not ok and "no evidence" in why
    assert not verify_ownership(ACME, FetchResult([]), linked_from_site=False, careers_url=None)[0]


def test_tiny_namesake_board_rejected_for_large_company():
    uber = SimpleNamespace(id=2, name="Uber", domain="uber.com", aliases=[])
    one_job = FetchResult([RawJob("1", "Driver ops", url="https://jobs.smartrecruiters.com/uber/1")], "Uber")
    ok, why = verify_ownership(uber, one_job, linked_from_site=False, careers_url=None,
                               method="smartrecruiters", slug="uber", employee_estimate="10000+")
    assert not ok and "namesake" in why
    # The same board is fine for a small company.
    assert verify_ownership(uber, one_job, linked_from_site=False, careers_url=None, employee_estimate="11-50")[0]


def test_workday_tenant_matching_domain_is_strong_evidence():
    lilly = SimpleNamespace(id=3, name="Eli Lilly", domain="lilly.com", aliases=[])
    jobs = FetchResult([RawJob("R1", "Senior Data Analyst", url="https://lilly.wd5.myworkdayjobs.com/LLY/job/R1")])
    ok, why = verify_ownership(lilly, jobs, linked_from_site=False, careers_url=None,
                               method="workday", slug="lilly", employee_estimate="10000+")
    assert ok and "tenant" in why
    assert not verify_ownership(lilly, jobs, linked_from_site=False, careers_url=None,
                                method="workday", slug="lillyfoods", employee_estimate="10000+")[0]
