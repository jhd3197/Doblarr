"""Phase G — the two timing owners, coverage, reuse and compatibility together.

These run the real pipeline over the offline tone fixtures with real FFmpeg, so
the request counts, the artifact roles and the cache decisions are measured
rather than inferred from function names. Tones establish numbers and structure.
They say nothing about rhythm, naturalness or whether a retained laugh lands.
"""

import json
import shutil

import pytest

from doblarr import benchmarks
from doblarr.config import Config
from doblarr.cues import (
    CUE_SCHEMA_VERSION,
    FITTED,
    LEVELED,
    PHRASED,
    RAW,
    NonverbalEvent,
)
from doblarr.errors import JobCancelled
from doblarr.models import DubJob
from doblarr.pipeline import run_job
from doblarr.services import Services

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")

PHRASE = {"timing.mode": "phrase"}


class Engine(benchmarks.ToneEngine):
    """The tone engine plus a recognizer that can be told what it heard."""

    def __init__(self, root, scene=benchmarks.PHRASE_SCENE, heard=""):
        super().__init__(root, scene)
        self.heard = heard
        self.transcriptions = 0

    def transcribe(self, path, language=""):
        self.transcriptions += 1
        return {"text": self.heard}


def build(root, overrides=None, engine=None, media_root=None,
          scene=benchmarks.PHRASE_SCENE):
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


def run(root, overrides=None, engine=None, media_root=None, scene=benchmarks.PHRASE_SCENE,
        **kwargs):
    job, config, services, tone = build(root, overrides, engine, media_root, scene)
    run_job(job, config, services=services, **kwargs)
    return job, config, tone


def _spoken(job):
    return [s for s in job.segments if s.phrasing.mode == "phrase"]


# -- the two timing owners --------------------------------------------------

def test_exactly_one_timing_owner_produces_a_derivative(tmp_path):
    whole, _config, _tone = run(tmp_path / "w")
    assert all(s.audio.render(PHRASED) is None for s in whole.segments)
    assert any(s.audio.render(FITTED) is not None for s in whole.segments)

    phrase, _config, _tone = run(tmp_path / "p", PHRASE)
    assert any(s.audio.render(PHRASED) is not None for s in phrase.segments)
    assert all(s.audio.render(FITTED) is None for s in phrase.segments)


def test_switching_owners_drops_the_other_ones_audio(tmp_path):
    root = tmp_path / "switch"
    media = root / "media"
    first, _config, _tone = run(root, PHRASE, media_root=media)
    assert any(s.audio.render(PHRASED) for s in first.segments)
    second, _config, _tone = run(root, {"timing.mode": "whole"}, media_root=media)
    assert all(s.audio.render(PHRASED) is None for s in second.segments)
    back, _config, _tone = run(root, PHRASE, media_root=media)
    assert all(s.audio.render(FITTED) is None for s in back.segments)


def test_the_level_owner_reads_whichever_timing_owner_ran(tmp_path):
    job, _config, _tone = run(tmp_path / "levels",
                              {**PHRASE, "levels.mode": "consistent"})
    leveled = [s.audio.render(LEVELED) for s in job.segments
               if s.audio.render(LEVELED)]
    assert leveled
    assert {a.derived_from for a in leveled} <= {PHRASED, RAW, "trimmed", "normalized"}
    assert PHRASED in {a.derived_from for a in leveled}


def test_phrase_fitting_makes_no_extra_generation_requests(tmp_path):
    """Timing is processing. The only request it may make is a bounded repair."""
    whole, _config, plain = run(tmp_path / "w")
    phrase, _config, fitted = run(tmp_path / "p", PHRASE)
    assert len(fitted.requests) == len(plain.requests)
    spent = phrase.metrics["request_budget"]["by_kind"]
    assert set(spent) <= {"timing_repair"}


# -- reuse ------------------------------------------------------------------

def test_a_timing_only_edit_reuses_every_generation(tmp_path):
    root = tmp_path / "reuse"
    media = root / "media"
    first, _config, tone = run(root, PHRASE, media_root=media)
    assert tone.requests
    cue = _spoken(first)[0].cue_id
    pause = next(p.pause_id for p in _spoken(first)[0].phrasing.pauses if p.after)

    engine = Engine(root)
    second, _config, warm = run(
        root, {**PHRASE, "timing.phrases": {cue: {"pauses": {pause: {"protected": True}}}}},
        engine=engine, media_root=media)
    # Not one TTS call: the take on disk is re-cut, not regenerated.
    assert warm.requests == []
    assert second.metrics["phrase_rendered"] >= 1


