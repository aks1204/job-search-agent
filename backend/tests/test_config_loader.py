from datetime import time

import pytest

from app.config_loader import ConfigError, load_config


def _write(dir_, name, body):
    (dir_ / name).write_text(body, encoding="utf-8")


def test_loads_files_and_skips_examples(tmp_path):
    _write(tmp_path, "app.yaml", 'timezone: "Asia/Kolkata"\nquiet_hours: {start: "22:00", end: "08:00"}\n')
    _write(tmp_path, "company_rules.yaml", "blocklist: [food delivery]\n")
    _write(tmp_path, "owner_stub.example.yaml", "name: x\n")

    cfg = load_config(tmp_path)

    assert set(cfg.files) == {"app", "company_rules"}
    assert cfg.app.quiet_hours.start == time(22, 0)
    assert cfg.file_version("company_rules") is not None
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


def test_missing_dir(tmp_path):
    with pytest.raises(ConfigError):
        load_config(tmp_path / "nope")
