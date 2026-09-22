"""The real-media comparison runner, exercised on the offline fixtures.

The fixtures here are tones, so nothing below establishes that anything sounds
better — that is the whole reason this runner exists and the whole reason it
cannot be the thing that answers the question. What these tests do fix is that
the runner is *fair*: the same window, the same cast, the same takes, zero
generation, and an objective gate that refuses to hand a broken comparison to a
listener.
"""

import json
import shutil

import pytest

from doblarr import benchmarks, comparison
from doblarr.config import Config
from doblarr.cues import TREATED

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")


def completed_run(root, scene=benchmarks.TREATMENT_SCENE):
    """A finished tone render, standing in for a completed real dub.

    The comparison runner only ever reads a script cache, a clips directory and
    the source media, so a fixture render is a faithful stand-in for the shape
    of the input even though its audio is a sine wave.
    """
    media, subtitles = benchmarks.write_media(root / "media", scene)
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
    })
    engine = benchmarks.ToneEngine(root, scene)
    services = benchmarks.Services(config)
    services._cache["voicebox"] = engine
    from doblarr.models import DubJob
    from doblarr.pipeline import run_job

    job = DubJob(input_file=media, source_lang="ja", target_lang="es",
                 subtitle_file=subtitles)
    job.script_is_target = True
    run_job(job, config, services=services)
    script = next((root / "work").rglob("*.script.json"))
    clips = next(p for p in (root / "work").rglob("line_0000.wav")).parent
    return {"media": media, "script": script, "clips": clips, "job": job,
            "vocals": job.vocals, "background": job.background}


# -- reading a completed run -------------------------------------------------

def test_a_missing_script_names_the_file_rather_than_crashing(tmp_path):
    with pytest.raises(comparison.ComparisonError, match="no script cache"):
        comparison.read_script(tmp_path / "nothing.json")


def test_an_empty_script_is_refused(tmp_path):
    path = tmp_path / "s.json"
    path.write_text(json.dumps({"segments": []}), encoding="utf-8")
    with pytest.raises(comparison.ComparisonError, match="no segments"):
        comparison.read_script(path)


@needs_ffmpeg
def test_the_inventory_says_which_lines_still_have_audio(tmp_path):
    run = completed_run(tmp_path)
    rows = comparison.inventory(comparison.read_script(run["script"]), run["clips"])
    assert rows
    assert all(row["clip"] for row in rows if row["text_translated"])
    missing = comparison.inventory(comparison.read_script(run["script"]),
                                   tmp_path / "gone")
    assert not any(row["clip"] for row in missing)


@needs_ffmpeg
def test_a_window_with_no_playable_line_is_an_error_not_an_empty_scene(tmp_path):
    run = completed_run(tmp_path)
    rows = comparison.inventory(comparison.read_script(run["script"]), run["clips"])
    with pytest.raises(comparison.ComparisonError, match="no line with generated audio"):
        comparison.scenes_from(rows, [(500.0, 520.0)])


def test_an_oversized_window_is_refused_by_a_bounded_runner():
    rows = [{"index": 0, "start": 0.0, "end": 1.0, "speaker": "A", "text_src": "x",
             "text_translated": "y", "delivery": "", "clip": "c.wav"}]
    with pytest.raises(comparison.ComparisonError, match="bounded excerpt"):
        comparison.scenes_from(rows, [(0.0, 999.0)])


def test_a_backwards_window_is_refused():
    with pytest.raises(comparison.ComparisonError, match="non-positive window"):
        comparison.scenes_from([], [(10.0, 5.0)])


def test_suggested_windows_prefer_more_speakers_in_less_time():
    rows = [
        {"index": 0, "start": 0.0, "end": 1.0, "speaker": "A", "clip": "a",
         "text_src": "", "text_translated": "", "delivery": ""},
        {"index": 1, "start": 1.2, "end": 2.0, "speaker": "B", "clip": "b",
         "text_src": "", "text_translated": "", "delivery": ""},
        {"index": 2, "start": 40.0, "end": 41.0, "speaker": "A", "clip": "c",
         "text_src": "", "text_translated": "", "delivery": ""},
    ]
    found = comparison.suggest(rows, count=2)
    assert found[0]["speakers"] == ["A", "B"]
    assert found[0]["lines"] == 2


