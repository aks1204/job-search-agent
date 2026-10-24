"""Owner rule: current and past employers are never watched or contacted."""

import pytest
from sqlalchemy import select

from app.core.llm import LLMResult
from app.models import Company, CompanyVerdict, Notification
from app.models.company import STATUS_BLOCKED, STATUS_CANDIDATE, STATUS_WATCHING
from app.models.employer import OwnerEmployer
from app.modules.m1_companies import employers as E
from app.modules.m1_companies.candidate import Candidate
from app.modules.m1_companies.evaluator import evaluate
from app.modules.m1_companies.identity import intake
from app.modules.m1_companies.override import override
from app.modules.m1_companies.resolver import DomainGuess

pytestmark = pytest.mark.integration

# Made-up employers, so your real profile can't interfere.
PROFILE = [("Zzemp Associates", True), ("Oldplace Analytics Ltd", False), ("Self-employed", False),
           ("Global Technologies Ltd", False)]


class FakeLLM:
    def __init__(self, domain, confidence="high"):
        self.guess, self.calls = DomainGuess(domain=domain, confidence=confidence), 0

    def structured(self, tier, *, system, user, schema, purpose, session=None, subject=None):
        self.calls += 1
        return LLMResult(parsed=self.guess, model="fake", cost_usd=0.0, input_tokens=1, output_tokens=1)


@pytest.fixture(autouse=True)
def _profile(monkeypatch):
    monkeypatch.setattr(E, "_from_profile", lambda: [(n, c) for n, c in PROFILE
                                                     if E.normalize_name(n) not in E.NOT_EMPLOYERS])
    monkeypatch.setattr(E, "_site_text", lambda http, domain: "Welcome to ZZEMP. We are Zzemp Associates.")


def _company(db_session, name, domain, status=STATUS_WATCHING):
    intake(db_session, Candidate(scout="seeds", external_id=f"emp-{domain}", name=name, domain=domain,
                                 type_hint="established"))
    c = db_session.scalars(select(Company).where(Company.domain == domain)).one()
    c.status = status
    db_session.flush()
    return c


def _row(db_session, key):
    return db_session.scalar(select(OwnerEmployer).where(OwnerEmployer.normalized == key))


def test_how_a_website_fits_an_employer_name():
    assert E._fit("Zzemp Associates", "zzemp.io") == "first"          # like ZS Associates → zs.com
    assert E._fit("McKinsey & Company", "mckinsey.com") == "full"
    assert E._fit("Bristol Myers Squibb", "bms.com") == "full"
    assert E._fit("American Zzexpress", "american.com") is None        # a generic first word never fits
    assert E._fit("Zzemp Associates", "somethingelse.io") is None
    assert E._says("© Bristol-Myers Squibb Company", "bristol myers squibb")
    assert not E._says("They make keys", "ey")                         # whole words only


def test_your_employer_is_found_by_website_even_under_another_name(db_session):
    # Stored under its site title, as ZS is ("Strategy & Transformation Consulting"): only the website matches.
    c = _company(db_session, "Zz Strategy & Transformation Consulting", "zzemp.io")
    out = E.refresh(db_session, FakeLLM("zzemp.io"), None)
    assert out["blocked"] == 1
    db_session.refresh(c)
    assert c.status == STATUS_BLOCKED
    v = db_session.scalars(select(CompanyVerdict).where(CompanyVerdict.company_id == c.id)
                           .order_by(CompanyVerdict.id.desc())).first()
    assert v.failed_check == "former_employer" and v.permanent and "current employer" in v.summary
    note = db_session.scalars(select(Notification).where(Notification.subject_type == "company",
                                                         Notification.subject_id == str(c.id))).one()
    assert "Not watching" in note.title
    assert [x.id for x in E.blocked_for(db_session, E.employers(db_session)[0])] == [c.id]
    assert E.refresh(db_session, FakeLLM("zzemp.io"), None)["blocked"] == 0   # blocked once, one note


