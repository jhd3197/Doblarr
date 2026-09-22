"""Phase G — one shared budget, targeted invalidation, recovery and compatibility.

These run the real pipeline over the offline tone fixtures with real FFmpeg, so
the request counts and cache decisions are measured rather than inferred from
function names. They say nothing about how any of it sounds.
"""

import shutil

import pytest

from doblarr import benchmarks, levels
from doblarr.budget import RequestBudget
from doblarr.config import Config
from doblarr.cues import (
    CUE_SCHEMA_VERSION,
    FITTED,
    LEVELED,
    NORMALIZED,
    RAW,
    TRIMMED,
)
from doblarr.models import DubJob
from doblarr.pipeline import run_job
from doblarr.services import Services

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")


class Engine(benchmarks.ToneEngine):
    """The tone engine plus a recognizer that can be told what it heard."""

    def __init__(self, root, scene=benchmarks.SCENE, heard=None, fail_transcribe=False):
        super().__init__(root, scene)
        self.heard = heard or {}
        self.fail_transcribe = fail_transcribe
        self.transcriptions = 0

    def transcribe(self, path, language=""):
        self.transcriptions += 1
        if self.fail_transcribe:
            raise RuntimeError("the recognizer is unreachable")
        return {"text": self.heard.get("text", "")}


def build(root, overrides=None, engine=None, scene=benchmarks.SCENE, media_root=None):
    media, subtitles = benchmarks.write_media(media_root or root / "media", scene)
    config = Config.load(root / "config.yaml").with_overrides({
        "paths.work_dir": str(root / "work"),
        "paths.output_dir": str(root / "output"),
        "dub.dry_run": False,
        "dub.voice_mode": "preset",
        "dub.preset_voices": ["tone-voice"],
        "dub.preserve_versions": False,
        "transcribe.diarize": False,
        "quality.asr": "off",
        **(overrides or {}),
    })
    tone = engine or Engine(root, scene)
    services = Services(config)
    services._cache["voicebox"] = tone
    job = DubJob(input_file=media, source_lang="ja", target_lang="es",
                 subtitle_file=subtitles)
    job.script_is_target = True
    return job, config, services, tone


def run(root, overrides=None, engine=None, media_root=None):
    job, config, services, tone = build(root, overrides, engine, media_root=media_root)
    run_job(job, config, services=services)
    return job, tone, config


# -- the shared budget -----------------------------------------------------

def test_recognition_candidates_and_repairs_all_spend_one_budget(tmp_path):
    engine = Engine(tmp_path, heard={"text": "algo completamente distinto"})
    job, tone, _ = run(tmp_path, {
        "quality.asr": "all",
        "quality.max_retries": 1,
        "quality.request_budget": 3,
    }, engine)
    budget = job.metrics["request_budget"]
    assert budget["limit"] == 3 and budget["spent"] == 3
    assert budget["exhausted"] and budget["refused"] > 0
    # Every kind of extra request is counted, and none of them is free.
    assert set(budget["by_kind"]) <= {"asr", "quality_retry", "timing_repair", "candidate"}
    assert sum(budget["by_kind"].values()) == 3


def test_polling_is_not_charged_as_a_request(tmp_path):
    """A budget counts creations. Waiting for one is not asking for another."""
    budget = RequestBudget(limit=2)
    assert budget.charge("asr") and budget.charge("candidate")
    assert not budget.charge("asr")
    assert budget.snapshot()["by_kind"] == {"asr": 1, "candidate": 1}
    assert budget.snapshot()["spent"] == 2      # no poll ever appears here


def test_an_exhausted_budget_leaves_usable_audio_and_visible_evidence(tmp_path):
    engine = Engine(tmp_path, heard={"text": "algo completamente distinto"})
    job, _, _ = run(tmp_path, {"quality.asr": "all", "quality.request_budget": 1}, engine)
    assert job.output_file and job.output_file.is_file()
    unchecked = [s for s in job.segments if not s.verification.checked]
    assert unchecked, "the budget should have stopped at least one check"
    assert any("budget is exhausted" in s.verification.reason for s in unchecked)
    assert job.metrics["verification"]["unchecked"] >= 1


# -- targeted invalidation -------------------------------------------------