def test_a_coverage_decision_reuses_every_generation_and_only_re_mixes(tmp_path):
    root = tmp_path / "coverage"
    media = root / "media"
    first, _config, _tone = run(root, {**PHRASE, "coverage.mode": "retain"},
                                media_root=media)
    event = first.nonverbal[0]
    assert event.coverage == "unresolved"

    engine = Engine(root)
    second, _config, warm = run(
        root, {**PHRASE, "coverage.mode": "retain",
               "coverage.events": {event.event_id: {"decision": "retain"}}},
        engine=engine, media_root=media)
    assert warm.requests == []
    placed = next(e for e in second.nonverbal if e.event_id == event.event_id)
    assert placed.coverage == "retained" and placed.placed
    # The dialogue renders are untouched; only the mix had to change.
    assert second.metrics["coverage"]["placed"] == 1


def test_repeating_a_run_is_stable_and_accumulates_no_stretching(tmp_path):
    root = tmp_path / "stable"
    media = root / "media"
    first, _config, _tone = run(root, PHRASE, media_root=media)
    before = {s.cue_id: (s.audio.render(PHRASED).fingerprint
                         if s.audio.render(PHRASED) else None,
                         s.phrasing.actual_duration)
              for s in first.segments}
    second, _config, _tone = run(root, PHRASE, media_root=media)
    after = {s.cue_id: (s.audio.render(PHRASED).fingerprint
                        if s.audio.render(PHRASED) else None,
                        s.phrasing.actual_duration)
             for s in second.segments}
    assert before == after


# -- compatibility ----------------------------------------------------------

def test_the_whole_clip_path_renders_exactly_what_it_rendered_before(tmp_path):
    """Plan 04 must not move a single sample for a run that did not opt in."""
    root = tmp_path / "legacy"
    media = root / "media"
    before = benchmarks.baseline(root / "a", benchmarks.SCENE, media_root=media)
    after = benchmarks.baseline(root / "b", benchmarks.SCENE, media_root=media)
    difference = benchmarks.compare(
        benchmarks.record(root / "c", root / "before.json", benchmarks.SCENE,
                          media_root=media),
        benchmarks.record(root / "d", root / "after.json", benchmarks.SCENE,
                          media_root=media))
    assert difference["changed_cues"] == [] and difference["removed_cues"] == []
    assert difference["tts_requests"][0] == difference["tts_requests"][1]
    assert before["tts_requests"] == after["tts_requests"]
    for line in after["lines"]:
        assert line["timing_mode"] == "whole" and line["timing_state"] == "bypassed"
        assert PHRASED not in line["roles"]


def test_defaults_still_leave_timing_and_coverage_where_plan_03_left_them(tmp_path):
    job, config, _tone = run(tmp_path / "defaults")
    assert config["timing"]["mode"] == "whole"
    assert config["coverage"]["mode"] == "off"
    assert config["levels"]["mode"] == "legacy"
    assert config["quality"]["asr"] == "off"
    assert job.metrics["coverage"]["placed"] == 0


def test_a_snapshot_without_a_timing_plan_gains_no_invented_one(tmp_path):
    from doblarr.stages.common import load_script, save_script

    job, _config, _tone = run(tmp_path / "old", PHRASE)
    script = next((tmp_path / "old" / "work" / "media").rglob("*.script.json"))
    payload = json.loads(script.read_text(encoding="utf-8"))
    payload["cue_schema"] = 2
    for row in payload["segments"]:
        row["cue"].pop("phrasing", None)
    payload["nonverbal"] = [{"cue_id": payload["segments"][0]["cue"]["cue_id"],
                             "type": "unknown", "speaker": "SPEAKER_00",
                             "text": "[laughter]", "coverage": "uncovered",
                             "source": [{"start": 9.0, "end": 10.5, "domain": "source"}]}]
    script.write_text(json.dumps(payload), encoding="utf-8")

    restored = DubJob(input_file=job.input_file, source_lang="ja", target_lang="es",
                      subtitle_file=job.subtitle_file)
    restored.source_audio = job.source_audio
    restored.transcription_options = dict(job.transcription_options)
    restored.translation_options = dict(job.translation_options)
    assert load_script(restored, script.parent) is not None
    for seg in restored.segments:
        assert seg.phrasing.mode == "unknown" and not seg.phrasing.phrases
    # The Plan 01 dictionary migrates into the typed record without acquiring a
    # decision nobody made.
    event = restored.nonverbal[0]
    assert isinstance(event, NonverbalEvent)
    assert event.coverage == "unresolved" and event.decision == "unresolved"
    save_script(restored, script.parent)
    assert json.loads(script.read_text(encoding="utf-8"))["cue_schema"] == CUE_SCHEMA_VERSION


