"""Identity resolution against the real database (rolled back after each test).

Names/domains are deliberately odd so they never collide with real seeded data.
"""

import pytest
from sqlalchemy import select

from app.models import Company, CompanySighting, ContactLead
from app.modules.m1_companies.candidate import Candidate, ContactHint
from app.modules.m1_companies.identity import assign_domain, intake

pytestmark = pytest.mark.integration


def _cand(scout="yc", ext="1", name="Quuxly", domain=None, **kw) -> Candidate:
    return Candidate(scout=scout, external_id=ext, name=name, domain=domain, **kw)


def _company(session, domain=None, name=None) -> Company:
    stmt = select(Company)
    stmt = stmt.where(Company.domain == domain) if domain else stmt.where(Company.name == name)
    return session.scalars(stmt).one()


def test_new_with_domain_becomes_candidate_and_reseen_is_seen(db_session):
    assert intake(db_session, _cand(domain="https://www.quuxly-test.io/about")) == "new"
    c = _company(db_session, "quuxly-test.io")
    assert c.status == "candidate" and c.domain_source == "source"
    assert intake(db_session, _cand(domain="quuxly-test.io")) == "seen"
    assert db_session.scalar(select(CompanySighting).where(CompanySighting.company_id == c.id)) is not None


def test_name_only_sighting_joins_known_company(db_session):
    intake(db_session, _cand(domain="quuxly-test.io"))
    assert intake(db_session, _cand(scout="adzuna", ext="job-9", name="Quuxly Technologies Pvt Ltd")) == "matched"
    c = _company(db_session, "quuxly-test.io")
    assert "Quuxly Technologies Pvt Ltd" in c.aliases


def test_same_name_different_domains_stay_separate(db_session):
    intake(db_session, _cand(ext="a", name="Zorbix", domain="zorbix-pay.in"))
    assert intake(db_session, _cand(ext="b", name="Zorbix", domain="zorbix-pizza.com")) == "new"


def test_name_only_company_is_unresolved_until_domain_attaches(db_session):
    assert intake(db_session, _cand(scout="remotive", ext="r1", name="Plimsoll Analytics")) == "new"
    c = _company(db_session, name="Plimsoll Analytics")
    assert c.status == "unresolved" and c.domain is None

    assert intake(db_session, _cand(scout="hn", ext="t:1", name="Plimsoll Analytics", domain="plimsoll-test.ai")) == "matched"
    assert c.domain == "plimsoll-test.ai" and c.status == "candidate"


def test_resolving_to_a_known_domain_merges(db_session):
    intake(db_session, _cand(ext="k", name="Vandelay Industries", domain="vandelay-test.com"))
    intake(db_session, _cand(scout="adzuna", ext="j1", name="Vandelay Imports"))  # different name, no domain
    dup = _company(db_session, name="Vandelay Imports")

    kept = assign_domain(db_session, dup, "vandelay-test.com", source="search")

    assert kept.domain == "vandelay-test.com"
    assert "Vandelay Imports" in kept.aliases
    scouts = set(db_session.scalars(select(CompanySighting.scout).where(CompanySighting.company_id == kept.id)))
    assert scouts == {"yc", "adzuna"}
    assert db_session.scalars(select(Company).where(Company.name == "Vandelay Imports")).first() is None


def test_seed_type_outranks_yc_and_contacts_dedupe(db_session):
    hint = [ContactHint(email="Hiring@Quuxly-Test.io")]
    intake(db_session, _cand(scout="seeds", ext="quuxly-test.io", domain="quuxly-test.io", type_hint="established"))
    intake(db_session, _cand(scout="yc", ext="77", domain="quuxly-test.io", type_hint="startup", contacts=hint))
    intake(db_session, _cand(scout="hn", ext="t:2", domain="quuxly-test.io", contacts=hint))

    c = _company(db_session, "quuxly-test.io")
    assert (c.type, c.type_source) == ("established", "seeds")
    emails = db_session.scalars(select(ContactLead.email).where(ContactLead.company_id == c.id)).all()
    assert emails == ["hiring@quuxly-test.io"]


def test_junk_domains_are_not_identities(db_session):
    intake(db_session, _cand(scout="hn", ext="t:3", name="Glimmerbox", domain="https://jobs.lever.co/glimmerbox"))
    assert _company(db_session, name="Glimmerbox").domain is None