def test_a_processing_only_change_reuses_every_generation(tmp_path):
    media = tmp_path / "media"
    job, tone, _ = run(tmp_path / "a", media_root=media)
    first = len(tone.requests)
    assert first > 0

    # Same media, same script, new work dir, level processing turned on. The
    # clips are regenerated because the work dir is new, so the comparison
    # that matters is the second run *in the same* work dir.
    again, tone_again, _ = run(tmp_path / "a", {"levels.mode": "consistent"},
                               media_root=media)
    assert len(tone_again.requests) == 0, "a level change must not regenerate speech"
    assert again.metrics.get("tts_cache_hits") == first
    assert all(s.audio.render(LEVELED) is not None for s in again.segments)
    assert all(s.audio.render(NORMALIZED) is None for s in again.segments)


def test_turning_the_level_owner_off_again_drops_its_derivatives(tmp_path):
    media = tmp_path / "media"
    run(tmp_path / "a", {"levels.mode": "consistent"}, media_root=media)
    job, tone, _ = run(tmp_path / "a", {"levels.mode": "legacy"}, media_root=media)
    assert len(tone.requests) == 0
    assert all(s.audio.render(LEVELED) is None for s in job.segments)
    assert all(s.audio.render(NORMALIZED) is not None for s in job.segments)
    assert all(s.level.outcome == "bypassed" for s in job.segments)


def test_a_checker_policy_change_alone_generates_no_audio(tmp_path):
    media = tmp_path / "media"
    run(tmp_path / "a", media_root=media)
    engine = Engine(tmp_path / "a", heard={"text": "Primera linea"})
    # Retries off, so this isolates the policy change itself: a repair the
    # checker legitimately asks for is its own behaviour and its own test.
    job, tone, _ = run(tmp_path / "a", {"quality.asr": "all", "quality.max_retries": 0},
                       engine, media_root=media)
    assert len(tone.requests) == 0            # no new speech
    assert tone.transcriptions > 0            # but the lines were listened to
    assert job.metrics["verification"]["checked"] > 0


def test_each_stage_reads_its_declared_upstream_not_its_own_output(tmp_path):
    job, _, _ = run(tmp_path, {"levels.mode": "consistent", "boundaries.trim": True})
    for seg in job.segments:
        for artifact in seg.audio.renders:
            assert artifact.role != artifact.derived_from
        leveled = seg.audio.render(LEVELED)
        if leveled is not None:
            assert leveled.derived_from in (FITTED, TRIMMED, RAW)


def test_repeating_a_run_is_stable_and_accumulates_no_gain(tmp_path):
    media = tmp_path / "media"
    options = {"levels.mode": "consistent"}
    first, _, _ = run(tmp_path / "a", options, media_root=media)
    before = {s.cue_id: levels.analyze(s.audio.render(LEVELED).path)["speech_db"]
              for s in first.segments if s.audio.render(LEVELED)}
    second, tone, _ = run(tmp_path / "a", options, media_root=media)
    after = {s.cue_id: levels.analyze(s.audio.render(LEVELED).path)["speech_db"]
             for s in second.segments if s.audio.render(LEVELED)}
    assert before and before.keys() == after.keys()
    for cue, value in before.items():
        assert after[cue] == pytest.approx(value, abs=0.01)
    assert len(tone.requests) == 0


# -- failure and recovery --------------------------------------------------

def test_a_recognizer_failure_is_reviewable_and_does_not_fail_the_run(tmp_path):
    engine = Engine(tmp_path, fail_transcribe=True)
    job, _, _ = run(tmp_path, {"quality.asr": "all"}, engine)
    assert job.output_file and job.output_file.is_file()
    states = {s.verification.state for s in job.segments}
    assert states == {"failed"}
    assert all("unreachable" in s.verification.reason for s in job.segments)
    assert all(not s.verification.checked for s in job.segments)
    # Unverified, explicitly — never quietly approved.
    codes = {f.code for s in job.segments for f in s.findings}
    assert "content_unverified" in codes


def test_missing_source_audio_downgrades_levels_without_stopping_the_run(tmp_path):
    job, config, services, tone = build(tmp_path, {"levels.mode": "follow_source"})
    run_job(job, config, services=services)
    # The source really was measured — `follow_source` implies it — but no
    # Demucs means the dialogue reference is the mix, so nothing measured from
    # it may drive an automatic gain.
    assert all(s.measurement.speech_db is not None for s in job.segments)
    assert all(s.measurement.contaminated for s in job.segments)
    assert all(not s.measurement.trusted for s in job.segments)
    assert all(s.measurement.exclusions for s in job.segments)
    assert all(s.level.outcome == "fallback" for s in job.segments)
    assert job.output_file.is_file()


