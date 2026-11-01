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


def test_talentbrew_with_search_closed_to_tools_reads_the_sitemap(http):
    client, mock = http

    def robots(request):
        if request.url.host == "careers.zzuhg-test.io":
            return httpx.Response(200, text="User-agent: *\nDisallow:/search-jobs/")
        return httpx.Response(404)

    mock.get(url__regex=r"https://[^/]+/robots\.txt").mock(side_effect=robots)
    search = mock.get(url__regex=r"https://careers\.zzuhg-test\.io/search-jobs/.*").respond(json={"results": ""})
    base = "https://careers.zzuhg-test.io"
    mock.get(f"{base}/sitemap.xml").respond(200, text=(
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"<url><loc>{base}/category/technology-jobs/34088/81405/1</loc></url>"
        f"<url><loc>{base}/job/hyderabad/senior-data-analyst/34088/98544565312</loc></url>"
        f"<url><loc>{base}/job/makati-city/data-analyst/34088/99821103152</loc></url>"
        f"<url><loc>{base}/job/dallas/physical-therapist-assistant-pta-prn/34088/102507844896</loc></url>"
        "</urlset>"))
    r = read_talentbrew(client, FeedSpec("talentbrew", "careers.zzuhg-test.io"), ["Senior Data Analyst"])
    assert not search.called                                        # robots.txt says no: never asked
    assert [(j.external_id, j.title, j.location) for j in r.jobs] == [
        ("98544565312", "Senior Data Analyst", "Hyderabad"),
        ("99821103152", "Data Analyst", "Makati City")]                # seniority aside, the work words match
    assert r.jobs[0].url == f"{base}/job/hyderabad/senior-data-analyst/34088/98544565312"


def test_a_talentbrew_job_page_gives_the_exact_title_and_place(http):
    from types import SimpleNamespace

    from app.modules.m2_jobs.details import fetch_description

    client, mock = http
    url = "https://careers.zzuhg-test.io/job/hyderabad/sr-data-analyst-payer/34088/1"
    ld = json.dumps({"@type": "JobPosting", "title": "Sr. Data Analyst &amp; Payer", "description": "<p>Own it</p>",
                     "jobLocation": [{"@type": "Place", "address": {
                         "addressLocality": "Hyderabad", "addressRegion": "Telangana", "addressCountry": "India"}}]})
    mock.get(url).respond(200, html=f'<script type="application/ld+json">{ld}</script>')
    job = SimpleNamespace(url=url)
    _, extra = fetch_description(client, SimpleNamespace(method="talentbrew"), job, {})
    assert extra == {"title": "Sr. Data Analyst & Payer", "location": "Hyderabad, Telangana, India"}
    _, extra = fetch_description(client, SimpleNamespace(method="phenom"), job, {})
    assert extra == {}                                               # other sites' lists already give these


def test_a_job_open_in_several_places_keeps_them_all():
    from app.modules.m2_jobs.details import _ld_place

    two = {"jobLocation": [{"address": {"addressLocality": "Dallas", "addressCountry": {"name": "US"}}},
                           {"address": {"addressLocality": "Hyderabad", "addressCountry": "India"}}]}
    assert _ld_place(two) == "Dallas, US; Hyderabad, India"           # not judged by Dallas alone
    assert _ld_place({"jobLocationType": "TELECOMMUTE"}) == "Remote"


def test_sitemap_roles_of_only_seniority_words_and_the_size_cap(http, monkeypatch):
    from app.modules.m1_companies.watch import readers

    client, mock = http
    base = "https://careers.zzcap-test.io"

    def job(ext, slug):
        return f"<url><loc>{base}/job/pune/{slug}/1/{ext}</loc></url>"

    mock.get(f"{base}/sitemap.xml").respond(200, text="<urlset>" + job(5, "associate-director-finance")
                                            + job(9, "registered-nurse") + job(7, "director-associate-relations")
                                            + "</urlset>")
    r = readers._talentbrew_sitemap(client, "careers.zzcap-test.io", ["Associate Director"])
    assert [j.external_id for j in r.jobs] == ["5", "7"]               # all its words needed, not "everything"
    monkeypatch.setattr(readers, "SITEMAP_JOBS_LIMIT", 2)
    r = readers._talentbrew_sitemap(client, "careers.zzcap-test.io", [])
    assert [j.external_id for j in r.jobs] == ["9", "7"]               # the newest two, whatever the order


# --- careers sites on a domain of their own; adapters searched per role ------------------------------------

def test_a_careers_site_on_its_own_domain_is_recognised_by_the_company_name():
    from types import SimpleNamespace

    from app.modules.m1_companies.watch.careers import _careers_domain

    def co(name, domain):
        return SimpleNamespace(name=name, domain=domain)

    assert _careers_domain("https://www.amazon.jobs/", co("Amazon", "amazon.com")) == "amazon.jobs"
    assert _careers_domain("https://www.metacareers.com/jobs", co("Meta", "meta.com")) == "metacareers.com"
    assert _careers_domain("https://jobs.citi.com/", co("Citi", "citigroup.com")) == "citi.com"
    assert _careers_domain("https://search.jobs.barclays/", co("Barclays", "barclays.com")) == "jobs.barclays"
    assert _careers_domain("https://www.linkedin.com/company/amazon/jobs", co("Amazon", "amazon.com")) is None
    assert _careers_domain("https://www.indeed.com/cmp/Amazon", co("Amazon", "amazon.com")) is None
    assert _careers_domain("https://jobs.example.org/", co("Amazon", "amazon.com")) is None