def test_a_less_sure_guess_is_fine_when_it_is_a_website_the_agent_knows(db_session):
    # What happened with ZS: the AI was only medium-sure that "ZS Associates" is zs.com.
    c = _company(db_session, "Zz Strategy & Transformation Consulting", "zzemp.io")
    assert E.refresh(db_session, FakeLLM("zzemp.io", confidence="medium"), None)["blocked"] == 1
    db_session.refresh(c)
    assert c.status == STATUS_BLOCKED


def test_a_guess_the_homepage_doesnt_back_is_not_trusted_and_you_are_asked(db_session, monkeypatch):
    monkeypatch.setattr(E, "_site_text", lambda http, domain: "They make keys and other things.")
    E.refresh(db_session, FakeLLM("zzemp.net"), None)                   # not a website the agent knows
    row = _row(db_session, "zzemp associates")
    assert row.domain is None and row.source == "none" and row.told_at is not None
    ask = db_session.scalars(select(Notification).where(Notification.subject_type == "owner_employer",
                                                        Notification.subject_id == "zzemp associates")).one()
    assert "employers fix" in ask.detail


def test_an_unreachable_ai_is_tried_again_and_then_you_are_asked(db_session):
    for _ in range(E.ASK_AFTER):
        E.refresh(db_session, None, None)                                # no AI key
    row = _row(db_session, "zzemp associates")
    assert row.domain is None and row.source == "pending" and row.attempts == E.ASK_AFTER
    assert row.told_at is not None


def test_not_an_employer_and_generic_names_never_block_strangers(db_session):
    assert all(e.name != "Self-employed" for e in E.employers(db_session))
    c = _company(db_session, "Global", "zz-some-global.io")              # "Global Technologies Ltd" → "global"
    assert E.match(db_session, c) is None


def test_same_name_but_a_different_website_is_a_different_company(db_session):
    E.fix(db_session, "Oldplace Analytics Ltd", "oldplace-zz.io")
    other = _company(db_session, "Oldplace Analytics", "another-oldplace-zz.io")
    assert E.match(db_session, other) is None


def test_a_past_employer_found_later_is_rejected_before_any_ai_is_used(db_session):
    c = _company(db_session, "Oldplace Analytics", "oldplace-zz.io", status="candidate")
    v = evaluate(db_session, c, llm=None, http=None)                    # would crash if it reached the AI
    assert not v.passed and v.failed_check == "former_employer" and "past employer" in v.summary


def test_your_own_keep_after_the_block_wins(db_session):
    c = _company(db_session, "Oldplace Analytics", "oldplace-zz.io")
    assert E.apply(db_session) == 1
    override(db_session, c, "pass", "I'd go back there")
    assert E.match(db_session, c) is None
    assert E.apply(db_session) == 0
    db_session.refresh(c)
    assert c.status != STATUS_BLOCKED


def test_nothing_automatic_ever_unblocks_your_employer(db_session):
    c = _company(db_session, "Zz Strategy Consulting", "zzemp.io")
    E.fix(db_session, "Zzemp Associates", "zzemp.io")
    assert E.apply(db_session) == 1
    db_session.delete(_row(db_session, "zzemp associates"))             # e.g. the website row went missing
    db_session.flush()
    E.refresh(db_session, None, None)
    db_session.refresh(c)
    assert c.status == STATUS_BLOCKED


def test_fixing_a_wrong_website_releases_only_the_wrongly_blocked_company(db_session):
    wrong = _company(db_session, "Unrelated Zz Co", "unrelated-zz.io")
    E.fix(db_session, "Zzemp Associates", "unrelated-zz.io")
    assert E.apply(db_session) == 1
    right = _company(db_session, "Something Else Entirely", "zzemp-real.io")
    e, released = E.fix(db_session, "Zzemp Associates", "zzemp-real.io")
    assert e.domain == "zzemp-real.io" and released == 1
    assert E.apply(db_session) == 1
    db_session.refresh(wrong)
    db_session.refresh(right)
    assert wrong.status == STATUS_CANDIDATE and right.status == STATUS_BLOCKED
    with pytest.raises(ValueError):
        E.fix(db_session, "Zzemp Associates", "zs")                      # not a website
    assert E.fix(db_session, "Not In My Profile", "x.io") == (None, 0)
