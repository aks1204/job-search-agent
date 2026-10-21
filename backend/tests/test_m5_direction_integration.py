"""Part 5b against the real database with a fake AI: view → suggestions → your yes / no (rolled back)."""

import pytest
from sqlalchemy import select

from app.core.llm import LLMResult
from app.models import DirectionSuggestion, Notification
from app.modules.m5_profile import direction as D
from app.modules.m5_profile import profile as P
from app.modules.m5_profile import store
from tests.test_m5_profile import STUB

pytestmark = pytest.mark.integration


class FakeLLM:
    def __init__(self, view):
        self.view = view

    def structured(self, tier, *, system, user, schema, purpose, session=None, subject=None):
        assert purpose == "m5.direction" and "Excluded families: risk analytics" in user
        return LLMResult(parsed=self.view, model="fake", cost_usd=0.03, input_tokens=1, output_tokens=1)


def _view():
    return D.DirectionView(
        position="Senior Analyst at ZS, ~3 years", trajectory="towards industry analytics",
        families=[D.FamilyView(family="business analytics", verdict="progression", condition="", why="3 years of it",
                               strength="strong"),
                  D.FamilyView(family="applied data science", verdict="progression", condition="",
                               why="built forecasting models", strength="strong"),
                  D.FamilyView(family="risk analytics", verdict="progression", condition="", why="some credit work",
                               strength="moderate")],
        target_roles=[D.TitleView(title="Senior Data Analyst", action="keep", why="fits", strength="strong")],
        lateral_appetite="medium", appetite_why="", track="individual_contributor", open_to_management=False, track_why="")


@pytest.fixture
def confirmed(db_session, monkeypatch):
    monkeypatch.setattr(P, "resume_text", lambda: "")
    data = {**STUB, "role_families": {"progression": ["business analytics"], "lateral": [], "excluded": ["risk analytics"]}}
    row = P.confirm(db_session, data)
    yield row
    store.invalidate()


def test_differences_become_questions_and_nothing_changes_by_itself(db_session, confirmed):
    out = D.refresh(db_session, FakeLLM(_view()))
    assert out["new_suggestions"] == 2
    open_ = db_session.scalars(select(DirectionSuggestion).where(DirectionSuggestion.status == "open")).all()
    assert sorted((s.kind, s.item) for s in open_) == [("add", "applied data science"), ("include", "risk analytics")]
    assert confirmed.data["role_families"]["excluded"] == ["risk analytics"]       # untouched until you answer
    assert db_session.scalars(select(Notification).where(Notification.subject_type == "direction")).all()
    info = D.explained(db_session)
    assert info["progression"][0]["why"] == "3 years of it" and info["position"].startswith("Senior Analyst")


def test_your_yes_changes_the_lists_and_your_no_is_remembered(db_session, confirmed):
    D.refresh(db_session, FakeLLM(_view()))
    by_item = {s.item: s for s in db_session.scalars(select(DirectionSuggestion).where(DirectionSuggestion.status == "open"))}
    D.answer(db_session, by_item["applied data science"].id, yes=True)
    D.answer(db_session, by_item["risk analytics"].id, yes=False)
    owner = store.as_owner(store.load_confirmed(db_session))
    assert "applied data science" in owner.role_families.progression
    assert owner.role_families.excluded == ["risk analytics"]                     # your no: still excluded
    D.refresh(db_session, FakeLLM(_view()))                                       # same resume: not asked again
    again = db_session.scalars(select(DirectionSuggestion).where(DirectionSuggestion.status == "open")).all()
    assert not again


def test_your_own_edit_of_the_lists(db_session, confirmed, tmp_path):
    path = D.export(db_session, tmp_path / "direction.yaml")
    text = path.read_text(encoding="utf-8").replace("lateral_appetite: medium", "lateral_appetite: high")
    D.confirm_edit(db_session, text)
    assert store.as_owner(store.load_confirmed(db_session)).lateral_appetite == "high"
    with pytest.raises(D.DirectionError):
        D.confirm_edit(db_session, "name: Someone Else")                       # only direction fields here
