"""Phase E — treatments, export validation and the review surface together.

These run the real pipeline over the offline tone fixtures with real FFmpeg, so
the request counts, the artifact roles, the export findings and the API payloads
are measured rather than inferred. Tones establish numbers and structure. They
say nothing about whether a room sounds like a room.
"""

import shutil

import pytest

from doblarr import benchmarks, delivery, treatments
from doblarr.config import Config
from doblarr.cues import CUE_SCHEMA_VERSION, EDGED, TREATED
from doblarr.errors import JobCancelled
from doblarr.models import DubJob
from doblarr.pipeline import run_job
from doblarr.services import Services

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")

ROOM = {"treatments.mode": "on", "treatments.default": "room"}
PHONE = {"treatments.mode": "on", "treatments.default": "phone"}


def build(root, overrides=None, engine=None, media_root=None,
          scene=benchmarks.TREATMENT_SCENE):
    media, subtitles = benchmarks.write_media(media_root or root / "media", scene)
    config = Config.load(root / "config.yaml").with_overrides({
        "paths.work_dir": str(root / "work"),
        "paths.output_dir": str(root / "output"),
        "dub.dry_run": False,
        "dub.voice_mode": "preset",
        "dub.preset_voices": ["tone-voice"],
        "dub.preserve_versions": False,
        "transcribe.diarize": False,
        "translate.provider": "passthrough",
        "quality.asr": "off",
        **(overrides or {}),
    })
    tone = engine or benchmarks.ToneEngine(root, scene)
    services = Services(config)
    services._cache["voicebox"] = tone
    job = DubJob(input_file=media, source_lang="ja", target_lang="es",
                 subtitle_file=subtitles)
    job.script_is_target = True
    return job, config, services, tone


def run(root, overrides=None, engine=None, media_root=None,
        scene=benchmarks.TREATMENT_SCENE, **kwargs):
    job, config, services, tone = build(root, overrides, engine, media_root, scene)
    run_job(job, config, services=services, **kwargs)
    return job, config, tone


def spoken(job):
    return [s for s in job.segments if s.audio.raw() is not None]


# -- the treated role -------------------------------------------------------

def test_treatments_are_off_by_default_and_produce_no_role(tmp_path):
    job, _config, _tone = run(tmp_path / "off")
    assert all(s.audio.render(TREATED) is None for s in job.segments)
    assert {s.treatment.outcome for s in job.segments} == {"bypassed"}
    assert job.metrics["treatments"]["mode"] == "off"


def test_turning_a_treatment_on_produces_the_treated_role_last(tmp_path):
    job, _config, _tone = run(tmp_path / "on", ROOM)
    treated = [s for s in job.segments if s.audio.render(TREATED)]
    assert treated
    for seg in treated:
        assert seg.audio.current().role == TREATED
        assert seg.treatment.applied
        assert seg.treatment.preset == "room"
        # produced from the finished dry line, never from its own output
        assert seg.audio.render(TREATED).derived_from != TREATED


def test_a_treatment_change_generates_no_speech(tmp_path):
    """Space is processing. It re-renders audio that already exists."""
    root = tmp_path / "cost"
    media = root / "media"
    _dry, _config, plain = run(root / "a", media_root=media)
    _wet, _config, roomed = run(root / "b", ROOM, media_root=media)
    assert len(roomed.requests) == len(plain.requests)
    _phone, _config, called = run(root / "c", PHONE, media_root=media)
    assert len(called.requests) == len(plain.requests)


def test_switching_a_preset_reprocesses_the_dry_line(tmp_path):
    root = tmp_path / "switch"
    media = root / "media"
    first, _c, _t = run(root, ROOM, media_root=media)
    room_prints = {s.cue_id: s.audio.render(TREATED).fingerprint
                   for s in first.segments if s.audio.render(TREATED)}
    second, _c, _t = run(root, PHONE, media_root=media)
    phone_prints = {s.cue_id: s.audio.render(TREATED).fingerprint
                    for s in second.segments if s.audio.render(TREATED)}
    assert room_prints and phone_prints
    assert set(room_prints) == set(phone_prints)
    assert all(room_prints[cue] != phone_prints[cue] for cue in room_prints)
    for seg in second.segments:
        if seg.audio.render(TREATED):
            assert seg.audio.render(TREATED).derived_from != TREATED


def test_turning_treatments_back_off_restores_the_dry_render(tmp_path):
    root = tmp_path / "back"
    media = root / "media"
    wet, _c, _t = run(root, ROOM, media_root=media)
    assert any(s.audio.render(TREATED) for s in wet.segments)
    dry, _c, _t = run(root, media_root=media)
    assert all(s.audio.render(TREATED) is None for s in dry.segments)
    for seg in dry.segments:
        current = seg.audio.current()
        if current is not None:
            assert current.role != TREATED


