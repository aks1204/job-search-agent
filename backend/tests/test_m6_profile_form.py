"""Part 6b-2: your profile as a form. The checks without a database; Save against the real database (rolled back
afterwards), from a profile made for the test."""

import re
from datetime import date

import pytest
from sqlalchemy import select

from app.web.profile_edit import blank_job, to_data, to_values
from tests.test_m6_web_integration import client  # noqa: F401  (same fixture)

PROFILE = {
    "name": "Test Person", "location": "Gurgaon", "career_field": "business analytics",
    "current_role": {"title": "Consultant", "company": "Acme", "started": "2023-01"},
    "employment_history": [{"company": "Acme", "title": "Consultant", "start": "2023-01", "end": None},
                           {"company": "Acme", "title": "Associate", "start": "2020-07", "end": "2022-12"}],
    "current_compensation": {"annual_ctc": 2400000, "currency": "INR", "includes_variable": True},
    "notice_period_days": 60, "level_ladder": ["Associate", "Consultant", "Manager"], "current_level": "Consultant",
    "target_roles": ["Analytics Manager"], "role_families": {"progression": ["business analytics"]},
    "skills": {"must_have": ["SQL", "Python"]},
    "work_authorization": {"citizenship": "India", "relocation_countries": ["Singapore"]},
    "preferred_locations_india": ["Gurgaon", "Bengaluru"],
}


def test_the_form_gives_back_the_same_profile():
    data, errors = to_data(to_values(PROFILE), PROFILE)
    assert errors == {}
    assert data["employment_history"] == PROFILE["employment_history"] and data["current_role"] == PROFILE["current_role"]
    assert data["current_compensation"]["annual_ctc"] == 2400000 and data["notice_period_days"] == 60
    assert data["preferred_locations_india"] == ["Gurgaon", "Bengaluru"] and data["skills"]["must_have"] == ["SQL", "Python"]
    assert data["work_authorization"]["relocation_countries"] == ["Singapore"]       # not on the form: kept


def test_the_form_says_whats_wrong_next_to_the_field():
    v = to_values(PROFILE)
    v["jobs"][1]["end"] = "2019-01"                                  # ends before it starts
    v["jobs"][2] = {**blank_job(), "company": "Beta", "title": "Analyst", "start": "Mar 2018"}   # a second "now"
    v["pay_amount"], v["notice_days"] = "lots", "sixty"
    v["current_level"] = "Partner"
    v["excluded"] = "business analytics"
    _, errors = to_data(v, PROFILE)
    assert errors["job-1"] == "The end date is before the start date."
    assert errors["job-2"] == "Only your current job can have no end date."
    assert {"pay_amount", "notice_days", "current_level", "excluded"} <= set(errors)


def test_months_and_money_are_read_the_way_people_write_them():
    v = to_values(PROFILE)
    v["jobs"][1].update(start="7/2020", end="Dec 2022")
    v["pay_amount"] = "24,00,000"
    v["cities"] = "Gurgaon, Bengaluru,  gurgaon"
    v["jobs"][0]["remove"] = False
    data, errors = to_data(v, PROFILE)
    assert errors == {}
    assert data["employment_history"][1]["start"] == "2020-07" and data["employment_history"][1]["end"] == "2022-12"
    assert data["current_compensation"]["annual_ctc"] == 2400000
    assert data["preferred_locations_india"] == ["Gurgaon", "Bengaluru"]          # duplicates dropped


# --- Save, against the database ------------------------------------------------------------------------

def _form(values: dict, token: str, **extra) -> dict:
    out = {"token": token, "job_count": str(len(values["jobs"]))}
    for k, v in values.items():
        if k == "jobs":
            for i, j in enumerate(v):
                out.update({f"job-{i}-{f}": j[f] for f in ("company", "title", "start", "end")})
                if j["remove"]:
                    out[f"job-{i}-remove"] = "on"
        elif isinstance(v, bool):
            if v:
                out[k] = "on"
        else:
            out[k] = v
    return {**out, **extra}


@pytest.fixture
def profile(db_session):
    """Your real profile set aside for the test (rolled back); a test profile is the confirmed one."""
    from app.models.profile import OwnerProfile
    from app.modules.m5_profile import store

    for row in db_session.scalars(select(OwnerProfile).where(OwnerProfile.status.in_(("confirmed", "draft")))).all():
        row.status = "superseded"
    row = OwnerProfile(status="confirmed", data=dict(PROFILE), notes={}, resume_hash="t",
                       confirmed_fields={"current_compensation": "2026-01-01", "notice_period_days": "2026-01-01"})
    db_session.add(row)
    db_session.flush()
    yield row
    store.invalidate()


