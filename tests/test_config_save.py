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


def test_moving_a_service_url_drops_its_saved_key(tmp_path):
    from doblarr.config import SECRET_SENTINEL

    cfg = Config.load(tmp_path / "config.yaml")
    cfg.apply_and_save({"connect": {"sonarr_url": "http://s:8989", "sonarr_api_key": "KEY1",
                                    "plex_url": "http://p:32400", "plex_token": "TOKEN"}})
    cfg.apply_and_save({"connect": {"sonarr_url": "http://elsewhere:8989",
                                    "sonarr_api_key": SECRET_SENTINEL,
                                    "plex_url": "http://p:32400", "plex_token": SECRET_SENTINEL}})
    assert cfg["connect"]["sonarr_api_key"] == ""
    assert cfg["connect"]["plex_token"] == "TOKEN"          # its URL did not change


def test_a_new_key_sent_with_a_new_url_is_kept(tmp_path):
    cfg = Config.load(tmp_path / "config.yaml")
    cfg.apply_and_save({"connect": {"radarr_url": "http://r:7878", "radarr_api_key": "OLD"}})
    cfg.apply_and_save({"connect": {"radarr_url": "http://new:7878", "radarr_api_key": "NEW"}})
    assert cfg["connect"]["radarr_api_key"] == "NEW"


@pytest.mark.parametrize("key,value", [
    ("translate", {"provider": "nope"}), ("dub", {"voice_mode": "robot"}),
    ("quality", {"asr": "sometimes"}), ("dub", {"output_codec": "mp3"})])
def test_a_closed_choice_refuses_values_outside_it(tmp_path, key, value):
    cfg = Config.load(tmp_path / "config.yaml")
    with pytest.raises(InvalidSettings, match=f"{key}.{next(iter(value))}"):
        cfg.apply_and_save({key: value})
