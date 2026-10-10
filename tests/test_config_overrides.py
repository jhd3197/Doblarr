"""Settings must reach a job without modifying global defaults."""

import pytest

from doblarr.config import Config
from doblarr.errors import InvalidSettings
from doblarr.jobs import JobStore, Worker


def test_mixed_overrides_leave_the_payload_unchanged(tmp_path):
    config = Config.load(tmp_path / "config.yaml")
    overrides = {"dub": {"dry_run": False}, "dub.voice_mode": "preset"}
    effective = config.with_overrides(overrides)
    assert effective["dub"]["dry_run"] is False
    assert effective["dub"]["voice_mode"] == "preset"
    assert overrides == {"dub": {"dry_run": False}, "dub.voice_mode": "preset"}


@pytest.mark.parametrize("overrides", [
    {"dub.voice_mode": "preset", "dub.dry_run": False},
    {"dub": {"voice_mode": "preset", "dry_run": False}},
])
def test_worker_applies_title_settings(tmp_path, monkeypatch, overrides):
    config = Config.load(tmp_path / "config.yaml")
    seen = []

    def run_job(job, effective, **kwargs):
        seen.append((effective["dub"]["voice_mode"], kwargs["dry_run"]))
        effective["general"]["target_languages"].append("fr")

    monkeypatch.setattr("doblarr.jobs.run_job", run_job)
    store = JobStore(tmp_path / "jobs.db")
    try:
        job = store.add(title="Film", source="manual", source_lang="ko",
                        target_lang="en", overrides=overrides)
        Worker(store, config)._process(job)
        assert seen == [("preset", False)]
        assert store.get(job.id).status == "done"
        assert config["dub"]["voice_mode"] == "clone"
        assert config["dub"]["dry_run"] is True
        assert config["general"]["target_languages"] == ["en", "es"]
    finally:
        store.close()


def test_a_job_cannot_redirect_files_or_servers(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    for overrides in ({"paths.output_dir": "/anywhere"},
                      {"connect": {"sonarr_url": "http://elsewhere"}},
                      {"translate.endpoint": "http://elsewhere"}):
        with pytest.raises(InvalidSettings):
            store.add(title="Harbor Lights", source="t", source_lang="ja", target_lang="es",
                      overrides=overrides)
    job = store.add(title="Harbor Lights", source="t", source_lang="ja", target_lang="es",
                    overrides={"dub.voice_mode": "preset", "translate": {"model": "m"}})
    assert job.overrides == {"dub.voice_mode": "preset", "translate": {"model": "m"}}
