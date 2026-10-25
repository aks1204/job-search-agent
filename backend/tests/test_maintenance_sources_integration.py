"""The source report, and the fixes it points to (names ok, fix-website)."""

import pytest
from sqlalchemy import select

from app.models import Company, CompanyFeed, CompanySighting, CompanyVerdict, Notification
from app.modules.m1_companies import fix_website as F
from app.modules.maintenance.sources import build, fits_website, name_fits, weekly_check

pytestmark = pytest.mark.integration
SRC = "zzsrc"   # a made-up source, so your real data can't interfere


def _company(db_session, i, name, domain, status, feed=None, scout=SRC):
    c = Company(name=name, normalized_name=name.lower(), domain=domain, status=status, aliases=[], source_data={})
    db_session.add(c)
    db_session.flush()
    db_session.add(CompanySighting(company_id=c.id, scout=scout, external_id=f"zz-{scout}-{i}", name_seen=name))
    if status == "watching":
        db_session.add(CompanyVerdict(company_id=c.id, passed=True, profile="startup", summary="kept", checks=[],
                                      judgment={}, trigger="new", decided_by="machine", rules_versions={},
                                      precedent_ids=[]))
    if feed:
        db_session.add(CompanyFeed(company_id=c.id, method=feed, cadence="active", health="ok", detected_vendors=[],
                                   ownership_evidence="aggregator floor: jobs are on icims (no reader yet); 2 listings"
                                   if feed == "aggregator" else "linked from site"))
    db_session.flush()
    return c


def _row(db_session):
    return next(r for r in build(db_session, days=90)["rows"] if r.scout == SRC)


def test_a_broken_source_shows_where_it_breaks(db_session):
    for i, name in enumerate(["Zzkepta", "Zzkeptb", "Zzkeptc"]):
        _company(db_session, i, name, f"{name.lower()}.io", "watching", feed="aggregator")   # jobs not read directly
    _company(db_session, 3, "Zzkeptd", "zzkeptd.io", "watching", feed="greenhouse")
    _company(db_session, 4, "Zzmisfit", "elsewhere-zz.io", "watching", feed="greenhouse")     # name ≠ website
    for i in range(5, 12):
        _company(db_session, i, f"Zznosite{i}", None, "unresolved")                         # no website yet

    r = _row(db_session)
    assert (r.found, r.with_website, r.judged, r.kept_of_found, r.looking_for_website) == (12, 5, 5, 5, 7)
    assert (r.kept, r.read_directly, r.via_job_sites, r.only_here) == (5, 2, 3, 5)
    assert r.last_new_company is not None
    assert {w.key for w in r.warnings} == {f"{SRC}:names", f"{SRC}:not_read"}    # waiting is counted, not warned
    names = next(w for w in r.warnings if w.key.endswith(":names"))
    assert names.examples == ["Zzmisfit (elsewhere-zz.io)"]
    assert "icims 3" in next(w for w in r.warnings if w.key.endswith(":not_read")).text


def test_a_healthy_source_has_no_warnings(db_session):
    for i, name in enumerate(["Zzfinea", "Zzfineb", "Zzfinec"]):
        _company(db_session, i, name, f"{name.lower()}.io", "watching", feed="greenhouse")
    r = _row(db_session)
    assert r.healthy and r.kept == 3 and r.read_directly == 3


def test_marking_a_name_correct_stops_the_warning(db_session):
    _company(db_session, 0, "Zzokname", "zzokname.io", "watching", feed="greenhouse")
    _company(db_session, 1, "Zzokname2", "zzokname2.io", "watching", feed="greenhouse")
    odd = _company(db_session, 2, "Zzeventual", "zzdaft.ai", "watching", feed="greenhouse")  # a product website
    assert not fits_website(odd) and any(w.key.endswith(":names") for w in _row(db_session).warnings)
    odd.source_data = {**odd.source_data, "owner": {"name_ok": "zzdaft.ai"}}
    db_session.flush()
    assert fits_website(odd) and _row(db_session).healthy


def test_the_weekly_check_notes_each_new_warning_once(db_session):
    _company(db_session, 0, "Zzbada", "elsewhere-a-zz.io", "watching", feed="greenhouse")
    _company(db_session, 1, "Zzgoodb", "zzgoodb.io", "watching", feed="greenhouse")
    _company(db_session, 2, "Zzgoodc", "zzgoodc.io", "watching", feed="greenhouse")
    weekly_check(db_session)
    weekly_check(db_session)                                                                # a week later: same warning
    notes = db_session.scalars(select(Notification).where(Notification.subject_type == "source_warning",
                                                          Notification.subject_id == f"{SRC}:names")).all()
    assert len(notes) == 1 and notes[0].section == "housekeeping" and "names don't fit" in notes[0].title