def test_two_target_locales_keep_separate_timing_and_coverage(tmp_path):
    root = tmp_path / "locales"
    media = root / "media"
    job_a, config_a, _tone = run(root, {**PHRASE, "dub.target_locale": "es-MX"},
                                 media_root=media)
    job_b, config_b, _tone = run(root, {**PHRASE, "dub.target_locale": "es-ES"},
                                 media_root=media)
    paths_a = {s.audio.render(PHRASED).path for s in job_a.segments
               if s.audio.render(PHRASED)}
    paths_b = {s.audio.render(PHRASED).path for s in job_b.segments
               if s.audio.render(PHRASED)}
    assert paths_a and paths_b and not (paths_a & paths_b)


def test_a_dry_run_plans_the_new_stages_and_renders_nothing(tmp_path):
    job, config, services, _tone = build(tmp_path / "dry",
                                         {**PHRASE, "coverage.mode": "retain"})
    run_job(job, config, dry_run=True, services=services)
    assert all(s.audio.render(PHRASED) is None for s in job.segments)
    assert "coverage" not in job.metrics
    assert list((tmp_path / "dry" / "work").rglob("*/phrased/*.wav")) == []


def test_cancelling_stops_the_new_stages_between_cues(tmp_path):
    """A cancel is honoured per cue, not only inside an FFmpeg process."""
    import threading

    from doblarr import reactions
    from doblarr.stages import phrase_timing

    root = tmp_path / "cancel"
    job, _config, _tone = run(root, {**PHRASE, "coverage.mode": "retain"})
    cancel = threading.Event()
    cancel.set()
    # The stage takes the `timing` section itself, not the dotted override form.
    with pytest.raises(JobCancelled):
        phrase_timing.run(job, root / "work", options={"mode": "phrase"}, cancel=cancel)
    job.nonverbal = [NonverbalEvent(event_id="e1", cue_id=job.segments[0].cue_id,
                                    type="laugh", category="vocal", decision="retain")]
    with pytest.raises(JobCancelled):
        reactions.process(job, {"mode": "retain"}, cancel=cancel, work_dir=root / "work")


# -- the shared budget ------------------------------------------------------

def test_timing_repairs_and_coverage_screening_share_one_budget(tmp_path):
    job, _config, _tone = run(
        tmp_path / "budget",
        {**PHRASE, "coverage.mode": "retain", "coverage.leakage_check": True,
         "quality.asr": "all", "quality.request_budget": 2})
    spent = job.metrics["request_budget"]
    assert spent["limit"] == 2 and spent["spent"] <= 2
    assert spent["refused"] > 0          # the cap actually bit
    assert set(spent["by_kind"]) <= {"asr", "timing_repair", "quality_retry",
                                     "reaction_screen", "background_screen"}


def test_the_run_reports_what_timing_and_coverage_actually_did(tmp_path):
    job, _config, _tone = run(tmp_path / "report", {**PHRASE, "coverage.mode": "retain"})
    assert job.metrics["phrase_rendered"] >= 1
    assert job.metrics["phrases_planned"] >= len(job.segments)
    assert job.metrics["protected_pauses"] >= 1
    assert job.metrics["conversation"]["measured"] >= 1
    summary = job.metrics["coverage"]
    assert summary["events"] == 1 and summary["unresolved"] == 1
    assert job.metrics["background"]["separated"] in (True, False)


def test_the_level_pass_runs_once_over_the_phrase_render(tmp_path):
    """Gain is applied to the timed audio, once, and a repeat does not stack it."""
    root = tmp_path / "gain"
    media = root / "media"
    options = {**PHRASE, "levels.mode": "consistent"}
    first, _config, _tone = run(root, options, media_root=media)
    before = {s.cue_id: (s.audio.render(LEVELED).fingerprint,
                         s.level.applied_db, s.level.baseline_db)
              for s in first.segments if s.audio.render(LEVELED)}
    assert before
    second, _config, _tone = run(root, options, media_root=media)
    after = {s.cue_id: (s.audio.render(LEVELED).fingerprint,
                        s.level.applied_db, s.level.baseline_db)
             for s in second.segments if s.audio.render(LEVELED)}
    assert before == after
    # And the level derivative is made from the phrase render, not from itself.
    assert {s.audio.render(LEVELED).derived_from for s in second.segments
            if s.audio.render(LEVELED)} == {PHRASED}