# -- fairness ---------------------------------------------------------------

def test_blind_labels_are_deterministic_and_always_revealable():
    first = comparison.blind_labels(["baseline", "improved"], "run-1")
    assert first == comparison.blind_labels(["baseline", "improved"], "run-1")
    assert set(first) == {"A", "B"}
    assert set(first.values()) == {"baseline", "improved"}
    # a different comparison relabels, so the baseline is not always first
    others = [comparison.blind_labels(["baseline", "improved"], f"run-{n}")
              for n in range(12)]
    assert len({tuple(sorted(m.items())) for m in others}) > 1


def test_identical_variants_are_a_note_not_a_problem():
    rows = [{"scene": {"index": 0}, "variants": [
        {"name": "a", "mixed": "a.wav", "mixed_sha256": "same", "tts_requests": 0,
         "measured": {"duration": 10.0}, "delivery": {}},
        {"name": "b", "mixed": "b.wav", "mixed_sha256": "same", "tts_requests": 0,
         "measured": {"duration": 10.0}, "delivery": {}},
    ]}]
    result = comparison.objective(rows)
    assert result["ready"] is True
    assert "changed nothing here" in result["notes"][0]


def test_variants_of_different_lengths_are_not_the_same_window():
    rows = [{"scene": {"index": 0}, "variants": [
        {"name": "a", "mixed": "a.wav", "mixed_sha256": "x", "tts_requests": 0,
         "measured": {"duration": 10.0}, "delivery": {}},
        {"name": "b", "mixed": "b.wav", "mixed_sha256": "y", "tts_requests": 0,
         "measured": {"duration": 12.0}, "delivery": {}},
    ]}]
    result = comparison.objective(rows)
    assert result["ready"] is False
    assert "not the same window" in result["problems"][0]


def test_any_generation_voids_a_same_take_comparison():
    rows = [{"scene": {"index": 0}, "variants": [
        {"name": "a", "mixed": "a.wav", "mixed_sha256": "x", "tts_requests": 0,
         "measured": {"duration": 10.0}, "delivery": {}},
        {"name": "b", "mixed": "b.wav", "mixed_sha256": "y", "tts_requests": 3,
         "measured": {"duration": 10.0}, "delivery": {}},
    ]}]
    result = comparison.objective(rows)
    assert result["ready"] is False
    assert "not a same-take comparison" in result["problems"][0]


def test_a_failed_export_stops_the_listening_test():
    rows = [{"scene": {"index": 0}, "variants": [
        {"name": "a", "mixed": "a.wav", "mixed_sha256": "x", "tts_requests": 0,
         "measured": {"duration": 10.0},
         "delivery": {"findings": [{"code": "duration_mismatch", "severity": "failure"}]}},
        {"name": "b", "mixed": "b.wav", "mixed_sha256": "y", "tts_requests": 0,
         "measured": {"duration": 10.0}, "delivery": {}},
    ]}]
    result = comparison.objective(rows)
    assert result["ready"] is False
    assert "export validation failed" in result["problems"][0]


def test_one_playable_variant_is_not_a_comparison():
    rows = [{"scene": {"index": 0}, "variants": [
        {"name": "a", "mixed": None, "tts_requests": 0, "delivery": {}},
        {"name": "b", "mixed": "b.wav", "mixed_sha256": "y", "tts_requests": 0,
         "measured": {"duration": 10.0}, "delivery": {}},
    ]}]
    assert comparison.objective(rows)["ready"] is False


