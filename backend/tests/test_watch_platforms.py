"""Careers-site readers added 2026-11-01, each against the public format checked live on the real site."""

import json

import httpx
import pytest
import respx

from app.core.hosts import ats_hint
from app.core.http import PoliteClient
from app.modules.m1_companies.watch.careers import scan_page
from app.modules.m1_companies.watch.readers import (
    FeedNotFound, FeedSpec, read_jazzhr, read_jibe, read_phenom, read_pinpoint, read_talentbrew, read_teamtailor,
)


@pytest.fixture
def http():
    with PoliteClient(default_interval=0, use_redis=False) as client, respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=r"https://[^/]+/robots\.txt").respond(404)
        yield client, mock


# --- recognising them on a careers page ----------------------------------------------------------------

def test_platform_sites_are_recognised_by_their_own_host():
    jibe = scan_page('<img src="https://cms.jibecdn.com/prod/workingatbooking/assets/logo.svg">',
                     "https://jobs.booking.com/booking/jobs")
    assert {"vendor": "jibe", "slug": "jobs.booking.com"} in jibe.ats_hints
    phenom = scan_page('<link href="https://cdn.phenompeople.com/CareerConnectResources/common/x.css">',
                       "https://careers.bcg.com/global/en/")
    assert {"vendor": "phenom", "slug": "careers.bcg.com"} in phenom.ats_hints
    tb = scan_page('<img src="//tbcdn.talentbrew.com/company/34088/img/logo/uhg-logo.webp">',
                   "https://careers.unitedhealthgroup.com/search-jobs")
    assert {"vendor": "talentbrew", "slug": "careers.unitedhealthgroup.com"} in tb.ats_hints


def test_job_board_links_are_hints():
    assert ats_hint("https://axmed.teamtailor.com/jobs") == {"vendor": "teamtailor", "slug": "axmed"}
    assert ats_hint("https://paradromicsinc.applytojob.com/apply") == {"vendor": "jazzhr", "slug": "paradromicsinc"}
    assert ats_hint("https://sunking.pinpointhq.com/") == {"vendor": "pinpoint", "slug": "sunking"}


# --- the readers ---------------------------------------------------------------------------------------

def test_jibe_reads_the_whole_board_page_by_page(http):
    client, mock = http
    job = lambda n: {"data": {"req_id": str(n), "slug": str(n), "title": f"Analyst {n}", "full_location": "Bangalore, India",  # noqa: E731
                              "apply_url": f"https://external-workingatbooking.icims.com/jobs/{n}/login",
                              "posted_date": "2026-10-30T14:35:00+0000", "description": "<p>Own <b>data</b></p>",
                              "ats_code": "icims", "country": "India"}}
    pages = {"1": {"jobs": [job(i) for i in range(100)], "totalCount": 130, "count": 130},
             "2": {"jobs": [job(i) for i in range(100, 130)], "totalCount": 130, "count": 130}}
    mock.get(url__regex=r"https://jobs\.booking\.com/api/jobs.*").mock(
        side_effect=lambda request: httpx.Response(200, json=pages[request.url.params["page"]]))
    r = read_jibe(client, FeedSpec("jibe", "jobs.booking.com"), [])
    assert len(r.jobs) == 130
    j = r.jobs[0]
    assert (j.title, j.location, j.url) == ("Analyst 0", "Bangalore, India", "https://jobs.booking.com/jobs/0")
    assert j.description == "Own data" and j.payload["apply_url"].endswith("/jobs/0/login")


def test_jibe_without_a_job_list_is_not_a_board(http):
    client, mock = http
    mock.get(url__regex=r"https://careers\.nope-test\.io/api/jobs.*").respond(404)
    with pytest.raises(FeedNotFound):
        read_jibe(client, FeedSpec("jibe", "careers.nope-test.io"), [])


def _phenom_page(jobs, total):
    ddo = {"eagerLoadRefineSearch": {"totalHits": total, "hits": len(jobs), "data": {"jobs": jobs}}}
    return f"<script>var phApp = phApp || {{}}; phApp.ddo = {json.dumps(ddo)}; phApp.experimentData = {{}};</script>"


def test_phenom_searches_with_your_roles_from_the_embedded_results(http):
    client, mock = http
    mock.get("https://careers.bcg.com/").mock(return_value=httpx.Response(
        301, headers={"location": "https://careers.bcg.com/global/en/"}))
    mock.get("https://careers.bcg.com/global/en/").respond(200, html="<html>home</html>")
    jobs = [{"jobId": "59533", "title": "Global People Analytics Manager", "location": "Gurgaon, Haryana, India",
             "jobUrl": "https://careers.bcg.com/global/en/job/59533/x", "category": "People", "type": "Full-Time",
             "postedDate": "2026-10-06T00:00:00.000+0000", "applyUrl": "https://experiencedtalent.bcg.com/x",
             "country": "India", "descriptionTeaser": "Lead people analytics"}]
    route = mock.get(url__regex=r"https://careers\.bcg\.com/global/en/search-results.*").respond(
        200, html=_phenom_page(jobs, 1))
    r = read_phenom(client, FeedSpec("phenom", "careers.bcg.com"), ["analytics"])
    assert [(j.external_id, j.title, j.location) for j in r.jobs] == \
        [("59533", "Global People Analytics Manager", "Gurgaon, Haryana, India")]
    assert route.calls[0].request.url.params["keywords"] == "analytics"