def test_a_dry_run_measures_nothing_and_renders_nothing(tmp_path):
    job, config, services, tone = build(tmp_path, {"levels.mode": "follow_source",
                                                   "quality.asr": "all"})
    run_job(job, config, dry_run=True, services=services)
    assert not tone.requests and tone.transcriptions == 0
    assert all(s.audio.render(LEVELED) is None for s in job.segments)
    assert all(s.verification.state == "unknown" for s in job.segments)


# -- compatibility ---------------------------------------------------------

def test_the_legacy_path_renders_exactly_what_it_rendered_before(tmp_path):
    """Defaults off: the new roles never appear and loudness stays pre-fit."""
    job, _, _ = run(tmp_path)
    assert all(s.audio.render(LEVELED) is None for s in job.segments)
    assert all(s.audio.render(NORMALIZED) is not None for s in job.segments)
    assert all(s.level.mode == "legacy" for s in job.segments)
    assert all(s.verification.state == "skipped" for s in job.segments)
    assert all(s.intent.empty for s in job.segments)


def test_a_tease_keeps_its_own_namespace_and_still_measures_nothing_it_should_not(tmp_path):
    media, subtitles = benchmarks.write_media(tmp_path / "media")
    config = Config.load(tmp_path / "config.yaml").with_overrides({
        "paths.work_dir": str(tmp_path / "work"),
        "paths.output_dir": str(tmp_path / "output"),
        "dub.dry_run": False, "dub.voice_mode": "preset",
        "dub.preset_voices": ["tone-voice"], "dub.preserve_versions": False,
        "transcribe.diarize": False, "levels.mode": "consistent",
    })
    services = Services(config)
    services._cache["voicebox"] = Engine(tmp_path)
    job = DubJob(input_file=media, source_lang="ja", target_lang="es",
                 subtitle_file=subtitles, kind="tease")
    job.script_is_target = True
    run_job(job, config, services=services)
    assert job.output_file.is_file()
    assert ".tease" in str(job.artifacts_dir) or "tease" in job.output_file.name
    assert all(s.audio.render(LEVELED) is not None for s in job.segments)


def test_two_target_locales_do_not_share_takes_levels_or_evidence(tmp_path):
    media, subtitles = benchmarks.write_media(tmp_path / "media")
    results = {}
    for locale in ("es-MX", "es-VE"):
        config = Config.load(tmp_path / "config.yaml").with_overrides({
            "paths.work_dir": str(tmp_path / "work"),
            "paths.output_dir": str(tmp_path / "output"),
            "dub.dry_run": False, "dub.voice_mode": "preset",
            "dub.preset_voices": ["tone-voice"], "dub.preserve_versions": False,
            "transcribe.diarize": False, "levels.mode": "consistent",
        })
        services = Services(config)
        engine = Engine(tmp_path)
        services._cache["voicebox"] = engine
        job = DubJob(input_file=media, source_lang="ja", target_lang="es",
                     target_locale=locale, subtitle_file=subtitles)
        job.script_is_target = True
        run_job(job, config, services=services)
        results[locale] = (job, engine)
    mx, ve = results["es-MX"][0], results["es-VE"][0]
    assert mx.artifacts_dir != ve.artifacts_dir
    assert results["es-VE"][1].requests, "the second locale generated its own speech"
    assert {s.audio.render(LEVELED).path for s in mx.segments} != {
        s.audio.render(LEVELED).path for s in ve.segments}


def test_a_version_one_snapshot_still_loads_and_gains_no_invented_evidence(tmp_path):
    import json

    from doblarr.stages.common import load_script, save_script

    job, _, _ = run(tmp_path)
    path = tmp_path / "work" / "media"
    script = next(path.rglob("*.script.json"))
    payload = json.loads(script.read_text(encoding="utf-8"))
    # Rewrite it as a version-1 payload: no intent, measurement, level or
    # verification anywhere.
    payload["cue_schema"] = 1
    for row in payload["segments"]:
        for key in ("intent", "measurement", "level", "verification", "phrasing"):
            row["cue"].pop(key, None)
    script.write_text(json.dumps(payload), encoding="utf-8")

    restored = DubJob(input_file=job.input_file, source_lang="ja", target_lang="es",
                      subtitle_file=job.subtitle_file)
    restored.source_audio = job.source_audio
    restored.transcription_options = dict(job.transcription_options)
    restored.translation_options = dict(job.translation_options)
    assert load_script(restored, script.parent) is not None
    for seg in restored.segments:
        assert seg.intent.empty and seg.intent.mode == "unknown"
        assert seg.measurement.state == "unknown" and seg.measurement.speech_db is None
        assert seg.level.outcome == "unknown"
        assert seg.verification.state == "unknown" and not seg.verification.checked
        # Plan 04's records are equally empty: that run fitted whole clips and
        # never planned a phrase, which is what an empty plan says.
        assert seg.phrasing.mode == "unknown" and not seg.phrasing.phrases
    # And it round-trips forward without inventing anything.
    save_script(restored, script.parent)
    assert (json.loads(script.read_text(encoding="utf-8"))["cue_schema"]
            == CUE_SCHEMA_VERSION)


