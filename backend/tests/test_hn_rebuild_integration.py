"""The Hacker News rebuild: the post is the source of truth."""

from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.core.llm import LLMResult
from app.models import Company, CompanyFeed, CompanySighting, ContactLead, RawPosting
from app.models.company import STATUS_CANDIDATE, STATUS_REJECTED, STATUS_WATCHING
from app.modules.m1_companies.candidate import APPLY_MARK, Candidate
from app.modules.m1_companies.hn_rebuild import rebuild
from app.modules.m1_companies.identity import intake
from app.modules.m1_companies.scouts.hn import HNContact, HNRead, HNRole
from app.modules.m1_companies.watch.sync import aggregator_jobs
from app.modules.m3_outreach.pipeline import _designated_for

pytestmark = pytest.mark.integration

POSTS = {   # made-up comment ids, far from real ones
    990000001: "Zz Strategy Consulting (zst) | Head of Development | REMOTE (Germany)"
               "<p>Send your CV to recruiting@zz-strategy-consulting.io",
    990000002: 'Zzgive | Data Analyst | Remote<p>Job description: <a href="https:&#x2F;&#x2F;zz-docs-host.page&#x2F;jd">here</a>',
    990000005: "Zzownedco | Analyst | Remote (worldwide)<p>We build things.",
}
READS = {
    990000001: HNRead(is_hiring_post=True, company_name="Zz Strategy Consulting", website="zz-strategy-consulting.io",
                      careers_url="unknown", what_they_build="AI agent platform", hires_in_india="no",
                      remote_from_india="no", visa="unknown",
                      roles=[HNRole(title="Head of Development", location="Remote (Germany)", apply_url="unknown")],
                      contacts=[HNContact(email="recruiting@zz-strategy-consulting.io", name="unknown", role="unknown",
                                          to_apply=True)]),
    990000002: HNRead(is_hiring_post=True, company_name="Zzgive", website="unknown", careers_url="unknown",
                      what_they_build="unknown", hires_in_india="unknown", remote_from_india="yes", visa="unknown",
                      roles=[HNRole(title="Data Analyst", location="Remote", apply_url="unknown")], contacts=[]),
    990000005: HNRead(is_hiring_post=True, company_name="Zzownedco", website="unknown", careers_url="unknown",
                      what_they_build="Things", hires_in_india="unknown", remote_from_india="yes", visa="unknown",
                      roles=[HNRole(title="Analyst", location="Remote (worldwide)", apply_url="unknown")], contacts=[]),
}


class FakeHTTP:
    def get(self, url, **kw):
        kid = int(url.rsplit("/", 1)[-1].split(".")[0])
        item = {"id": kid, "time": 1, "text": POSTS[kid]} if kid in POSTS else None
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: item)


class FakeLLM:
    def structured(self, tier, *, system, user, schema, purpose, session=None, subject=None):
        kid = next(k for k, text in POSTS.items() if text.split("|")[0][:12] in user)
        return LLMResult(parsed=READS[kid], model="fake", cost_usd=0.0, input_tokens=1, output_tokens=1)


def _misfiled(db_session, kid, name, domain):
    """A company built from one HN post, with a website the old reader got wrong — and a role from the post."""
    intake(db_session, Candidate(scout="hn", external_id=f"900:{kid}", name=name, domain=domain,
                                 data={"comment_id": kid, "thread_id": 900, "headline": POSTS[kid].split("<p>")[0]}))
    c = db_session.scalars(select(Company).where(Company.domain == domain)).one()
    c.status = STATUS_WATCHING
    feed = CompanyFeed(company_id=c.id, method="aggregator", cadence="active", health="ok", detected_vendors=[])
    db_session.add(feed)
    db_session.flush()
    db_session.add(RawPosting(feed_id=feed.id, company_id=c.id, external_id=f"hn:900:{kid}", title="x",
                              payload={}, content_hash="h", lead_only=True))
    db_session.flush()
    return c


def test_a_misfiled_post_moves_to_its_real_company_and_the_wrong_one_is_dropped(db_session):
    # Like the s&t post filed under zs.com, and GiveDirectly under the page hosting its job description.
    big = _misfiled(db_session, 990000001, "Zz Strategy Consulting", "zz-big-consultancy.io")
    docs = _misfiled(db_session, 990000002, "Zzgive", "zz-docs-host.page")
    out = rebuild(db_session, FakeHTTP(), FakeLLM())
    assert out["counts"]["moved"] == 2 and out["counts"]["dropped"] == 2

    real = db_session.scalars(select(Company).where(Company.domain == "zz-strategy-consulting.io")).one()
    assert real.status == STATUS_CANDIDATE and real.reeval_reason == "rules_change"    # judged again
    lead = db_session.scalars(select(ContactLead).where(ContactLead.company_id == real.id)).one()
    assert lead.context.startswith(APPLY_MARK)                                         # "send your CV to …"
    name_only = db_session.scalars(select(CompanySighting).where(
        CompanySighting.external_id == "900:990000002")).one()
    zzgive = db_session.get(Company, name_only.company_id)
    assert zzgive.domain is None and zzgive.name == "Zzgive" and zzgive.status == STATUS_CANDIDATE

    for wrong in (big, docs):
        db_session.refresh(wrong)
        assert wrong.status == STATUS_REJECTED
        post_role = db_session.scalars(select(RawPosting).where(RawPosting.company_id == wrong.id)).one()
        assert post_role.closed_at is not None and post_role.lead_only is False

    again = rebuild(db_session, FakeHTTP(), FakeLLM())
    assert again["counts"].get("moved", 0) == 0 and again["counts"].get("dropped", 0) == 0   # nothing left to fix


