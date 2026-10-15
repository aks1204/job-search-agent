"""Part 2c, no database: final score maths, caps, bands, feedback overrides, grade schema."""

from types import SimpleNamespace

import pytest

from app.config_loader import get_config
from app.core.llm import strict_schema
from app.modules.m2_jobs import scoring as S

BANDS = get_config().job_rules.scoring.bands


def row(graded=True, dims=None, similarity=0.7, owner_action=None):
    dims = dims if dims is not None else {d: 80 for d in S.DIMENSIONS}
    return SimpleNamespace(graded=graded, grade={"dimensions": dims} if graded else {}, similarity=similarity,
                           owner_action=owner_action, breakdown={}, score=0, band="archive")


def assessment(penalties=(), flags=(), lead_only=False):
    return SimpleNamespace(penalties=list(penalties), flags=list(flags), lead_only=lead_only)


def test_equal_weights_average_the_dimensions():
    score, breakdown = S.final_score(row(dims={d: v for d, v in zip(S.DIMENSIONS, [90, 80, 70, 60, 50, 40])}),
                                     assessment())
    assert score == 65 and breakdown["source"] == "ai_grade"


def test_penalties_subtract_and_are_explained():
    score, breakdown = S.final_score(row(), assessment(penalties=[{"name": "services", "points": -10}]))
    assert score == 70 and breakdown["penalties"][0]["points"] == -10


def test_ungraded_and_leads_never_exceed_maybe():
    rules = get_config().job_rules.scoring
    assert S.final_score(row(graded=False, similarity=0.99), assessment())[0] <= rules.ungraded_max_score
    score, breakdown = S.final_score(row(dims={d: 99 for d in S.DIMENSIONS}), assessment(lead_only=True))
    assert score == rules.ungraded_max_score and "lead only" in breakdown["caps"][0]


def test_unresolved_questions_cap_out_of_fast_lane_but_pay_doubt_does_not():
    great = {d: 95 for d in S.DIMENSIONS}
    capped, b = S.final_score(row(dims=great), assessment(flags=["geo: confirm"]))
    assert capped == BANDS.fast_lane - 1 and "unresolved" in b["caps"][0]
    assert S.final_score(row(dims=great), assessment(flags=["comp: unverified"]))[0] == 95


@pytest.mark.parametrize("score, band", [(90, "fast_lane"), (85, "fast_lane"), (70, "inbox"), (50, "maybe"), (10, "archive")])
def test_bands(score, band):
    assert S.band_for(score) == band


def test_similarity_proxy_is_monotonic_and_bounded():
    values = [S.similarity_proxy(x) for x in (0.4, 0.6, 0.7, 0.8, 0.95)]
    assert values == sorted(values) and values[0] == 0
    assert values[-1] == get_config().job_rules.scoring.ungraded_max_score


def test_owner_feedback_beats_the_score():
    r = row()
    r.owner_action = "skip"
    S._rebanding(r, assessment())
    assert r.band == "archive" and "you chose: skip" in r.breakdown["caps"]
    low = row(graded=False, similarity=0.6)
    low.owner_action = "maybe_rescue"
    S._rebanding(low, assessment())
    assert low.band == "inbox"


def test_grade_schema_is_strict():
    schema = strict_schema(S.Grade)
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]["dimensions"]["required"]) == set(S.DIMENSIONS)