def test_an_edge_change_invalidates_the_treatment_downstream_of_it(tmp_path):
    root = tmp_path / "edges"
    media = root / "media"
    first, _c, _t = run(root, ROOM, media_root=media)
    before = {s.cue_id: s.audio.render(TREATED).fingerprint
              for s in first.segments if s.audio.render(TREATED)}
    second, _c, _t = run(root, {**ROOM, "boundaries.edge_fade_ms": 8},
                         media_root=media)
    faded = [s for s in second.segments if s.audio.render(EDGED)]
    assert faded, "the fixture should produce at least one edge fade"
    for seg in faded:
        treated = seg.audio.render(TREATED)
        assert treated is not None
        assert treated.derived_from == EDGED
        assert before.get(seg.cue_id) != treated.fingerprint


def test_a_reverb_tail_is_not_reported_as_an_introduced_collision(tmp_path):
    """The ring-out past a line is not that line still talking."""
    root = tmp_path / "tails"
    media = root / "media"
    dry, _c, _t = run(root / "a", media_root=media)
    wet, _c, _t = run(root / "b", {"treatments.mode": "on",
                                   "treatments.default": "distant"},
                      media_root=media)
    assert any(s.treatment.applied for s in wet.segments)
    assert wet.metrics["conversation"]["collisions"] == \
        dry.metrics["conversation"]["collisions"]


def test_the_schema_records_the_treatment_and_survives_a_resume(tmp_path):
    root = tmp_path / "resume"
    media = root / "media"
    first, _c, _t = run(root, ROOM, media_root=media)
    saved = next((root / "work").rglob("*.script.json"))
    import json
    payload = json.loads(saved.read_text(encoding="utf-8"))
    assert payload["cue_schema"] == CUE_SCHEMA_VERSION
    row = next(r for r in payload["segments"] if r["cue"]["treatment"]["outcome"] == "applied")
    assert row["cue"]["treatment"]["preset"] == "room"
    assert row["cue"]["treatment"]["tail"] == treatments.tail_seconds("room")
    again, _c, _t = run(root, ROOM, media_root=media)
    assert [s.treatment.as_dict() for s in again.segments] == \
        [s.treatment.as_dict() for s in first.segments]


def test_a_per_line_override_beats_the_run_default(tmp_path):
    # One fixture media for both runs: a cue id is scoped to the source
    # document, so two different fixtures would be two different lines.
    media = tmp_path / "media"
    job, _c, _t = run(tmp_path / "lines", ROOM, media_root=media)
    cue = job.segments[0].cue_id
    override, _c, _t = run(tmp_path / "lines2",
                           {**ROOM, "treatments.lines": {cue: {"preset": "phone"}}},
                           media_root=media)
    chosen = next(s for s in override.segments if s.cue_id == cue)
    assert chosen.treatment.preset == "phone"
    assert chosen.treatment.origin == "line"
    others = [s for s in override.segments if s.cue_id != cue and s.treatment.applied]
    assert others and all(s.treatment.preset == "room" for s in others)


def test_a_scene_rule_applies_to_the_lines_inside_it(tmp_path):
    job, _c, _t = run(tmp_path / "scenes", {
        "treatments.mode": "on", "treatments.default": "dry",
        "treatments.scenes": [{"start": 3.5, "end": 8.0, "preset": "phone"}]})
    inside = [s for s in job.segments if 3.5 <= s.start < 8.0]
    outside = [s for s in job.segments if not (3.5 <= s.start < 8.0)]
    assert inside and all(s.treatment.preset == "phone" for s in inside)
    assert all(s.treatment.preset == "dry" for s in outside)
    assert all(s.treatment.origin == "scene" for s in inside)


def test_cancelling_during_treatment_stops_the_run(tmp_path):
    import threading

    cancel = threading.Event()
    seen = []

    def on_stage(name, i, total):
        seen.append(name)
        if name == "treatments":
            cancel.set()

    job, config, services, _tone = build(tmp_path / "cancel", ROOM)
    with pytest.raises(JobCancelled):
        run_job(job, config, services=services, cancel_event=cancel,
                on_stage=on_stage)
    assert "treatments" in seen


def test_a_dry_run_renders_no_treatment_and_no_export_check(tmp_path):
    job, config, services, _tone = build(tmp_path / "dry", {**ROOM, "dub.dry_run": True})
    run_job(job, config, dry_run=True, services=services)
    assert all(s.audio.render(TREATED) is None for s in job.segments)
    assert job.delivery.get("state") in (None, "skipped", "unavailable")


# -- export validation in the pipeline --------------------------------------

