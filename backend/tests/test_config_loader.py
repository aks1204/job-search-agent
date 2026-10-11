from datetime import time

import pytest

from app.config_loader import ConfigError, load_config


def _write(dir_, name, body):
    (dir_ / name).write_text(body, encoding="utf-8")


def test_loads_files_and_skips_examples(tmp_path):
    _write(tmp_path, "app.yaml", 'timezone: "Asia/Kolkata"\nquiet_hours: {start: "22:00", end: "08:00"}\n')
    _write(tmp_path, "notes.yaml", "anything: [goes]\n")  # files without a schema load as raw data
    _write(tmp_path, "owner_stub.example.yaml", "name: x\n")

    cfg = load_config(tmp_path)

    assert set(cfg.files) == {"app", "notes"}
    assert cfg.app.quiet_hours.start == time(22, 0)
    assert cfg.file_version("notes") is not None
    assert cfg.file_version("missing") is None


def test_comment_only_edit_keeps_version(tmp_path):
    _write(tmp_path, "rules.yaml", "a: 1\n")
    before = load_config(tmp_path).file_version("rules")
    _write(tmp_path, "rules.yaml", "# a comment\na: 1\n")
    assert load_config(tmp_path).file_version("rules") == before


def test_content_edit_changes_version(tmp_path):
    _write(tmp_path, "rules.yaml", "a: 1\n")
    first = load_config(tmp_path)
    _write(tmp_path, "rules.yaml", "a: 2\n")
    second = load_config(tmp_path)
    assert first.file_version("rules") != second.file_version("rules")
    assert first.version != second.version


def test_defaults_when_app_yaml_absent(tmp_path):
    cfg = load_config(tmp_path)
    assert cfg.app.timezone == "Asia/Kolkata"


def test_invalid_yaml_names_the_file(tmp_path):
    _write(tmp_path, "broken.yaml", "a: [unclosed\n")
    with pytest.raises(ConfigError, match="broken.yaml"):
        load_config(tmp_path)


def test_unknown_timezone_rejected(tmp_path):
    _write(tmp_path, "app.yaml", 'timezone: "Mars/Olympus"\n')
    with pytest.raises(ConfigError, match="timezone"):
        load_config(tmp_path)


def test_unknown_app_key_rejected(tmp_path):
    _write(tmp_path, "app.yaml", "heartbeat_interval: 60\n")  # typo of heartbeat_interval_seconds
    with pytest.raises(ConfigError):
        load_config(tmp_path)


OWNER = """
name: "T"
career_field: "analytics"
current_role: {title: "Analyst", company: "Co", started: "2024-03"}
employment_history: [{company: "Co", title: "Analyst", start: "2024-03"}]
"""


def test_owner_stub_optional_and_validated(tmp_path):
    assert load_config(tmp_path).owner is None
    _write(tmp_path, "owner_stub.yaml", OWNER)
    owner = load_config(tmp_path).owner
    assert owner.current_role.started == "2024-03" and owner.target_roles == []


def test_owner_stub_placeholders_detected(tmp_path):
    _write(tmp_path, "owner_stub.yaml", OWNER.replace('company: "Co"', 'company: "Current Company"', 1))
    assert load_config(tmp_path).owner.placeholder_fields() == ["current_role.company"]


def test_owner_stub_bad_date_names_the_file(tmp_path):
    _write(tmp_path, "owner_stub.yaml", OWNER.replace('started: "2024-03"', 'started: "March 2024"'))
    with pytest.raises(ConfigError, match="owner_stub.yaml"):
        load_config(tmp_path)


def test_seeds_and_scouts_parse(tmp_path):
    _write(tmp_path, "seed_companies.yaml", "companies:\n  - {name: Uber, domain: uber.com, type: established}\n")
    _write(tmp_path, "scouts.yaml", 'hn: {enabled: false}\nyc: {min_batch_year: 2023}\n')
    cfg = load_config(tmp_path)
    assert cfg.seeds.companies[0].name == "Uber"
    assert cfg.scouts.hn.enabled is False and cfg.scouts.yc.min_batch_year == 2023
    assert cfg.scouts.adzuna.enabled is True  # untouched sections keep defaults


def test_seed_with_bad_type_rejected(tmp_path):
    _write(tmp_path, "seed_companies.yaml", "companies:\n  - {name: X, domain: x.com, type: mnc}\n")
    with pytest.raises(ConfigError, match="seed_companies.yaml"):
        load_config(tmp_path)


def test_shipped_config_files_are_valid():
    from pathlib import Path

    shipped = Path(__file__).resolve().parents[2] / "config"
    if not shipped.is_dir():  # inside the container config is mounted at /config instead
        shipped = Path("/config")
    cfg = load_config(shipped)
    assert len(cfg.seeds.companies) >= 1


def test_missing_dir(tmp_path):
    with pytest.raises(ConfigError):
        load_config(tmp_path / "nope")