def test_discovery_follows_the_homepage_careers_link_to_the_companys_careers_domain(http):
    from types import SimpleNamespace

    from app.modules.m1_companies.watch.careers import discover

    client, mock = http
    mock.get("https://zzshop-test.com").respond(
        200, html='<a href="https://www.zzshop-test.jobs/">Careers</a><a href="https://www.linkedin.com/x">Jobs</a>')
    mock.get("https://www.zzshop-test.jobs/").respond(
        200, html='<a href="https://boards.greenhouse.io/zzshop">See open roles</a>')
    mock.get(url__regex=r"https://(www\.|careers\.|jobs\.)?zzshop-test\.com/.*").respond(404)
    found = discover(client, SimpleNamespace(domain="zzshop-test.com", name="Zzshop"))
    assert found and found.scan.ats_hints == [{"vendor": "greenhouse", "slug": "zzshop"}]
    assert "their careers site" in found.how and found.on_company_site


def test_an_adapter_with_role_is_searched_once_per_role_without_seniority_words(http):
    from app.modules.m1_companies.watch.readers import read_adapter

    client, mock = http
    route = mock.get("https://www.zzbig-test.jobs/en/search.json").mock(side_effect=lambda request: httpx.Response(
        200, json={"jobs": [{"id_icims": "1", "title": "Data Analyst", "normalized_location": "Bengaluru, IND"},
                            {"id_icims": "2" if "Product" in request.url.params["q"] else "1", "title": "Product Analyst"}]}))
    adapter = {"name": "Zzbig", "request": {"method": "GET", "url": "https://www.zzbig-test.jobs/en/search.json",
                                            "params": {"q": "{role}", "limit": 100}},
               "items": "jobs", "fields": {"external_id": "id_icims", "title": "title", "location": "normalized_location"},
               "url_template": "https://www.zzbig-test.jobs/en/jobs/{external_id}", "pagination": None}
    r = read_adapter(client, adapter, ["Senior Data Analyst", "Product Analyst", "Data Analyst"])
    assert [c.request.url.params["q"] for c in route.calls] == ["Data Analyst", "Product Analyst"]  # each once
    assert [(j.external_id, j.url) for j in r.jobs] == [("1", "https://www.zzbig-test.jobs/en/jobs/1"),
                                                         ("2", "https://www.zzbig-test.jobs/en/jobs/2")]


def test_careers_domains_that_only_look_like_the_companys_are_not_trusted():
    from types import SimpleNamespace

    from app.modules.m1_companies.watch.careers import _careers_domain

    def co(name, domain):
        return SimpleNamespace(name=name, domain=domain)

    assert _careers_domain("https://www.healthecareers.com/", co("Health Catalyst", "healthcatalyst.com")) is None
    assert _careers_domain("https://barclays.csod.com/ux/ats", co("Barclays", "barclays.com")) is None  # Cornerstone's
    assert _careers_domain("https://jobs.anything.com/", co("Jobsforher", "jobsforher.com")) is None


def test_a_careers_link_that_redirects_to_the_companys_careers_domain_is_followed(http):
    from types import SimpleNamespace

    from app.modules.m1_companies.watch.careers import discover

    client, mock = http
    mock.get("https://zzciti-test.com/").respond(200, html='<a href="/careers">Careers</a>')
    mock.get("https://zzciti-test.com/careers").mock(return_value=httpx.Response(
        301, headers={"location": "https://jobs.zzciti-test.io/"}))
    mock.get("https://jobs.zzciti-test.io/").respond(
        200, html='<a href="https://jobs.lever.co/zzciti">Open roles</a>')
    found = discover(client, SimpleNamespace(domain="zzciti-test.com", name="Zzciti"))
    assert found and found.scan.ats_hints == [{"vendor": "lever", "slug": "zzciti"}]


def test_a_moved_homepage_and_a_careers_page_linking_to_the_careers_site_are_followed(http):
    from types import SimpleNamespace

    from app.modules.m1_companies.watch.careers import discover

    client, mock = http
    # barclays.com → home.barclays, whose careers hub links on to search.jobs.barclays
    mock.get("https://zzbank-test.com").mock(return_value=httpx.Response(
        301, headers={"location": "https://www.zzbank-test.co.uk/"}))
    mock.get("https://www.zzbank-test.co.uk/").respond(200, html='<a href="/careers/">Careers hub</a>')
    mock.get("https://www.zzbank-test.co.uk/careers/").respond(
        200, html='<a href="https://search.jobs.zzbank-test.io/">Search jobs</a>'
                  '<a href="https://jobs.example.org/">Search jobs</a>')
    mock.get("https://search.jobs.zzbank-test.io/").respond(
        200, html='<a href="https://jobs.lever.co/zzbank">Open roles</a>')
    found = discover(client, SimpleNamespace(domain="zzbank-test.com", name="Zzbank"))
    assert found and found.scan.ats_hints == [{"vendor": "lever", "slug": "zzbank"}]
    assert found.on_company_site
    assert not any("example.org" in str(c.request.url) for c in mock.calls)   # not the company's: not followed


def test_dates_written_out_in_words_are_understood():
    from datetime import datetime, timezone

    from app.modules.m2_jobs.normalize import parse_posted

    seen = datetime(2026, 11, 1, tzinfo=timezone.utc)
    assert parse_posted("October 29, 2026", seen) == datetime(2026, 10, 29, tzinfo=timezone.utc)
    assert parse_posted("Oct 29, 2026", seen) == datetime(2026, 10, 29, tzinfo=timezone.utc)
    assert parse_posted("sometime soon", seen) is None