def test_every_role_in_a_post_is_its_own_job_with_its_own_link(db_session):
    intake(db_session, Candidate(scout="hn", external_id="900:990000003", name="Zzroles", name_only_ok=True,
                                 url="https://news.ycombinator.com/item?id=990000003",
                                 data={"text": "post", "roles": [
                                     {"title": "Senior Data Analyst", "location": "Bengaluru",
                                      "apply_url": "https://jobs.ashbyhq.com/zzroles/1"},
                                     {"title": "Analytics Engineer", "location": "Remote", "apply_url": None}]}))
    c = db_session.scalars(select(Company).where(Company.normalized_name == "zzroles")).one()
    assert c.status == STATUS_CANDIDATE and c.domain is None        # judged from the post: no web search
    jobs = aggregator_jobs(db_session, c.id)
    assert [(j.external_id, j.url) for j in jobs] == [
        ("hn:900:990000003", "https://jobs.ashbyhq.com/zzroles/1"),
        ("hn:900:990000003:1", "https://news.ycombinator.com/item?id=990000003")]


def test_the_posts_apply_address_is_the_only_one_for_that_companys_roles(db_session):
    intake(db_session, Candidate(scout="hn", external_id="900:990000004", name="Zzapply", domain="zzapply.io",
                                 data={"text": "post"}))
    c = db_session.scalars(select(Company).where(Company.domain == "zzapply.io")).one()
    db_session.add_all([
        ContactLead(company_id=c.id, email="press@zzapply.io", source="hn", context="HN post: Zzapply"),
        ContactLead(company_id=c.id, email="priya@zzapply.io", name="Priya", source="hn",
                    context=APPLY_MARK + "HN post: Zzapply"),
        ContactLead(company_id=c.id, email="rahul@zzapply.io", name="Rahul", source="hn",
                    context=APPLY_MARK + "HN post: Zzapply")])
    db_session.flush()
    board_role = SimpleNamespace(description="Great role.", external_id="greenhouse:7", lead_only=False)
    email, _, more = _designated_for(db_session, board_role, c)
    # "email Priya or Rahul to apply": both get their own email; press@ (not an apply address) neither
    assert sorted([email, *more]) == ["priya@zzapply.io", "rahul@zzapply.io"]

    from app.models import RoleChannel
    from app.modules.m3_outreach.campaigns import _recipients

    ch = RoleChannel(cluster_id=0, company_id=c.id, mode="designated", designated_email=email, also_emails=more)
    assert sorted((r["to_email"], r["to_name"]) for r in _recipients(db_session, ch)) == [
        ("priya@zzapply.io", "Priya"), ("rahul@zzapply.io", "Rahul")]           # each greeted by name


def test_a_post_without_a_website_never_joins_a_same_name_company_elsewhere(db_session):
    intake(db_session, Candidate(scout="seeds", external_id="zzsame-test.io", name="Zzsame", domain="zzsame-test.io"))
    intake(db_session, Candidate(scout="hn", external_id="900:990000006", name="Zzsame", name_only_ok=True,
                                 data={"text": "post"}))
    rows = db_session.scalars(select(Company).where(Company.normalized_name == "zzsame")).all()
    assert sorted(c.domain or "" for c in rows) == ["", "zzsame-test.io"]   # two companies, not one


def test_a_company_you_decided_on_yourself_is_never_judged_again(db_session):
    from app.modules.m1_companies.override import override

    intake(db_session, Candidate(scout="hn", external_id="900:990000005", name="Zzownedco", name_only_ok=True,
                                 data={"comment_id": 990000005, "thread_id": 900, "headline": "Zzownedco | Analyst"}))
    c = db_session.scalars(select(Company).where(Company.normalized_name == "zzownedco")).one()
    override(db_session, c, "fail", "not for me")
    out = rebuild(db_session, FakeHTTP(), FakeLLM())
    db_session.refresh(c)
    assert c.status == STATUS_REJECTED and out["counts"].get("kept_your_decision") == 1


def test_another_companys_address_from_a_post_is_never_used(db_session):
    from app.modules.m3_outreach.pipeline import _company_address

    apple = SimpleNamespace(domain="zzapple.com")
    assert _company_address("jobs@zzapple.com", apple)
    assert _company_address("founder.zz@gmail.com", apple)            # a founder's own inbox
    assert not _company_address("hiring@zzapplepie.io", apple)        # "Apple Pie Labs" is someone else
    assert not _company_address("hiring@zzapplepie.io", SimpleNamespace(domain=None))