def test_cancelling_stops_the_new_stages_between_cues(tmp_path):
    """A cancel is honoured per cue, not only inside an FFmpeg process."""
    import threading

    from doblarr.errors import JobCancelled

    job, config, services, tone = build(tmp_path, {"levels.mode": "follow_source"})
    run_job(job, config, services=services)
    assert job.segments
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(JobCancelled, match="source measurement"):
        levels.measure_sources(job, {"mode": "follow_source", "measure_source": True},
                               cancel=cancel, work_dir=tmp_path / "work")
    with pytest.raises(JobCancelled, match="level processing"):
        levels.process(job, {"mode": "consistent"}, cancel=cancel)


def test_an_audition_measures_the_original_not_its_own_montage(tmp_path):
    """The montage replaces the working audio; it is never the level evidence."""
    media, subtitles = benchmarks.write_media(tmp_path / "media")
    config = Config.load(tmp_path / "config.yaml").with_overrides({
        "paths.work_dir": str(tmp_path / "work"),
        "paths.output_dir": str(tmp_path / "output"),
        "dub.dry_run": False, "dub.voice_mode": "preset",
        "dub.preset_voices": ["tone-voice"], "dub.preserve_versions": False,
        "transcribe.diarize": False, "levels.mode": "follow_source",
    })
    services = Services(config)
    services._cache["voicebox"] = Engine(tmp_path)
    job = DubJob(input_file=media, source_lang="ja", target_lang="es",
                 subtitle_file=subtitles, kind="audition")
    job.script_is_target = True
    run_job(job, config, services=services)

    assert job.source_track is not None
    assert job.source_audio != job.source_track       # the montage took over
    assert "samples" in job.source_audio.name
    # Every cue still keeps the interval it was actually spoken at, and the
    # measurement was taken from the preserved track.
    for seg in job.segments:
        assert seg.placement.montage is not None
        assert seg.measurement.source in ("source-stream", "separated-vocals")


def test_a_mixed_engine_cast_directs_only_the_engine_that_can_be_directed(tmp_path):
    from doblarr.models import Segment, Speaker
    from doblarr.stages import synthesize

    class Recorder(Engine):
        def synthesize_to_file(self, profile, text, language, dest, cancel_event=None,
                               **kwargs):
            dest.parent.mkdir(parents=True, exist_ok=True)
            return super().synthesize_to_file(profile, text, language, dest,
                                              cancel_event, **kwargs)

    job = DubJob(tmp_path / "movie.mkv", "ja", "es")
    job.input_file.write_bytes(b"media")
    job.source_audio = tmp_path / "src.wav"
    job.source_audio.write_bytes(b"src")
    job.speakers = {"A": Speaker("A", voicebox_profile_id="v1"),
                    "B": Speaker("B", voicebox_profile_id="v2")}
    for index, speaker in enumerate(("A", "B")):
        seg = Segment(index, index * 2.0, index * 2.0 + 1.5, "Primera linea",
                      speaker=speaker, text_translated="Primera linea")
        seg.cue_id = f"cue-{index}"
        seg.delivery = "sound exhausted"
        job.segments.append(seg)

    engine = Recorder(tmp_path)
    synthesize.run(job, engine, tmp_path / "work", voice_mode="preset",
                   cast={"A": {"voice": "v1", "engine": "qwen"},
                         "B": {"voice": "v2", "engine": "chatterbox"}})
    directed = [r for r in engine.requests if "instruct" in r["options"]]
    assert len(directed) == 1
    assert job.segments[0].intent.capability == "supported"
    assert job.segments[1].intent.capability == "unsupported"
    assert job.segments[1].intent.unsupported      # asked for, visibly not applied
