"""Module 5a, no database: which profile the app uses, merging a resume read, the review file, freshness."""

import dataclasses
from datetime import date
from types import SimpleNamespace

import pytest

from app.config_loader import load_config
from app.config_schemas import OwnerStub
from app.modules.m5_profile import profile as P
from app.modules.m5_profile import store

STUB = {
    "name": "Ashish Singh", "location": "Gurugram, India", "career_field": "analytics",
    "current_role": {"title": "Associate", "company": "ZS", "started": "2023-01"},
    "employment_history": [{"company": "ZS", "title": "Associate", "start": "2023-01", "end": None}],
    "target_roles": ["Senior Data Analyst"], "level_ladder": ["Analyst", "Senior Analyst", "Manager"],
    "current_level": "Senior Analyst", "notice_period_days": 60,
    "current_compensation": {"annual_ctc": 1800000, "currency": "INR"},
}


def read(**kw):
    base = dict(name="Ashish Kumar Singh", location="Gurugram, India", mobile="+91 90000 00000", linkedin_url="",
                career_field="analytics", total_experience_years=3.4, skills=["SQL", "Python", "Tableau"],
                jobs=[P.ResumeJob(company="ZS Associates", title="Decision Analytics Associate", start="2023-01", end=""),
                      P.ResumeJob(company="Acme", title="Analyst", start="2021-07", end="2022-12")])
    return P.ResumeRead(**{**base, **kw})


def test_the_app_uses_your_confirmed_profile_and_falls_back_to_the_file(tmp_path, monkeypatch):
    import yaml

    (tmp_path / "owner_stub.yaml").write_text(yaml.safe_dump(STUB), encoding="utf-8")
    cfg = load_config(tmp_path)
    assert cfg.owner.name == "Ashish Singh"                     # plain load_config: the file
    live = dataclasses.replace(cfg, profile_from_db=True)
    confirmed = OwnerStub.model_validate({**STUB, "name": "From Profile"})
    monkeypatch.setattr(store, "current_profile", lambda: confirmed)
    assert live.owner.name == "From Profile"                    # the running app: your confirmed profile
    monkeypatch.setattr(store, "current_profile", lambda: None)
    assert live.owner.name == "Ashish Singh"                    # none confirmed yet: the file


def test_merge_takes_facts_from_the_resume_and_keeps_your_answers():
    data, notes = P.merge(STUB, read())
    assert data["current_role"] == {"title": "Decision Analytics Associate", "company": "ZS Associates", "started": "2023-01"}
    assert [j["company"] for j in data["employment_history"]] == ["ZS Associates", "Acme"]
    assert data["total_experience_years"] == store.experience_years(data["employment_history"])  # from the dates
    assert data["mobile"] == "+91 90000 00000"
    assert data["notice_period_days"] == 60 and data["level_ladder"] == STUB["level_ladder"]   # your answers stay
    assert any(c["field"] == "name" for c in notes["changed"])
    assert OwnerStub.model_validate(data)


def test_merge_lists_what_a_resume_cant_say_and_what_it_couldnt_read():
    data, notes = P.merge({}, read(jobs=[P.ResumeJob(company="X", title="Y", start="", end="")]))
    gaps = " ".join(notes["gaps"])
    assert "current_compensation" in gaps and "notice_period_days" in gaps and "level_ladder" in gaps
    assert notes["unclear"] and "start month" in notes["unclear"][0]


def test_review_file_round_trip_and_clear_errors(tmp_path):
    data, notes = P.merge(STUB, read())
    row = SimpleNamespace(data=data, notes=notes)
    path = P.export(row, tmp_path / "profile.yaml")
    text = path.read_text(encoding="utf-8")
    assert text.startswith("# draft: none") and "# Your profile" in text and "profile confirm" in text
    assert P.parse_review(text)["current_role"]["company"] == "ZS Associates"
    with pytest.raises(P.ProfileError, match="current_level"):
        P.parse_review(text.replace("current_level: Senior Analyst", "current_level: Wizard"))


def test_pay_and_notice_need_reconfirming_after_six_months():
    row = SimpleNamespace(data=STUB, confirmed_fields={"current_compensation": "2026-01-01", "notice_period_days": "2026-10-01"})
    assert P.stale_fields(row, today=date(2026, 10, 21)) == ["current_compensation"]
    assert P.stale_fields(None) == []


def test_a_review_file_remembers_which_draft_it_came_from(tmp_path):
    data, notes = P.merge(STUB, read())
    text = P.export(SimpleNamespace(id=42, data=data, notes=notes), tmp_path / "p.yaml").read_text(encoding="utf-8")
    assert P.draft_id_in(text) == 42                    # confirm refuses a file from an older draft
    assert P.draft_id_in("current_role: x") is None


@pytest.mark.parametrize("history, years", [
    ([{"start": "2023-11", "end": None}], 3.0),                                   # 2y11m → 3
    ([{"start": "2024-03", "end": None}], 2.5),                                   # 2y7m → 2.5
    ([{"start": "2024-08", "end": None}], 2.0),                                   # 2y2m → 2
    ([{"start": "2024-07", "end": None}], 2.5),                                   # exactly 2y3m → 2.5 (halves up)
    ([{"start": "2021-07", "end": "2022-12"}, {"start": "2023-01", "end": None}], 5.5),   # two jobs back to back
    ([{"start": "2022-01", "end": "2023-06"}, {"start": "2023-01", "end": None}], 5.0),   # overlap counted once
    ([{"start": "", "end": None}], None),
])
def test_experience_comes_from_job_dates(history, years):
    assert store.experience_years(history, today=date(2026, 10, 21)) == years


def test_the_profile_in_use_keeps_its_experience_current():
    row = SimpleNamespace(id=1, data={**STUB, "total_experience_years": 1.0,
                                      "employment_history": [{"company": "ZS", "title": "A", "start": "2023-11", "end": None}]})
    assert store.as_owner(row).total_experience_years >= 3.0                  # not the stale stored number