@needs_ffmpeg
def test_level_matching_touches_only_the_playback_copies(tmp_path):
    from doblarr.levels import analyze
    from tests.test_treatments import tone

    quiet = tone(tmp_path / "quiet.wav", seconds=1.0, amplitude=0.05, channels=2)
    loud = tone(tmp_path / "loud.wav", seconds=1.0, amplitude=0.5, channels=2)
    rows = [
        {"name": "quiet", "mixed": str(quiet),
         "measured": {"speech_db": analyze(quiet)["speech_db"]}},
        {"name": "loud", "mixed": str(loud),
         "measured": {"speech_db": analyze(loud)["speech_db"]}},
    ]
    matched = comparison.level_match(rows, tmp_path)
    assert {row["name"]: row["matched_gain_db"] for row in matched}["loud"] == 0.0
    assert matched[0]["matched_gain_db"] > 5
    # the production files are untouched
    assert analyze(quiet)["speech_db"] == rows[0]["measured"]["speech_db"]
    levelled = analyze(matched[0]["matched"])["speech_db"]
    assert levelled == pytest.approx(rows[1]["measured"]["speech_db"], abs=0.5)
    assert "playback copy only" in matched[0]["matched_note"]


def test_level_matching_is_skipped_when_there_is_nothing_to_match():
    rows = [{"name": "only", "mixed": "a.wav", "measured": {"speech_db": -20.0}}]
    assert comparison.level_match(rows, "unused") == rows


# -- the engine that refuses ------------------------------------------------

def test_the_comparison_engine_makes_generation_a_crash():
    engine = comparison.RefusesToGenerate(["v1"])
    assert engine.list_voices() == [{"id": "v1", "name": "v1"}]
    with pytest.raises(comparison.ComparisonError, match="reuses the takes"):
        engine.synthesize_to_file("v1", "hola", "es", "x.wav")
    assert engine.attempts == [{"profile": "v1", "text": "hola"}]


# -- end to end --------------------------------------------------------------

@needs_ffmpeg
def test_a_bounded_comparison_reuses_every_take_and_generates_nothing(tmp_path):
    run = completed_run(tmp_path / "source")
    config = Config.load(tmp_path / "config.yaml")
    manifest = comparison.run(
        config, comparison_id="c1", source=run["media"], script=run["script"],
        clips=run["clips"], windows=[(0.5, 8.5)],
        variants={"baseline": {}, "space": {"treatments.mode": "on",
                                            "treatments.default": "room"}},
        root=tmp_path / "out", source_lang="ja", target_lang="es",
        note="fixture comparison")
    scene = manifest["scenes"][0]
    assert scene["scene"]["lines"] >= 2
    for variant in scene["variants"]:
        assert variant["tts_requests"] == 0
        assert variant["mixed"] and variant["mixed_sha256"]
        assert variant["delivery"]["state"] in ("passed", "warned")
    baseline = next(v for v in scene["variants"] if v["name"] == "baseline")
    space = next(v for v in scene["variants"] if v["name"] == "space")
    assert baseline["active"]["treatments"]["mode"] == "off"
    assert space["active"]["treatments"]["mode"] == "on"
    assert space["active"]["treatments"]["applied"] >= 1
    # a real, audible difference rather than two copies of one file
    assert baseline["mixed_sha256"] != space["mixed_sha256"]
    assert manifest["objective"]["ready"] is True
    root = tmp_path / "out" / "c1"
    assert (root / "index.html").is_file()
    assert (root / "results.md").is_file()
    assert (root / "manifest.json").is_file()
    assert (root / "scene-00" / "excerpt.mkv").is_file()
    assert (root / "scene-00" / "source.wav").is_file()


@needs_ffmpeg
def test_the_cast_and_the_window_survive_the_import(tmp_path):
    run = completed_run(tmp_path / "source")
    config = Config.load(tmp_path / "config.yaml")
    manifest = comparison.run(
        config, comparison_id="c2", source=run["media"], script=run["script"],
        clips=run["clips"], windows=[(0.5, 8.5)],
        variants={"a": {}, "b": {"boundaries.trim": True}},
        root=tmp_path / "out", source_lang="ja", target_lang="es")
    scene = manifest["scenes"][0]
    speakers = set(scene["scene"]["speakers"])
    # Whatever the completed run cast, the comparison keeps: a variant that
    # quietly recast the scene would be comparing two different performances.
    expected = {seg.speaker for seg in run["job"].segments
                if scene["scene"]["window"]["start"] <= seg.start
                <= scene["scene"]["window"]["end"]}
    assert speakers and speakers <= expected
    for variant in scene["variants"]:
        assert {row["speaker"] for row in variant["lines"]} == speakers
    # every imported take is the one the completed run produced
    prepared = scene["prepared"]["a"]
    assert prepared["imported"]
    assert all(row["clip"].endswith(".wav") for row in prepared["imported"])
    # Source time is the excerpt's own time, because the excerpt is what this
    # job's source track actually is. A span carrying the episode's timestamps
    # would send source measurement past the end of the file.
    offset = prepared["episode_offset"]
    for row in prepared["cues"]:
        assert row["source"][0]["domain"] == "source"
        assert row["excerpt"]["domain"] == "target"
        assert row["source"][0]["end"] <= scene["scene"]["duration"] + 1e-6
        # and where it sits in the episode is still recorded, just not as a
        # time anything downstream could seek to
        assert row["episode"][0] == pytest.approx(row["source"][0]["start"] + offset,
                                                  abs=1e-3)