def test_talentbrew_reads_the_results_fragment(http):
    client, mock = http
    fragment = ('<section id="search-results" data-total-results="2"><ul>'
                '<li> <a href="/job/hyderabad/senior-data-analyst/34088/98544565312" data-job-id="98544565312" class="x">'
                '<div> <h2>Senior Data Analyst</h2> <span class="job-id job-info">2376686</span>'
                '<span class="job-location 1">Hyderabad, Telangana</span></div></a></li>'
                '<li> <a href="/job/makati-city/data-analyst/34088/99821103152" data-job-id="99821103152">'
                '<div><h2>Data Analyst</h2><span class="job-location">Makati City, Philippines</span></div></a></li>'
                '</ul></section>')
    route = mock.get(url__regex=r"https://careers\.unitedhealthgroup\.com/search-jobs/results.*").respond(
        json={"filters": "", "results": fragment, "hasJobs": True, "hasContent": True})
    r = read_talentbrew(client, FeedSpec("talentbrew", "careers.unitedhealthgroup.com"), ["data analyst"])
    assert [(j.external_id, j.title, j.location) for j in r.jobs] == [
        ("98544565312", "Senior Data Analyst", "Hyderabad, Telangana"),
        ("99821103152", "Data Analyst", "Makati City, Philippines")]
    assert r.jobs[0].url == "https://careers.unitedhealthgroup.com/job/hyderabad/senior-data-analyst/34088/98544565312"
    assert route.calls[0].request.url.params["Keywords"] == "data analyst"


def test_teamtailor_reads_the_json_feed(http):
    client, mock = http
    mock.get("https://axmed.teamtailor.com/jobs.json").respond(json={
        "version": "https://jsonfeed.org/version/1.1", "title": "Axmed", "items": [{
            "id": "ab8d20e2", "title": "AI Engineer", "url": "https://axmed.teamtailor.com/jobs/8175859-ai-engineer",
            "date_published": "2026-08-05T16:00:15+01:00", "content_html": "<p>Build things</p>",
            "_jobposting": {"identifier": {"value": "8175859"}, "employmentType": "FULL_TIME",
                            "jobLocationType": "TELECOMMUTE",
                            "jobLocation": [{"address": {"addressLocality": "Madrid", "addressCountry": "Spain"}}]}}]})
    r = read_teamtailor(client, FeedSpec("teamtailor", "axmed"), [])
    j = r.jobs[0]
    assert (j.external_id, j.title, j.location, j.employment_type) == \
        ("8175859", "AI Engineer", "Remote (Madrid, Spain)", "FULL_TIME")
    assert r.board_name == "Axmed"


def test_pinpoint_reads_every_posting(http):
    client, mock = http
    mock.get("https://sunking.pinpointhq.com/postings.json").respond(json={"data": [{
        "id": "395125", "title": "Survey Engineer", "url": "https://sunking.pinpointhq.com/en/postings/abc",
        "employment_type": "contract", "employment_type_text": "Contract", "workplace_type": "onsite",
        "description": "<p>Survey sites</p>", "job": {"department": {"name": "Engineering"}},
        "location": {"city": "New Delhi", "province": "", "name": "India"}}]})
    j = read_pinpoint(client, FeedSpec("pinpoint", "sunking"), []).jobs[0]
    assert (j.external_id, j.title, j.location, j.department, j.employment_type) == \
        ("395125", "Survey Engineer", "New Delhi, India", "Engineering", "Contract")


def test_jazzhr_reads_the_apply_page(http):
    client, mock = http
    mock.get("https://paradromicsinc.applytojob.com/apply").respond(200, html=(
        "<html><head><title>Paradromics, Inc. - Career Page</title></head><body><ul>"
        '<li class="list-group-item"> <h3 class="list-group-item-heading"> '
        '<a href="https://paradromicsinc.applytojob.com/apply/u5hXN4N9ID/Embedded-Software-Engineer"> '
        'Embedded Software Engineer </a> </h3> <ul class="list-inline list-group-item-text"> '
        '<li><i class="fa fa-map-marker"></i>Austin, TX</li> </ul> </li></ul></body></html>'))
    r = read_jazzhr(client, FeedSpec("jazzhr", "paradromicsinc"), [])
    assert [(j.external_id, j.title, j.location) for j in r.jobs] == \
        [("u5hXN4N9ID", "Embedded Software Engineer", "Austin, TX")]
    assert r.board_name == "Paradromics, Inc."


# --- a careers-site front never hides the real job system behind it ------------------------------------------

def _page(body: str) -> dict:
    return {"headers": {"content-type": "text/html"}, "html": f"<html><body>{body}</body></html>"}