def test_names_that_fit_their_websites():
    assert name_fits("Eli Lilly", "lilly.com") and name_fits("Boston Consulting Group", "bcg.com")
    assert name_fits("Mastra", "mastra.ai") and not name_fits("GiveDirectly", "perfectwiki.page")




# --- correcting a company's website or name: it starts over ------------------------------------------------

def _wrongly_filed(db_session):
    """Like s&t filed under zs.com: its post (a role, an address) plus everything built from the wrong site."""
    from app.models import CompanyFact, ContactEmail, ContactLead, JobCluster, OutreachDraft, RawPosting

    c = _company(db_session, 0, "Zz Strategy Consulting", "zz-wrongsite.io", "watching", feed="greenhouse",
                 scout="hn")
    s = db_session.scalars(select(CompanySighting).where(CompanySighting.company_id == c.id)).one()
    s.data = {"roles": [{"title": "Head of Development", "location": "Remote (Germany)"}]}
    feed = db_session.scalars(select(CompanyFeed).where(CompanyFeed.company_id == c.id)).one()
    cl = JobCluster(company_id=c.id, fingerprint="zz-fp", title="Partner")
    db_session.add_all([
        CompanyFact(company_id=c.id, key="employee_estimate", value="10000+", confidence="inferred", source="website"),
        ContactLead(company_id=c.id, email="recruiting@zz-rightsite.io", source="hn", context="HN post"),
        ContactEmail(company_id=c.id, email="someone@zz-wrongsite.io", source="hunter"),
        RawPosting(feed_id=feed.id, company_id=c.id, external_id="gh-123", title="Partner", payload={},
                   content_hash="b"),
        cl,
    ])
    db_session.flush()
    db_session.add(OutreachDraft(cluster_id=cl.id, scenario="fresh", role_name="Partner", subject="s", body="b",
                                 intro="", points=[], req_ids=[], status="ready", truth={}, inputs_version="v"))
    db_session.flush()
    return c


def test_correcting_the_website_keeps_only_the_post_and_starts_over(db_session):
    from app.models import CompanyFact, ContactEmail, ContactLead, JobCluster

    c = _wrongly_filed(db_session)
    p = F.plan(db_session, c, new_domain="zz-rightsite.io")
    assert p.kept_roles == ["Head of Development (Remote (Germany))"]
    assert p.kept_addresses == ["recruiting@zz-rightsite.io"]
    assert (p.site_roles, p.found_addresses, p.drafts) == (1, 1, 1)
    assert any("website zz-wrongsite.io → zz-rightsite.io" in line for line in p.describe())
    F.apply(db_session, p)
    db_session.refresh(c)
    assert (c.domain, c.name, c.status, c.type) == ("zz-rightsite.io", "Zz Strategy Consulting", "candidate", "unknown")
    assert db_session.scalars(select(CompanySighting).where(CompanySighting.company_id == c.id)).one().scout == "hn"
    assert db_session.scalars(select(ContactLead).where(ContactLead.company_id == c.id)).one().email \
        == "recruiting@zz-rightsite.io"                                    # the post's address stays
    for model in (CompanyFact, ContactEmail, JobCluster, CompanyFeed, CompanyVerdict):  # everything else: redone
        assert not db_session.scalars(select(model).where(model.company_id == c.id)).all()


def test_correcting_the_name_starts_over_too(db_session):
    c = _wrongly_filed(db_session)
    p = F.plan(db_session, c, new_name="Zz S&T Consulting")
    assert any("→ Zz S&T Consulting (website zz-wrongsite.io stays)" in line for line in p.describe())
    F.apply(db_session, p)
    db_session.refresh(c)
    assert c.name == "Zz S&T Consulting" and c.domain == "zz-wrongsite.io" and c.status == "candidate"


def test_two_records_of_one_company_become_one(db_session):
    c = _wrongly_filed(db_session)
    other = _company(db_session, 1, "Zz S&T", "zz-rightsite.io", "watching", scout="adzuna")
    other_id = other.id
    p = F.plan(db_session, c, new_domain="zz-rightsite.io")
    assert p.other_id == other_id
    F.apply(db_session, p)
    assert db_session.get(Company, other_id) is None
    scouts = {s.scout for s in db_session.scalars(select(CompanySighting).where(CompanySighting.company_id == c.id))}
    assert scouts == {"hn", "adzuna"}


def test_corrections_refuse_nonsense(db_session):
    c = _company(db_session, 0, "Zzsame", "zzsame.io", "watching")
    for bad in ("zzsame.io", "zs", "linkedin.com"):
        with pytest.raises(ValueError):
            F.plan(db_session, c, new_domain=bad)
    for bad in ("", "Zzsame"):
        with pytest.raises(ValueError):
            F.plan(db_session, c, new_name=bad)


