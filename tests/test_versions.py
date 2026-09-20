
import pytest

from doblarr.config import Config
from doblarr.models import DubJob, Segment, Speaker
from doblarr.versions import file_hash, preserve_version


def setup_job(tmp_path):
    source = tmp_path / "source.mkv"
    source.write_bytes(b"source video")
    output = tmp_path / "out" / "dub.mkv"
    output.parent.mkdir()
    output.write_bytes(b"first rendered dub")
    job = DubJob(source, "en", "es", output_file=output)
    job.segments = [Segment(0, 1, 2, "Hello", text_translated="Hola", speaker="GINKO")]
    job.speakers = {"GINKO": Speaker("GINKO", voicebox_profile_id="voice-1")}
    config = Config.load(tmp_path / "config.yaml").with_overrides({
        "dub.version_name": "LATAM quiet", "translate.locale": "es-419",
        "connect.plex_token": "private-token", "translate.endpoint": "private-endpoint"})
    return job, config, output


def test_script_and_dub_versions_are_independent_and_old_media_survives(tmp_path):
    job, config, working_output = setup_job(tmp_path)
    first = preserve_version(job, config)
    saved = job.output_file
    working_output.write_bytes(b"different voice take")
    job.output_file = working_output
    job.speakers["GINKO"].voicebox_profile_id = "voice-2"
    second = preserve_version(job, config)
    assert second["translation_id"] == first["translation_id"]
    assert second["version_id"] != first["version_id"]
    assert saved.read_bytes() == b"first rendered dub"
    job.output_file = working_output
    job.segments[0].text_translated = "Buenas"
    third = preserve_version(job, config)
    assert third["translation_id"] != first["translation_id"]
    assert len({first["version_id"], second["version_id"], third["version_id"]}) == 3


def test_identical_version_reused_and_manifest_has_no_connection_secrets(tmp_path):
    job, config, working_output = setup_job(tmp_path)
    first = preserve_version(job, config)
    job.output_file = working_output
    second = preserve_version(job, config.with_overrides({"dub.version_name": "Renamed"}))
    assert first == second
    text = job.version_file.read_text(encoding="utf-8")
    assert "private-token" not in text and "private-endpoint" not in text
    assert file_hash(job.output_file) == first["output_sha256"]


def test_modified_saved_media_is_never_silently_overwritten(tmp_path):
    job, config, working_output = setup_job(tmp_path)
    preserve_version(job, config)
    job.output_file.write_bytes(b"changed outside Doblarr")
    job.output_file = working_output
    with pytest.raises(ValueError, match="refusing to overwrite"):
        preserve_version(job, config)
