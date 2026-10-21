"""Part 5c against the real database (local embeddings, fake AI): answers, decisions, patterns, ageing, clashes."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.core.llm import LLMResult
from app.models import (
    CareerDirection, Company, CompanyFeed, DirectionSuggestion, JobAssessment, JobFeedback, JobPosting, JobScore,
    MemoryEntry, Notification, RawPosting,
)
from app.modules.m1_companies.candidate import Candidate
from app.modules.m1_companies.identity import intake as company_intake
from app.modules.m2_jobs.pipeline import intake
from app.modules.m5_profile import memory as M
from app.modules.m5_profile import profile as P
from app.modules.m5_profile import store
from tests.test_m5_profile import STUB

pytestmark = pytest.mark.integration
FAMILY = "zz test pattern analytics"  # made up, so your real feedback can't interfere


class FakeLLM:
    def __init__(self, parsed):
        self.parsed, self.prompts = parsed, []

    def structured(self, tier, *, system, user, schema, purpose, session=None, subject=None):
        self.prompts.append(user)
        return LLMResult(parsed=self.parsed, model="fake", cost_usd=0.0, input_tokens=1, output_tokens=1)


@pytest.fixture(autouse=True)
def _clean():
    yield
    store.invalidate()


def test_asked_at_most_once(db_session):
    M.record_answer(db_session, "Move 'sales analytics' to your excluded families?", "no", module="m5.direction")
    assert M.already_answered(db_session, "Move 'sales analytics' to your excluded families?").content["answer"] == "no"
    assert M.already_answered(db_session, "What is your favourite programming language?") is None


def test_your_decisions_become_examples_for_similar_roles(db_session):
    M.record_role_feedback(db_session, company="Acme Bank", title="Credit Risk Analyst", family="risk analytics",
                           level="Analyst", action="skip", reason="too junior and risk-heavy", cluster_id=1)
    examples = M.examples_for_job(db_session, "Risk Analyst at Other Bank. Credit risk modelling")
    assert any("too junior and risk-heavy" in e for e in examples)


def _role(db_session, family):
    domain = "m5mem-test.io"
    company_intake(db_session, Candidate(scout="seeds", external_id=domain, name="Mem Co", domain=domain,
                                         type_hint="established"))
    company = db_session.scalars(select(Company).where(Company.domain == domain)).one()
    feed = CompanyFeed(company_id=company.id, method="greenhouse", slug="m5m", cadence="active", health="ok",
                       detected_vendors=[])
    db_session.add(feed)
    db_session.flush()
    now = datetime.now(timezone.utc)
    db_session.add(RawPosting(feed_id=feed.id, company_id=company.id, external_id="1", title="Sales Analyst",
                              location="Pune, India", posted_at=now.isoformat(), payload={}, description="x " * 200,
                              content_hash="m5", first_seen_at=now, last_seen_at=now))
    db_session.flush()
    intake(db_session, company_ids=[company.id])
    job = db_session.scalars(select(JobPosting).where(JobPosting.company_id == company.id)).one()
    a = JobAssessment(cluster_id=job.cluster_id, job_id=job.id, content_hash=job.content_hash, profile_version="t",
                      rules_version="t", outcome="passed", read={"role_family": family}, reasons=[], flags=[],
                      penalties=[], comp={}, level={})
    db_session.add(a)
    db_session.flush()
    db_session.add(JobScore(cluster_id=job.cluster_id, assessment_id=a.id, similarity=0.7, similarity_version="t",
                            score=70, band="inbox"))
    db_session.flush()
    return job.cluster_id


def test_a_repeated_pattern_becomes_a_suggestion_never_a_change(db_session, monkeypatch):
    monkeypatch.setattr(P, "resume_text", lambda: "")
    row = P.confirm(db_session, {**STUB, "role_families": {"progression": [FAMILY], "lateral": [], "excluded": []}})
    db_session.add(CareerDirection(status="current", view={}, profile_id=row.id))
    cluster = _role(db_session, FAMILY)
    for _ in range(4):
        db_session.add(JobFeedback(cluster_id=cluster, action="skip", reason="not for me"))
    db_session.flush()
    out = M.update_taste(db_session)
    assert out["signals"] >= 1 and out["suggested"] == 1
    s = db_session.scalars(select(DirectionSuggestion).where(DirectionSuggestion.item == FAMILY,
                                                             DirectionSuggestion.kind == "remove",
                                                             DirectionSuggestion.status == "open")).one()
    assert s.strength == "taste"
    assert FAMILY in store.as_owner(store.load_confirmed(db_session)).role_families.progression  # untouched


def test_ageing_expires_money_answers_and_archives_the_unused_but_not_pinned(db_session):
    pay = M.record_answer(db_session, "What is your notice period, test?", "60 days", module="m5.profile", expires_days=1)
    pay.expires_at = datetime.now(timezone.utc) - timedelta(days=1)
    old = M.record_answer(db_session, "Which football club do you support?", "Mohun Bagan", module="test")
    pinned = M.record_answer(db_session, "Would you ever relocate to Antarctica for work?", "never", module="test")
    pinned.pinned = True
    for e in (old, pinned):
        e.last_used_at = datetime.now(timezone.utc) - timedelta(days=400)
    M.age(db_session)
    assert pay.status == "needs_reconfirm" and old.status == "archived" and pinned.status == "active"


def test_after_a_resume_update_clashes_are_shown_never_changed(db_session, monkeypatch):
    monkeypatch.setattr(P, "resume_text", lambda: "")
    P.confirm(db_session, STUB)
    e = M.record_role_feedback(db_session, company="Big Co", title="Analytics Manager", family="business analytics",
                               level="Manager", action="skip", reason="too senior for me", cluster_id=1)
    fake = FakeLLM(M.Clashes(clashes=[M.Clash(id=e.id, why="you're now at that level")]))
    out = M.check_clashes(db_session, fake)
    assert out["clashes"] == 1 and e.status == "active" and e.content["clash"]["why"].startswith("you're now")
    assert db_session.scalars(select(Notification).where(Notification.subject_type == "memory",
                                                         Notification.subject_id == str(e.id))).one()
    assert M.check_clashes(db_session, fake) == {}                              # once per confirmed profile
    M.act(db_session, e.id, "keep")
    assert "clash" not in e.content


def test_pay_never_goes_to_the_ai_summary(db_session):
    M.record_answer(db_session, "What is your current pay?", "1234567 INR a year", module="m5.profile", expires_days=180)
    M.record_answer(db_session, "Do you like startups, test?", "no", module="test")
    fake = FakeLLM(M.Beliefs(topics=[M.Belief(topic="Roles", statements=["You don't want startups."])]))
    from app.core.redis_client import get_redis

    for k in get_redis().scan_iter("m5:beliefs:*"):
        get_redis().delete(k)
    assert M.beliefs(db_session, fake).topics[0].topic == "Roles"
    assert "1234567" not in fake.prompts[0] and "kept private" in fake.prompts[0]
    assert db_session.scalars(select(MemoryEntry).where(MemoryEntry.meaning.like("%1234567%"))).all()  # kept locally


def test_one_word_apart_questions_are_never_merged(db_session):
    a = M.record_answer(db_session, "Move 'zz sales analytics' to your excluded families?", "no", module="m5.direction")
    b = M.record_answer(db_session, "Move 'zz marketing analytics' to your excluded families?", "yes", module="m5.direction")
    assert a.id != b.id
    assert M.already_answered(db_session, "Move 'zz sales analytics' to your excluded families?").content["answer"] == "no"
    again = M.record_answer(db_session, "Move 'zz sales analytics' to your excluded families?", "yes", module="m5.direction")
    assert again.id == a.id and a.content["answer"] == "yes"                    # same question: updated, not duplicated


def test_a_forgotten_pattern_stays_forgotten(db_session, monkeypatch):
    monkeypatch.setattr(P, "resume_text", lambda: "")
    P.confirm(db_session, STUB)
    cluster = _role(db_session, FAMILY)
    for _ in range(4):
        db_session.add(JobFeedback(cluster_id=cluster, action="skip", reason="no"))
    db_session.flush()
    M.update_taste(db_session)
    signal = db_session.scalars(select(MemoryEntry).where(MemoryEntry.kind == "taste_signal",
                                                          MemoryEntry.content["family"].astext == FAMILY)).one()
    M.act(db_session, signal.id, "forget")
    M.update_taste(db_session)
    assert signal.status == "archived"
    assert len(db_session.scalars(select(MemoryEntry).where(MemoryEntry.kind == "taste_signal",
                                                            MemoryEntry.content["family"].astext == FAMILY)).all()) == 1


def test_saying_yes_to_a_family_sets_aside_old_dislikes_and_asks_about_old_skips(db_session, monkeypatch):
    from app.modules.m5_profile import direction as D

    monkeypatch.setattr(P, "resume_text", lambda: "")
    row = P.confirm(db_session, {**STUB, "role_families": {"progression": [], "lateral": [], "excluded": [FAMILY]}})
    direction = CareerDirection(status="current", view={}, profile_id=row.id)
    db_session.add(direction)
    db_session.flush()
    cluster = _role(db_session, FAMILY)
    for _ in range(4):
        db_session.add(JobFeedback(cluster_id=cluster, action="skip", reason="not my field"))
    M.record_role_feedback(db_session, company="Mem Co", title="Sales Analyst", family=FAMILY, level="Analyst",
                           action="skip", reason="not my field", cluster_id=cluster)
    db_session.flush()
    M.update_taste(db_session)
    pattern = db_session.scalars(select(MemoryEntry).where(MemoryEntry.kind == "taste_signal",
                                                           MemoryEntry.content["family"].astext == FAMILY,
                                                           MemoryEntry.status == "active")).one()
    yes = DirectionSuggestion(direction_id=direction.id, kind="include", target="progression", item=FAMILY,
                              why="w", strength="strong", status="open")
    db_session.add(yes)
    db_session.flush()
    D.answer(db_session, yes.id, yes=True)                                         # you now include it
    assert pattern.status == "archived"                                            # the old dislike is set aside
    M.update_taste(db_session)
    assert pattern.status == "archived"                                            # old skips don't revive it
    ask = db_session.scalars(select(DirectionSuggestion).where(DirectionSuggestion.kind == "memory",
                                                               DirectionSuggestion.item == FAMILY)).one()
    skip_memory = M.old_skips_of(db_session, FAMILY)[0]
    D.answer(db_session, ask.id, yes=True)                                         # stop using old skips
    assert skip_memory.status == "archived"
    assert skip_memory not in M.listing(db_session)                                # no longer shown or used