def test_a_source_that_named_the_old_website_stays_with_it(db_session):
    # Your seed list said "ZS, zs.com"; the s&t post was filed on the same record. Fixing it keeps ZS.
    c = _wrongly_filed(db_session)
    db_session.add(CompanySighting(company_id=c.id, scout="seeds", external_id="zz-seed-1", name_seen="Zzwrongsite",
                                   domain_seen="zz-wrongsite.io"))
    db_session.flush()
    p = F.plan(db_session, c, new_domain="zz-rightsite.io")
    assert any("zz-wrongsite.io stays as its own company, “Zzwrongsite”" in line for line in p.describe())
    F.apply(db_session, p)
    left = db_session.scalars(select(Company).where(Company.domain == "zz-wrongsite.io")).one()
    assert left.name == "Zzwrongsite" and left.status == "candidate"
    assert left.domain_source == "seed"                 # still "from your seed list": protected from clean-ups
    assert {s.scout for s in db_session.scalars(select(CompanySighting).where(
        CompanySighting.company_id == left.id))} == {"seeds"}
    assert {s.scout for s in db_session.scalars(select(CompanySighting).where(
        CompanySighting.company_id == c.id))} == {"hn"}


def test_renaming_a_company_still_without_a_website_sends_it_back_to_the_website_finder(db_session):
    c = _company(db_session, 0, "Acme Pvt Ltd", None, "unresolved", scout="adzuna")
    c.resolution_attempts = 4
    F.apply(db_session, F.plan(db_session, c, new_name="Zzacme Analytics"))
    db_session.refresh(c)
    assert c.status == "unresolved" and c.resolution_attempts == 0 and c.name == "Zzacme Analytics"


def test_companies_glued_onto_a_profile_site_are_split_back_out(db_session, monkeypatch):
    from app.core import hosts
    from app.modules.m1_companies import profile_repair as R

    sites = hosts.PROFILE_SITES | {"zzprofilesite.io"}                 # a made-up profile site
    monkeypatch.setattr(hosts, "PROFILE_SITES", sites)
    monkeypatch.setattr(R, "PROFILE_SITES", sites)
    # The record was created by the first glued company, so it carries its name; the site's own company is there too.
    site = _company(db_session, 0, "Zzinnerscore", "zzprofilesite.io", "watching", feed="aggregator", scout="adzuna")
    db_session.add_all([
        CompanySighting(company_id=site.id, scout="adzuna", external_id="zz-glue-1", name_seen="Zztoppers Edge",
                        data={"title": "Analyst at Toppers"}),
        CompanySighting(company_id=site.id, scout="packs", external_id="zz-own-1", name_seen="Zzprofilesite"),
    ])
    db_session.flush()
    plan = next(p for p in R.plan(db_session) if p.company_id == site.id)
    assert sorted(plan.out) == ["Zzinnerscore", "Zztoppers Edge"] and plan.own_name == "Zzprofilesite"
    R.apply(db_session, [plan])
    db_session.refresh(site)
    assert site.name == "Zzprofilesite" and site.status == "candidate"           # starts over under its own name
    assert not db_session.scalars(select(CompanyFeed).where(CompanyFeed.company_id == site.id)).all()
    split = {c.name: c for c in db_session.scalars(select(Company).where(
        Company.name.in_(["Zzinnerscore", "Zztoppers Edge"]))).all()}
    assert set(split) == {"Zzinnerscore", "Zztoppers Edge"}
    assert all(c.domain is None and c.status == "unresolved" for c in split.values())
    assert split["Zztoppers Edge"].source_data == {"adzuna": {"title": "Analyst at Toppers"}}   # only its own listing


def test_the_profile_site_name_rule_needs_the_whole_name():
    from app.core.hosts import is_profile_site_for_other

    assert not is_profile_site_for_other("zoominfo.com", "ZoomInfo Technologies")
    assert is_profile_site_for_other("apollo.io", "Apollo Hospitals")           # not apollo.io's owner
    assert not is_profile_site_for_other("dnb.com", "Dun & Bradstreet")          # a known owner


def test_a_kept_company_without_job_reading_is_counted(db_session):
    _company(db_session, 0, "Zzfeedless", "zzfeedless.io", "watching")             # kept, no feed yet
    _company(db_session, 1, "Zzwithfeed", "zzwithfeed.io", "watching", feed="greenhouse")
    r = _row(db_session)
    assert (r.kept, r.read_directly, r.not_set_up) == (2, 1, 1)
