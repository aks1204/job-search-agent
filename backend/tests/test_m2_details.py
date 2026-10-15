"""Description fetchers for list-only boards (HTTP mocked)."""

from types import SimpleNamespace

import pytest
import respx

from app.core.http import PoliteClient
from app.modules.m2_jobs.details import fetch_description

BODY = "<p>" + "Own analytics for the commercial team. " * 10 + "</p>"


@pytest.fixture
def http():
    with PoliteClient(default_interval=0, use_redis=False) as client, respx.mock(assert_all_called=False) as mock:
        yield client, mock


def test_workday_detail_from_job_url(http):
    client, mock = http
    feed = SimpleNamespace(id=1, method="workday", slug="lilly", site="LLY", host="lilly.wd115")
    job = SimpleNamespace(external_id="R-112054",
                          url="https://lilly.wd115.myworkdayjobs.com/LLY/job/IN-Lilly-Bengaluru/Data-Analyst_R-112054")
    mock.get("https://lilly.wd115.myworkdayjobs.com/wday/cxs/lilly/LLY/job/IN-Lilly-Bengaluru/Data-Analyst_R-112054"
             ).respond(json={"jobPostingInfo": {"jobDescription": BODY, "timeType": "Full time", "startDate": "2026-10-09"}})
    text, extra = fetch_description(client, feed, job, {})
    assert text.startswith("Own analytics") and extra == {"employment": "Full time", "posted": "2026-10-09"}


def test_oracle_detail_looks_up_site_number_once(http):
    import re

    client, mock = http
    feed = SimpleNamespace(id=2, method="oracle", slug="UberCareers", site=None, host="pod.fa.ocs.oraclecloud.com")
    page = mock.get("https://pod.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/UberCareers/jobs").respond(
        200, html='<div data-sitenumber="CX_1"></div>')
    detail = mock.get(url__regex=re.escape("https://pod.fa.ocs.oraclecloud.com/hcmRestApi/resources/latest/"
                                           "recruitingCEJobRequisitionDetails") + ".*").respond(json={"items": [
        {"ExternalDescriptionStr": BODY, "ExternalQualificationsStr": "<ul><li>SQL</li></ul>", "JobSchedule": "Full time"}]})
    cache: dict = {}
    for ext in ("303068", "303069"):
        text, extra = fetch_description(client, feed, SimpleNamespace(external_id=ext, url=None), cache)
    assert page.call_count == 1 and detail.call_count == 2
    assert "SQL" in text and extra["employment"] == "Full time"
    assert "siteNumber=CX_1" in str(detail.calls[0].request.url) and "%22303068%22" in str(detail.calls[0].request.url)


def test_smartrecruiters_detail(http):
    client, mock = http
    feed = SimpleNamespace(id=3, method="smartrecruiters", slug="visa", site=None, host=None)
    mock.get("https://api.smartrecruiters.com/v1/companies/visa/postings/744").respond(json={
        "typeOfEmployment": {"label": "Full-time"},
        "jobAd": {"sections": {"jobDescription": {"text": BODY}, "qualifications": {"text": "<p>Python</p>"}}}})
    text, extra = fetch_description(client, feed, SimpleNamespace(external_id="744", url=None), {})
    assert "Python" in text and extra["employment"] == "Full-time"


def test_boards_without_a_detail_endpoint_say_so(http):
    client, _ = http
    with pytest.raises(LookupError):
        fetch_description(client, SimpleNamespace(id=4, method="workable"), SimpleNamespace(external_id="x"), {})