def test_a_real_job_system_behind_a_phenom_front_wins(http):
    from types import SimpleNamespace

    from app.modules.m1_companies.watch.careers import discover

    client, mock = http
    mock.get(url__regex=r"https://zzfront-test\.io/?$").respond(**_page('<a href="https://careers.zzfront-test.io/">Careers</a>'))
    mock.get(url__regex=r"https://careers\.zzfront-test\.io/?$").respond(**_page(
        '<link href="https://cdn.phenompeople.com/CareerConnectResources/x.css">'
        '<a href="https://careers.zzfront-test.io/jobs">Search jobs</a>'))
    mock.get("https://careers.zzfront-test.io/jobs").respond(**_page(
        '<a href="https://zzfront.wd5.myworkdayjobs.com/en-US/External">Apply</a>'))
    mock.get(url__regex=r"https://zzfront-test\.io/(careers|jobs)").respond(404)
    mock.get(url__regex=r"https://jobs\.zzfront-test\.io.*").respond(404)
    found = discover(client, SimpleNamespace(domain="zzfront-test.io", name="Zzfront"))
    assert found and {h["vendor"] for h in found.scan.ats_hints} == {"workday"}


def test_a_phenom_front_with_nothing_behind_it_is_used(http):
    from types import SimpleNamespace

    from app.modules.m1_companies.watch.careers import discover

    client, mock = http
    mock.get(url__regex=r"https://zzbcg-test\.io/?$").respond(**_page('<a href="https://careers.zzbcg-test.io/">Careers</a>'))
    mock.get(url__regex=r"https://careers\.zzbcg-test\.io/?$").respond(**_page(
        '<link href="https://cdn.phenompeople.com/CareerConnectResources/x.css">'))
    mock.get(url__regex=r"https://(jobs\.)?zzbcg-test\.io/.*").respond(404)
    mock.get(url__regex=r"https://careers\.zzbcg-test\.io/.+").respond(404)
    found = discover(client, SimpleNamespace(domain="zzbcg-test.io", name="Zzbcg"))
    assert found and found.scan.ats_hints == [{"vendor": "phenom", "slug": "careers.zzbcg-test.io"}]


def test_descriptions_come_from_the_job_pages_own_job_data(http):
    from types import SimpleNamespace

    from app.modules.m2_jobs.details import fetch_description

    client, mock = http
    ld = json.dumps({"@type": "JobPosting", "title": "Senior Data Analyst", "description": "<p>Own the metrics</p>"})
    mock.get("https://careers.zzuhg-test.io/job/hyderabad/senior-data-analyst/1/2").respond(
        200, html=f'<html><head><script type="application/ld+json">{ld}</script></head><body>…</body></html>')
    feed = SimpleNamespace(method="talentbrew", slug="careers.zzuhg-test.io", site=None, host=None)
    job = SimpleNamespace(url="https://careers.zzuhg-test.io/job/hyderabad/senior-data-analyst/1/2", method="talentbrew")
    text, _ = fetch_description(client, feed, job, {})
    assert text == "Own the metrics"


def test_regional_teamtailor_and_vendor_pages():
    assert ats_hint("https://acme.na.teamtailor.com/jobs") == {"vendor": "teamtailor", "slug": "acme.na.teamtailor.com"}
    assert ats_hint("https://help.pinpointhq.com/article/1") is None                 # Pinpoint's own help pages
    assert ats_hint("https://acme.wd5.myworkdayjobs.com/en-US/Career") == \
        {"vendor": "workday", "slug": "acme", "site": "Career", "host": "acme.wd5"}   # a real Workday site name
    assert ats_hint("https://acme.wd5.myworkdayjobs.com/Careers")["site"] == "Careers"
    assert ats_hint("https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/Careers/jobs") is None   # its API, not a site


def test_phenom_teaser_is_not_taken_for_the_description(http):
    client, mock = http
    mock.get("https://careers.zzph-test.io/").respond(200, html="<html>home</html>")
    jobs = [{"jobId": "1", "title": "Data Analyst", "jobUrl": "https://careers.zzph-test.io/job/1",
             "descriptionTeaser": "Lead people analytics"}]
    mock.get(url__regex=r"https://careers\.zzph-test\.io/search-results.*").respond(200, html=_phenom_page(jobs, 1))
    job = read_phenom(client, FeedSpec("phenom", "careers.zzph-test.io"), ["analyst"]).jobs[0]
    assert job.description is None and job.payload["teaser"] == "Lead people analytics"   # full page fetched later


def test_job_page_data_with_a_list_type_or_a_graph():
    from app.modules.m2_jobs.details import _ld_job_description

    def page(data):
        return f'<script type="application/ld+json">{json.dumps(data)}</script>'

    assert _ld_job_description(page({"@type": ["JobPosting", "Thing"], "description": "A"})) == "A"
    assert _ld_job_description(page({"@graph": [{"@type": "WebPage"}, {"@type": "JobPosting", "description": "B"}]})) == "B"
    assert _ld_job_description(page([{"@graph": [{"@type": "JobPosting", "description": "C"}]}])) == "C"
    assert _ld_job_description(page("just a string") + page(7)) is None
