"""Seed packs and the 'companies like these' scout. No database; HTTP mocked; fake AI."""

import pytest
import respx

from app.config_loader import ConfigError, load_config
from app.core.http import PoliteClient
from app.core.llm import LLMResult
from app.modules.m1_companies.scouts import SCOUTS, ScoutContext
from app.modules.m1_companies.scouts.peers import Peer, PeerSuggestions

OWNER = """
name: "T"
career_field: "analytics"
location: "Bengaluru, India"
current_role: {title: "Analyst", company: "Co", started: "2024-03"}
employment_history: [{company: "Co", title: "Analyst", start: "2024-03"}]
"""


def _cfg(tmp_path, scouts_yaml="", pack=None):
    (tmp_path / "owner_stub.yaml").write_text(OWNER, encoding="utf-8")
    if scouts_yaml:
        (tmp_path / "scouts.yaml").write_text(scouts_yaml, encoding="utf-8")
    packs = tmp_path / "seed_packs"
    packs.mkdir()
    (packs / "test_pack.yaml").write_text(pack or (
        'name: "Test pack"\ncompanies:\n'
        '  - {name: "Jio Platforms", domain: "https://www.jio.com/", type: established}\n'
        '  - {name: "Razorpay", domain: "razorpay.com", type: startup}\n'
    ), encoding="utf-8")
    return load_config(tmp_path)


def test_packs_scout_emits_enabled_pack_companies(tmp_path):
    cfg = _cfg(tmp_path, "packs: {enabled_packs: [test_pack]}\n")
    scout = SCOUTS["packs"](cfg)
    assert scout.not_ready_reason() is None
    with PoliteClient(default_interval=0, use_redis=False) as http:
        out = list(scout.discover(ScoutContext(http=http, config=cfg, state={})))
    assert [(c.name, c.domain, c.type_hint, c.data["pack"]) for c in out] == [
        ("Jio Platforms", "jio.com", "established", "test_pack"),
        ("Razorpay", "razorpay.com", "startup", "test_pack"),
    ]


def test_unknown_pack_name_is_reported_not_crashed(tmp_path):
    cfg = _cfg(tmp_path, "packs: {enabled_packs: [nope]}\n")
    assert "nope" in SCOUTS["packs"](cfg).not_ready_reason()


def test_bad_pack_file_names_the_file(tmp_path):
    with pytest.raises(ConfigError, match="seed_packs/test_pack.yaml"):
        _cfg(tmp_path, pack="companies:\n  - {name: X, domain: x.com, type: giant}\n")


def test_shipped_packs_are_valid():
    from pathlib import Path

    shipped = Path(__file__).resolve().parents[2] / "config"
    cfg = load_config(shipped if shipped.is_dir() else Path("/config"))
    assert {"gcc_india", "india_large", "india_unicorns"} <= set(cfg.seed_packs)
    assert any(c.domain == "jio.com" for c in cfg.seed_packs["india_large"].companies)


class FakeLLM:
    def __init__(self, peers):
        self.peers = peers

    def structured(self, tier, *, system, user, schema, purpose, session=None, subject=None):
        return LLMResult(parsed=PeerSuggestions(peers=self.peers), model="fake", cost_usd=0.0,
                         input_tokens=0, output_tokens=0)


class FakeSession:
    """Just enough of a session for the peers scout: one kept company, an empty fact card."""

    def __init__(self, company):
        self.company = company

    def scalars(self, stmt):
        rows = [self.company]

        class R:
            def all(self_inner):
                return rows

            def __iter__(self_inner):
                return iter([])  # fact_card(): no facts

        return R()


def test_peers_scout_only_emits_suggestions_whose_site_confirms_them(tmp_path):
    from types import SimpleNamespace

    cfg = _cfg(tmp_path)
    uber = SimpleNamespace(id=7, name="Uber", domain="uber.com")
    llm = FakeLLM([
        Peer(name="Rapido", domain="rapido.bike", type="startup", why="ride-hailing in India"),
        Peer(name="Imaginary Rides", domain="imaginary-rides.example", type="startup", why="made up"),
        Peer(name="Ola", domain="olacabs.com", type="startup", why="ride-hailing"),
        Peer(name="Uber Eats", domain="uber.com", type="established", why="same company"),
    ])
    with PoliteClient(default_interval=0, use_redis=False) as http, respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=r"https://[^/]+/robots\.txt").respond(404)
        mock.get(url__regex=r"https://(www\.)?rapido\.bike/?$").respond(
            200, headers={"content-type": "text/html"}, html="<title>Rapido</title>" + "Bike taxi app Rapido. " * 20)
        mock.get(url__regex=r"https://(www\.)?olacabs\.com/?$").respond(
            200, headers={"content-type": "text/html"}, html="<title>Book a cab</title>" + "Cabs near you. " * 20)
        mock.get(url__regex=r"https://(www\.)?imaginary-rides\.example.*").respond(404)
        ctx = ScoutContext(http=http, config=cfg, state={}, llm=llm, session=FakeSession(uber))
        out = list(SCOUTS["peers"](cfg).discover(ctx))

    # Rapido: live site names it. Ola: site doesn't say "Ola" in its text. Imaginary: no site. Uber: itself.
    assert [(c.name, c.domain, c.data["suggested_from"]) for c in out] == [("Rapido", "rapido.bike", "uber.com")]
    assert ctx.state["expanded"] == [7]


def test_peers_scout_skips_without_ai(tmp_path):
    cfg = _cfg(tmp_path)
    with PoliteClient(default_interval=0, use_redis=False) as http:
        ctx = ScoutContext(http=http, config=cfg, state={}, llm=None, session=None)
        assert list(SCOUTS["peers"](cfg).discover(ctx)) == []
    assert "skipped" in ctx.state["last_note"]
