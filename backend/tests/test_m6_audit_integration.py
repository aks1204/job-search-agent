"""Part 6e against the real database: the Audit log reads as plain sentences, filters, offers a correction on
each judgment call (and keeps it in Memory), never shows your pay, and condenses entries over 12 months old.
Every entry here carries the word ZZ6E so the tests find their own among your real ones."""

import re
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.core import audit
from app.models import AuditLog, MemoryEntry
from app.modules.maintenance.audit_compact import compact
from tests.test_m6_review_integration import _emails, campaign, sending_off  # noqa: F401  (same fixtures)
from tests.test_m6_web_integration import NOW, client, roles  # noqa: F401

pytestmark = pytest.mark.integration


def token_of(client) -> str:
    return re.search(r'name="token" value="([^"]+)"', client.get("/audit").text).group(1)


def entry_of(page: str, entry: AuditLog) -> str:
    start = page.index(f'id="a{entry.id}"')
    end = page.find('<div class="card s-none audit"', start + 1)
    return page[start:end if end > 0 else None]


def test_an_entry_says_what_why_and_what_it_touched(client, db_session, roles):
    company, *_ = roles
    a = audit.record(db_session, "m1", "company_verdict", f"Kept {company.name}: ZZ6E builds analytics tools",
                     subject_type="company", subject_id=str(company.id), is_judgment=True, rules_version="r7",
                     detail={"checks": [{"name": "size", "outcome": "pass"}], "profile": "startup", "cost_usd": 0.012})
    db_session.flush()
    card = entry_of(client.get("/audit?when=all&q=ZZ6E").text, a)
    assert "Judged a company" in card and "Companies" in card and "judgment call" in card
    assert "It passed every check in your company rules (version r7)" in card
    assert f'href="/companies/{company.id}"' in card and "Web Co" in card
    assert "Reject it instead" in card and f'action="/do/verdict/{company.id}"' in card
    assert "More detail" in card and "AI cost (USD)" in card and "&#34;checks&#34;" not in card   # no raw JSON


def test_filters_by_area_judgment_calls_and_search(client, db_session, roles):
    company, *_ = roles
    j = audit.record(db_session, "m1", "domain_resolved", "Identified ZZ6E Co as zz6e.io (from web search — inferred)",
                     subject_type="company", subject_id=str(company.id), is_judgment=True, detail={"domain": "zz6e.io"})
    o = audit.record(db_session, "m3", "campaign_armed", "ZZ6E outreach began")
    db_session.flush()
    assert f'id="a{o.id}"' in (p := client.get("/audit?area=outreach&when=all&q=ZZ6E").text) and f'id="a{j.id}"' not in p
    p = client.get("/audit?when=all&q=ZZ6E&judgments=1").text
    assert f'id="a{j.id}"' in p and f'id="a{o.id}"' not in p
    assert f'href="/companies/{company.id}#calls"' in entry_of(p, j)                     # wrong website → its page
    assert "Nothing matches" in client.get("/audit?when=all&q=ZZ6E-nothing-like-this").text


def test_pay_never_shows_in_the_log(client, db_session):
    a = audit.record(db_session, "m5", "profile_edited", "You edited your profile ZZ6E: pay",
                     detail={"fields": ["current pay"], "current_comp": "45 LPA", "note": "about 45% of your current pay"})
    db_session.flush()
    card = entry_of(client.get("/audit?when=all&q=ZZ6E").text, a)
    assert "45 LPA" not in card and "45%" not in card and "well below your current pay" in card


def test_re_sorting_a_reply_asks_first_then_acts_and_is_kept_in_memory(client, db_session, campaign):
    es = _emails(db_session, campaign)
    first = es[0]
    first.status, first.gmail_thread_id, first.reply_category, first.reply_from = "sent", "t6e", "ooo", "Asha Rao"
    campaign.status = "running"
    entry = audit.record(db_session, "m3", "reply_ooo", "Web Co: reply from Asha Rao sorted as ooo (80%): ZZ6E away",
                         subject_type="campaign", subject_id=str(campaign.id), is_judgment=True,
                         detail={"category": "ooo", "confidence": 0.8, "thread_id": "t6e"})
    db_session.flush()
    card = entry_of(client.get("/audit?when=all&q=ZZ6E").text, entry)
    assert "Read a reply" in card and "out of office, 80% sure" in card and f"/do/reply-sort/{entry.id}" in card
    assert '<option value="ooo">' not in card                                        # not offered what it already is

    form = {"token": token_of(client), "next": "/audit", "category": "interested"}
    asked = client.post(f"/do/reply-sort/{entry.id}", data=form).text
    assert "Re-sort this reply?" in asked and "Out of office → Interested" in asked
    assert entry.overridden_at is None                                                # nothing done before Confirm

    r = client.post(f"/do/reply-sort/{entry.id}", data={**form, "confirmed": "1"}, follow_redirects=False)
    assert r.status_code == 303
    es = _emails(db_session, campaign)
    assert es[0].reply_category == "interested" and campaign.outcome == "interested"
    assert all(e.status == "cancelled" for e in es[1:])                               # a person answered: no more
    assert entry.overridden_at is not None
    memo = db_session.get(MemoryEntry, entry.override_memory_id)
    assert memo.kind == "precedent" and "really Interested" in memo.meaning
    assert "corrected by you" in entry_of(client.get("/audit?when=all&q=ZZ6E").text, entry)
    again = client.post(f"/do/reply-sort/{entry.id}", data={**form, "confirmed": "1"},
                        follow_redirects=True).text
    assert "already corrected" in again


def test_entries_over_12_months_old_are_condensed_and_corrections_kept(db_session):
    long_ago = NOW - timedelta(days=900)                       # older than anything in your real database
    for i in range(3):
        db_session.add(AuditLog(module="m1", action="zz6e_test", summary=f"ZZ6E old {i}", created_at=long_ago))
    kept = AuditLog(module="m1", action="zz6e_test", summary="ZZ6E corrected", created_at=long_ago,
                    is_judgment=True, overridden_at=long_ago)
    db_session.add(kept)
    db_session.flush()
    result = compact(db_session, now=NOW - timedelta(days=500))
    assert result["condensed"] >= 3
    mine = db_session.scalars(select(AuditLog).where(AuditLog.summary.like("%Zz6e test%")
                                                     | AuditLog.summary.like("ZZ6E%"))).all()
    lines = [a for a in mine if a.action == "monthly_summary"]
    assert len(lines) == 1 and lines[0].detail["count"] == 3 and lines[0].summary.endswith("3 × Zz6e test")
    assert [a.summary for a in mine if a.action == "zz6e_test"] == ["ZZ6E corrected"]
    assert compact(db_session, now=NOW - timedelta(days=500))["condensed"] == 0      # twice: nothing more


def test_the_condensing_runs_monthly_and_from_maintenance():
    from app.celery_app import build_beat_schedule
    from app.config_loader import get_config
    from app.web import runs

    assert build_beat_schedule(get_config())["maintenance-audit-compact"]["task"] == "maintenance.audit_compact"
    assert "audit" in runs.MAINTENANCE_JOBS