@needs_ffmpeg
def test_a_treated_variant_produces_the_treated_role_and_the_other_does_not(tmp_path):
    run = completed_run(tmp_path / "source")
    config = Config.load(tmp_path / "config.yaml")
    manifest = comparison.run(
        config, comparison_id="c3", source=run["media"], script=run["script"],
        clips=run["clips"], windows=[(0.5, 8.5)],
        variants={"dry": {}, "phone": {"treatments.mode": "on",
                                       "treatments.default": "phone"}},
        root=tmp_path / "out", source_lang="ja", target_lang="es")
    scene = manifest["scenes"][0]
    dry = next(v for v in scene["variants"] if v["name"] == "dry")
    phone = next(v for v in scene["variants"] if v["name"] == "phone")
    assert all(row["treatment"]["outcome"] == "bypassed" for row in dry["lines"])
    assert any(row["treatment"]["preset"] == "phone"
               and row["treatment"]["outcome"] == "applied" for row in phone["lines"])
    assert any(row["role"] == TREATED for row in phone["lines"])
    assert all(row["role"] != TREATED for row in dry["lines"])


@needs_ffmpeg
def test_a_comparison_never_overwrites_one_somebody_may_have_heard(tmp_path):
    run = completed_run(tmp_path / "source")
    config = Config.load(tmp_path / "config.yaml")
    kwargs = dict(source=run["media"], script=run["script"], clips=run["clips"],
                  windows=[(0.5, 8.5)], variants={"a": {}, "b": {"boundaries.trim": True}},
                  root=tmp_path / "out", source_lang="ja", target_lang="es")
    comparison.run(config, comparison_id="c4", **kwargs)
    with pytest.raises(comparison.ComparisonError, match="already holds a comparison"):
        comparison.run(config, comparison_id="c4", **kwargs)


@needs_ffmpeg
def test_the_results_sheet_is_delivered_empty_with_the_mapping_written_down(tmp_path):
    run = completed_run(tmp_path / "source")
    config = Config.load(tmp_path / "config.yaml")
    manifest = comparison.run(
        config, comparison_id="c5", source=run["media"], script=run["script"],
        clips=run["clips"], windows=[(0.5, 8.5)],
        variants={"baseline": {}, "trimmed": {"boundaries.trim": True}},
        root=tmp_path / "out", source_lang="ja", target_lang="es")
    sheet = (tmp_path / "out" / "c5" / "results.md").read_text(encoding="utf-8")
    assert "better / same / worse / uncertain" in sheet
    assert "`baseline`" in sheet and "`trimmed`" in sheet
    # nothing is pre-filled: no answer is invented on the listener's behalf
    assert "| better |" not in sheet
    assert "bounded excerpt" in sheet
    page = (tmp_path / "out" / "c5" / "index.html").read_text(encoding="utf-8")
    assert "Reveal which is which" in page
    assert "No speech was generated" in page
    for label, name in manifest["labels"].items():
        assert f"{label} = {name}" in page
    # every playable file the page links to is relative and on disk
    import re
    for src in re.findall(r'data-src="([^"]+)"', page):
        assert not src.startswith(("/", "http"))
        assert (tmp_path / "out" / "c5" / src).is_file()