def test_the_exported_track_is_checked_and_identified_by_its_tags(tmp_path):
    job, _config, _tone = run(tmp_path / "export")
    report = job.delivery
    assert report["state"] in ("passed", "warned"), report["findings"]
    assert report["stream"]["language"] == "spa"
    assert "expected language and title" in report["identification"]
    assert report["loudness"]["meter"] == delivery.METER
    assert report["decoded_seconds"] is not None
    assert all(row["state"] == "present" for row in report["placement"])


def test_the_mux_records_which_stream_the_dub_is(tmp_path):
    job, _config, _tone = run(tmp_path / "mux")
    recorded = job.metrics["mux"]
    assert recorded["audio_index"] == recorded["original_streams"]["audio"]
    assert recorded["language"] == "spa"
    assert recorded["original_streams"]["video"] >= 1


def test_export_validation_can_be_turned_off_without_blocking_anything(tmp_path):
    job, _config, _tone = run(tmp_path / "off", {"delivery.mode": "off"})
    assert job.delivery["state"] == "skipped"
    assert job.delivery["publishable"] is True


def test_an_enforced_failure_withholds_the_saved_version(tmp_path, monkeypatch):
    """A candidate that failed its checks is kept, but it is not a version."""
    real = delivery.validate

    def failing(job, options=None, cancel=None, work_dir=None):
        report = real(job, options, cancel, work_dir)
        report.update(state="failed", publishable=False, findings=[
            {"code": "duration_mismatch", "severity": "failure",
             "detail": "deliberate", "evidence": {}, "detector": "test"}])
        job.delivery = report
        return report

    monkeypatch.setattr("doblarr.pipeline.delivery.validate", failing)
    job, _config, _tone = run(tmp_path / "enforce",
                              {"delivery.mode": "enforce",
                               "dub.preserve_versions": True})
    assert job.version_id is None            # nothing was published
    assert job.output_file.is_file()         # and the candidate is still there
    assert job.delivery["state"] == "failed"


def test_a_passing_export_still_saves_its_version(tmp_path):
    job, _config, _tone = run(tmp_path / "publish",
                              {"delivery.mode": "enforce",
                               "dub.preserve_versions": True})
    assert job.delivery["publishable"] is True
    assert job.version_id
    assert job.version_file.is_file()


def test_the_version_manifest_carries_the_treatment_and_the_export_report(tmp_path):
    import json

    job, _config, _tone = run(tmp_path / "version",
                              {**ROOM, "dub.preserve_versions": True})
    manifest = json.loads(job.version_file.read_text(encoding="utf-8"))
    assert manifest["settings"]["treatments"]["default"] == "room"
    assert manifest["delivery"]["state"] in ("passed", "warned")
    treated = [row for row in manifest["cues"]
               if row["treatment"]["outcome"] == "applied"]
    assert treated
    assert "path" not in treated[0]["audio"]["renders"][0]


def test_two_presets_are_two_versions(tmp_path):
    """Two renders that differ only by a preset are different dubs."""
    root = tmp_path / "identity"
    media = root / "media"
    first, _c, _t = run(root / "a", {**ROOM, "dub.preserve_versions": True},
                        media_root=media)
    second, _c, _t = run(root / "b", {**PHONE, "dub.preserve_versions": True},
                         media_root=media)
    assert first.version_id != second.version_id


# -- the review surface -----------------------------------------------------

def test_the_review_snapshot_freezes_the_treatment_policy_and_the_profile(tmp_path):
    import json

    job, _config, _tone = run(tmp_path / "review", ROOM)
    snapshot = json.loads(job.review_file.read_text(encoding="utf-8"))
    frozen = snapshot["settings"]
    assert frozen["treatments"]["mode"] == "on"
    assert frozen["treatments"]["default"] == "room"
    assert {p["preset"] for p in frozen["treatments"]["catalogue"]} == \
        set(treatments.TREATMENT_PRESETS)
    assert frozen["delivery"]["meter"] == delivery.METER
    assert snapshot["delivery"]["state"] in ("passed", "warned")
    applied = [r for r in snapshot["segments"]
               if r["cue"]["treatment"]["outcome"] == "applied"]
    assert applied
    assert applied[0]["cue"]["treatment"]["preset"] == "room"
    assert applied[0]["cue"]["treatment"]["tail"] == treatments.tail_seconds("room")


def test_the_metrics_say_what_was_applied_and_what_was_only_asked_for(tmp_path):
    job, _config, _tone = run(tmp_path / "metrics", ROOM)
    found = job.metrics["treatments"]
    assert found["mode"] == "on"
    assert found["applied"] >= 1
    assert found["presets"].get("room") == found["applied"]
    assert found["longest_tail"] == treatments.tail_seconds("room")
    assert found["catalogue"] == treatments.CATALOGUE
