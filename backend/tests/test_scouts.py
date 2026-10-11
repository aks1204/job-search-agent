"""Scout parsing against canned API responses — no network, no database."""

import httpx
import pytest
import respx

from app.config_loader import load_config
from app.core.http import PoliteClient
from app.modules.m1_companies.scouts import SCOUTS, ScoutContext
from app.modules.m1_companies.scouts.hn import API as HN_API
from app.modules.m1_companies.scouts.yc import YC_URL, batch_year
from app.settings import get_settings

OWNER = """
name: "Test Owner"
career_field: "analytics"
current_role: {title: "Senior Associate", company: "Current Co", started: "2024-03"}
employment_history:
  - {company: "Current Co", title: "Senior Associate", start: "2024-03", end: null}
target_roles: ["Senior Data Analyst", "Product Analyst"]
"""

SEEDS = """
companies:
  - {name: "Uber", domain: "https://www.uber.com", type: established}
  - {name: "Tiny Startup", domain: "tiny.ai", type: startup, notes: "friend works there"}
"""


@pytest.fixture
def cfg(tmp_path):
    (tmp_path / "owner_stub.yaml").write_text(OWNER, encoding="utf-8")
    (tmp_path / "seed_companies.yaml").write_text(SEEDS, encoding="utf-8")
    return load_config(tmp_path)


@pytest.fixture
def ctx(cfg):
    with PoliteClient(default_interval=0, use_redis=False) as http:
        yield ScoutContext(http=http, config=cfg, state={})


@pytest.fixture
def keys(monkeypatch):
    monkeypatch.setenv("ADZUNA_APP_ID", "test-app-id")
    monkeypatch.setenv("ADZUNA_APP_KEY", "test-app-key")
    monkeypatch.setenv("JOOBLE_API_KEY", "test-jooble-key")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _run(name, cfg, ctx):
    return list(SCOUTS[name](cfg).discover(ctx))


def test_seeds(cfg, ctx):
    out = _run("seeds", cfg, ctx)
    assert [(c.name, c.domain, c.type_hint) for c in out] == [
        ("Uber", "uber.com", "established"),
        ("Tiny Startup", "tiny.ai", "startup"),
    ]
    assert out[1].data == {"trusted": False, "notes": "friend works there"}


@pytest.mark.parametrize("batch, year", [("Winter 2024", 2024), ("S23", 2023), ("X25", 2025), ("", None), (None, None)])
def test_batch_year(batch, year):
    assert batch_year(batch) == year


@respx.mock
def test_yc_filters(cfg, ctx):
    base = {"status": "Active", "batch": "Winter 2024", "isHiring": True, "regions": ["India"]}
    respx.get(YC_URL).respond(json=[
        {**base, "id": 1, "name": "Keeper", "website": "https://keeper.ai", "one_liner": "AI for X"},
        {**base, "id": 2, "name": "Dead Co", "status": "Inactive"},
        {**base, "id": 3, "name": "Old Co", "batch": "Summer 2019"},
        {**base, "id": 4, "name": "Not Hiring", "isHiring": False},
        {**base, "id": 5, "name": "US Only", "regions": ["United States of America"], "all_locations": "San Francisco, CA, USA"},
        {**base, "id": 6, "name": "Remote Co", "regions": ["Remote", "America / Canada"]},
        {**base, "id": 7, "name": "Hoosier Co", "regions": [], "all_locations": "Indianapolis, Indiana, USA"},
    ])
    out = _run("yc", cfg, ctx)
    assert [c.name for c in out] == ["Keeper", "Remote Co"]
    assert out[0].domain == "https://keeper.ai" and out[0].type_hint == "startup"
    assert out[0].data["one_liner"] == "AI for X"
    assert ctx.state["last_total"] == 7


@respx.mock
def test_adzuna(cfg, ctx, keys):
    route = respx.get(url__regex=r"https://api\.adzuna\.com/v1/api/jobs/in/search/\d+").respond(json={"results": [
        {"id": "11", "company": {"display_name": "Acme Corp "}, "title": "Senior Data Analyst",
         "redirect_url": "https://www.adzuna.in/land/ad/11", "location": {"display_name": "Bengaluru"}},
        {"id": "12", "company": {"display_name": "Confidential"}, "title": "Analyst"},
        {"id": "13", "company": {}, "title": "No company"},
    ]})
    scout = SCOUTS["adzuna"](cfg)
    assert scout.not_ready_reason() is None
    out = list(scout.discover(ctx))
    assert route.call_count == 2  # one page per role: fewer results than a full page
    assert {c.name for c in out} == {"Acme Corp"}
    assert route.calls[0].request.url.params["what"] == "Senior Data Analyst"


def test_adzuna_not_ready_without_keys(cfg, monkeypatch):
    monkeypatch.setenv("ADZUNA_APP_ID", "")
    get_settings.cache_clear()
    try:
        assert "ADZUNA" in SCOUTS["adzuna"](cfg).not_ready_reason()
    finally:
        get_settings.cache_clear()


@respx.mock
def test_jooble(cfg, ctx, keys):
    respx.post("https://jooble.org/api/test-jooble-key").respond(json={"jobs": [
        {"id": 900, "company": "Zenith Analytics", "title": "Product Analyst", "link": "https://jooble.org/desc/900"},
    ]})
    out = _run("jooble", cfg, ctx)
    assert [c.name for c in out] == ["Zenith Analytics", "Zenith Analytics"]  # same job for both roles
    assert out[0].external_id == "900"


@respx.mock
def test_remotive_location_filter(cfg, ctx):
    respx.get("https://remotive.com/api/remote-jobs").respond(json={"jobs": [
        {"id": 1, "company_name": "Anywhere Inc", "candidate_required_location": "Worldwide"},
        {"id": 2, "company_name": "Desi Co", "candidate_required_location": "India"},
        {"id": 3, "company_name": "Yanks Only", "candidate_required_location": "USA Only"},
    ]})
    out = _run("remotive", cfg, ctx)
    assert {c.name for c in out} == {"Anywhere Inc", "Desi Co"}


HN_ITEMS = {
    100: {"id": 100, "title": "Ask HN: Who wants to be hired? (October 2026)"},
    99: {"id": 99, "title": "Ask HN: Who is hiring? (October 2026)", "kids": [1, 2, 3]},
    1: {"id": 1, "time": 1, "text": "Quuxly | Senior Data Analyst | Remote (India)<p>Email hiring@quuxly.io"},
    2: {"id": 2, "deleted": True},
    3: {"id": 3, "time": 1, "text": "We are hiring. Long free-text post with no pipes."},
}


@respx.mock
def test_hn_incremental(cfg, ctx):
    respx.get(f"{HN_API}/user/whoishiring.json").respond(json={"submitted": [100, 99]})
    items = respx.get(url__regex=rf"{HN_API}/item/(?P<item_id>\d+)\.json").mock(
        side_effect=lambda request, item_id: httpx.Response(200, json=HN_ITEMS[int(item_id)])
    )

    out = _run("hn", cfg, ctx)
    assert [(c.name, c.domain) for c in out] == [("Quuxly", "quuxly.io")]
    assert [h.email for h in out[0].contacts] == ["hiring@quuxly.io"]
    entry = ctx.state["threads"]["99"]
    assert entry["seen"] == [1, 2, 3] and entry["unparsed"] == [3]

    calls_before = items.call_count
    assert _run("hn", cfg, ctx) == []  # nothing new mid-month
    assert items.call_count - calls_before == 2  # only the two thread lookups, no comments re-fetched
