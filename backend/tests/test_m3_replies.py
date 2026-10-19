"""Part 3d, no database: quoted-text stripping and the confidence gate."""

from types import SimpleNamespace

from app.core.llm import LLMResult
from app.modules.m3_outreach import replies as R


def test_quoted_history_is_dropped():
    text = "Thanks, let's set up a call.\n\nOn Mon, 19 Oct 2026 at 10:00, Ashish <a@gmail.com> wrote:\n> Hi Priya,\n> ..."
    assert R.strip_quoted(text) == "Thanks, let's set up a call."
    assert R.strip_quoted("> only quoted") == ""


class FakeLLM:
    def __init__(self, read):
        self.read = read

    def structured(self, tier, *, system, user, schema, purpose, session=None, subject=None):
        assert tier == "fast" and purpose == "m3.read_reply"
        return LLMResult(parsed=self.read, model="fake", cost_usd=0.0, input_tokens=1, output_tokens=1)


def test_low_confidence_becomes_unsure():
    shaky = R.ReplyRead(category="interested", confidence=0.4, summary="maybe", back_on="", referred_name="",
                        referred_email="", referred_title="")
    assert R.classify(None, FakeLLM(shaky), "hmm", "Application", 1).category == "unsure"
    sure = shaky.model_copy(update={"confidence": 0.95})
    assert R.classify(None, FakeLLM(sure), "Let's talk", "Application", 1).category == "interested"


def test_no_ai_key_means_unsure_never_keep_sending():
    assert R.classify(None, None, "Let's talk", "Application", 1).category == "unsure"
