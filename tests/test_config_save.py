"""Saving settings: atomic writes and refusing values the schema rejects."""

import pytest
import yaml
from fastapi.testclient import TestClient

from doblarr import fileio
from doblarr.config import Config
from doblarr.errors import InvalidSettings
from doblarr.server import create_app


def test_invalid_value_is_refused_and_nothing_is_saved(tmp_path):
    path = tmp_path / "config.yaml"
    cfg = Config.load(path)
    cfg.apply_and_save({"dub": {"dry_run": False}})
    with pytest.raises(InvalidSettings, match="dub.dry_run"):
        cfg.apply_and_save({"dub": {"dry_run": "sometimes"}})
    assert cfg["dub"]["dry_run"] is False
    assert yaml.safe_load(path.read_text(encoding="utf-8")) == {"dub": {"dry_run": False}}


def test_a_problem_already_in_the_file_does_not_block_other_saves(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"dub": {"dry_run": "sometimes"}}), encoding="utf-8")
    cfg = Config.load(path)
    cfg.apply_and_save({"plex": {"auto_refresh": False}})
    assert cfg["plex"]["auto_refresh"] is False


def test_a_failed_write_keeps_the_previous_file(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    cfg = Config.load(path)
    cfg.apply_and_save({"dub": {"dry_run": False}})
    before = path.read_text(encoding="utf-8")

    def full_disk(src, dst):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(fileio.os, "replace", full_disk)
    with pytest.raises(OSError):
        cfg.apply_and_save({"plex": {"auto_refresh": False}})
    assert path.read_text(encoding="utf-8") == before
    assert list(tmp_path.glob("*.partial")) == []


def test_settings_route_answers_422_for_an_invalid_value(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"paths": {"work_dir": str(tmp_path / "work")}}),
                    encoding="utf-8")
    client = TestClient(create_app(Config.load(path)))
    r = client.post("/api/config", json={"dub": {"dry_run": "sometimes"}})
    assert r.status_code == 422
    assert "dub.dry_run" in r.json()["error"]
