"""Part 5c, no database: one-tap reasons and how a typed reason maps onto them."""

import pytest

from app.modules.m5_profile import reasons as R


@pytest.mark.parametrize("text, chips", [
    ("too junior", ["too_junior"]),
    ("not my field", ["not_my_field"]),
    ("Hyderabad is too far", ["location"]),
    ("salary too low and night shifts", ["pay", "hours_travel"]),
    ("meh", ["other"]),
    (None, []),
])
def test_typed_reasons_map_to_chips(text, chips):
    assert R.chips_from_text(text) == chips


def test_your_reason_wins_over_a_guess():
    guessed = [{"chip": "pay", "detail": "pay about 14 LPA"}]
    assert R.effective(["location"], guessed) == ["location"]
    assert R.effective([], guessed) == ["pay"]
    assert R.explain([], guessed).startswith("probably: pay about 14")
    assert R.explain(["location"], guessed) == "you said: location"
    assert "unclear" in R.explain([], [])