@pytest.mark.integration
def test_saving_the_form_updates_the_profile_and_logs_only_what_changed(client, db_session, profile):
    from app.models import AuditLog

    page = client.get("/profile/edit").text
    assert "Edit your profile" in page and 'name="job-1-start"' in page
    token = re.search(r'name="token" value="([^"]+)"', page).group(1)
    values = to_values(PROFILE)
    values["notice_days"] = "90"
    r = client.post("/profile/edit", data=_form(values, token), follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/profile"
    db_session.refresh(profile)
    assert profile.status == "confirmed" and profile.data["notice_period_days"] == 90       # changed in place
    assert profile.confirmed_fields["notice_period_days"] == date.today().isoformat()       # re-confirmed
    entry = db_session.scalars(select(AuditLog).where(AuditLog.action == "profile_edited")
                               .order_by(AuditLog.id.desc())).first()
    assert entry.summary == "You edited your profile: notice period" and "90" not in entry.summary
    assert "Saved: notice period" in client.get("/profile").text


@pytest.mark.integration
def test_a_mistake_saves_nothing_and_keeps_what_you_typed(client, db_session, profile):
    token = re.search(r'name="token" value="([^"]+)"', client.get("/profile/edit").text).group(1)
    values = to_values(PROFILE)
    values["jobs"][1]["end"] = "2019-01"
    values["location"] = "Pune"
    r = client.post("/profile/edit", data=_form(values, token))
    assert r.status_code == 200 and "The end date is before the start date." in r.text
    assert 'value="Pune"' in r.text                                   # what you typed is still there
    db_session.refresh(profile)
    assert profile.data["location"] == "Gurgaon"


@pytest.mark.integration
def test_a_change_the_ai_judges_jobs_by_asks_first(client, db_session, profile):
    token = re.search(r'name="token" value="([^"]+)"', client.get("/profile/edit").text).group(1)
    values = to_values(PROFILE)
    values["target_roles"] = "Analytics Manager\nStrategy Manager"
    r = client.post("/profile/edit", data=_form(values, token))
    assert r.status_code == 200 and "BEFORE YOU SAVE" in r.text and "target roles" in r.text
    db_session.refresh(profile)
    assert profile.data["target_roles"] == ["Analytics Manager"]       # not yet
    r = client.post("/profile/edit", data=_form(values, token, confirmed="1"), follow_redirects=False)
    assert r.status_code == 303
    db_session.refresh(profile)
    assert profile.data["target_roles"] == ["Analytics Manager", "Strategy Manager"]


@pytest.mark.integration
def test_add_a_row_keeps_the_form_and_saves_nothing(client, db_session, profile):
    token = re.search(r'name="token" value="([^"]+)"', client.get("/profile/edit").text).group(1)
    values = to_values(PROFILE)
    values["location"] = "Pune"
    r = client.post("/profile/edit", data=_form(values, token, add_row="1"))
    assert r.status_code == 200 and f'name="job-{len(values["jobs"])}-company"' in r.text and 'value="Pune"' in r.text
    db_session.refresh(profile)
    assert profile.data["location"] == "Gurgaon"


@pytest.mark.integration
def test_the_profile_page_reads_well(client, profile):
    page = client.get("/profile").text
    assert 'href="/profile/edit"' in page and "Test Person" in page
    assert "INR 2,400,000" in page and "current" in page and "Bengaluru" in page


@pytest.mark.integration
def test_a_leftover_terminal_edit_doesnt_make_a_new_version(client, db_session, profile):
    """`profile edit` leaves a copy waiting; Save replaces it and changes your profile in place (a new version
    would have the daily check re-work your career direction with the AI)."""
    from app.models.profile import OwnerProfile

    stray = OwnerProfile(status="draft", data=dict(PROFILE), notes={"gaps": [], "from_resume": []},
                         resume_hash="t", confirmed_fields={})
    db_session.add(stray)
    db_session.flush()
    page = client.get("/profile/edit").text
    assert "Edit your profile" in page and "Check your new résumé reading" not in page
    token = re.search(r'name="token" value="([^"]+)"', page).group(1)
    values = to_values(PROFILE)
    values["location"] = "Noida"
    assert client.post("/profile/edit", data=_form(values, token), follow_redirects=False).status_code == 303
    db_session.refresh(profile)
    db_session.refresh(stray)
    assert profile.status == "confirmed" and profile.data["location"] == "Noida" and stray.status == "superseded"


def test_a_new_mobile_number_rewrites_drafts_so_it_asks_first():
    from app.modules.m5_profile.profile import ai_changes

    assert ai_changes(PROFILE, {**PROFILE, "notice_period_days": 90}) == (False, False, False)
    assert ai_changes(PROFILE, {**PROFILE, "mobile": "+91 90000 00000"}) == (False, False, True)
    assert ai_changes(PROFILE, {**PROFILE, "target_roles": ["Strategy Manager"]})[:2] == (True, True)
