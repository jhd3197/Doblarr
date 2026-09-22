
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


def test_cue_provenance_is_recorded_without_changing_existing_version_ids(tmp_path):
    from doblarr.artifacts import digest
    from doblarr.cues import RAW, Artifact, Selection, Take
    from doblarr.versions import file_hash as hash_file

    job, config, _ = setup_job(tmp_path)
    seg = job.segments[0]
    seg.audio.takes.append(Take(take_id="t1", fingerprint="gen-1", engine="tone",
                                raw=Artifact(role=RAW, path=str(tmp_path / "raw.wav"),
                                             fingerprint="gen-1")))
    seg.audio.selection = Selection(take_id="t1")
    manifest = preserve_version(job, config)

    # The identity digest keeps its pre-Plan-01 shape, so versions already on
    # disk are not reinterpreted or forked by the new provenance.
    identity = {k: manifest[k] for k in
                ("schema_version", "translation_id", "output_sha256", "source_sha256",
                 "settings", "voices", "kind", "cast")}
    assert manifest["version_id"] == digest(identity)
    assert "cues" not in identity and "cue_schema" not in identity

    assert manifest["cue_schema"] == 1
    cue = manifest["cues"][0]
    assert cue["index"] == 0 and cue["cue_id"] == seg.cue_id
    assert cue["audio"]["selection"]["take_id"] == "t1"
    # Manifests travel: they carry roles and fingerprints, never local paths.
    assert "path" not in cue["audio"]["takes"][0]["raw"]
    assert cue["audio"]["takes"][0]["raw"]["fingerprint"] == "gen-1"
    assert hash_file(job.output_file) == manifest["output_sha256"]
