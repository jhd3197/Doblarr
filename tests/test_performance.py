"""D06 — acting direction, engine capability, and retained alternate takes."""

from pathlib import Path

import pytest

from doblarr import performance
from doblarr.budget import RequestBudget
from doblarr.clients.voicebox import VoiceboxClient
from doblarr.cues import PerformanceIntent, Selection
from doblarr.models import DubJob, Segment, Speaker
from doblarr.review import apply_edits
from doblarr.stages import synthesize
from tests.test_audio_quality import wav


class Engine:
    """An offline stand-in that records every request it is given."""

    def __init__(self, fail_on=(), directable=True):
        self.requests = []
        self.fail_on = set(fail_on)
        self.directable = directable
        self.observer = None

    def supports_direction(self, engine):
        return self.directable and VoiceboxClient.supports_direction(engine)

    def list_voices(self):
        return [{"id": "voice-1", "name": "Voice"}]

    def create_profile(self, name, language):
        return "voice-1"

    def add_sample(self, profile_id, path, text):
        return None

    def transcribe(self, path, language=""):
        return {"text": ""}

    def synthesize_to_file(self, profile, text, language, dest, cancel_event=None, **kwargs):
        self.requests.append({"profile": profile, "text": text, **kwargs})
        if len(self.requests) in self.fail_on:
            raise RuntimeError("the engine refused this generation")
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        return wav(dest, 6000, 1.0)


def job_with(tmp_path, lines=1, engine="qwen"):
    job = DubJob(tmp_path / "movie.mkv", "ja", "es")
    # Written once: rewriting the input would make every cached script stale,
    # which is exactly the freshness rule the resume test is relying on.
    if not job.input_file.exists():
        job.input_file.write_bytes(b"media")
    job.source_audio = tmp_path / "source.wav"
    if not job.source_audio.exists():
        job.source_audio.write_bytes(b"src")
    job.speakers = {"SPEAKER_00": Speaker("SPEAKER_00", voicebox_profile_id="voice-1")}
    job.segments = [
        Segment(i, i * 2.0, i * 2.0 + 1.5, f"line {i}", text_translated=f"linea {i}")
        for i in range(lines)
    ]
    for i, seg in enumerate(job.segments):
        seg.cue_id = f"cue-{i}"
    return job


# -- composition -----------------------------------------------------------

def test_a_line_direction_replaces_one_field_and_keeps_the_rest():
    seg = Segment(0, 0, 1, "hola")
    seg.delivery = "sound exhausted"
    intent = performance.compose(seg, engine="qwen",
                                 locale_direction="speak in Mexican Spanish",
                                 cast_delivery="warm and measured")
    assert "speak in Mexican Spanish" in intent.effective   # the accent survives
    assert "warm and measured" in intent.effective
    assert intent.effective.endswith("sound exhausted")
    assert [s["origin"] for s in intent.sources if s["field"] == "line"] == ["line"]


def test_a_more_specific_character_direction_wins_its_own_slot():
    seg = Segment(0, 0, 1, "hola")
    intent = performance.compose(seg, engine="qwen", narrator_delivery="narrate calmly",
                                 character_note="clipped and formal",
                                 cast_delivery="gravelly")
    assert intent.effective == "gravelly"
    # The layers it displaced are still visible, so review can explain the result.
    assert {s["origin"] for s in intent.sources} == {"narrator", "character-note", "cast"}


def test_speech_mode_and_traits_compose_in_a_fixed_order():
    seg = Segment(0, 0, 1, "hola")
    seg.intent = PerformanceIntent(mode="whisper", traits=["urgent", "afraid"])
    intent = performance.compose(seg, engine="qwen",
                                 locale_direction="speak in Mexican Spanish")
    assert intent.effective == (
        "speak in Mexican Spanish; whispering, breathy and unprojected; urgent, afraid")
    assert intent.mode == "whisper"
    # Composing twice gives the same string, which is what makes it cacheable.
    assert performance.compose(seg, engine="qwen",
                               locale_direction="speak in Mexican Spanish").effective == (
        intent.effective)


def test_a_speech_mode_never_carries_a_level_or_a_new_voice():
    seg = Segment(0, 0, 1, "hola")
    for mode in ("thought", "whisper", "shout"):
        seg.intent = PerformanceIntent(mode=mode)
        intent = performance.compose(seg, engine="qwen")
        assert "dB" not in intent.effective
        assert intent.treatment == ""
    # The same character speaking, thinking and shouting keeps one profile:
    # nothing here touches the voice at all.
    assert not hasattr(performance.compose(seg, engine="qwen"), "voice")


def test_an_engine_that_cannot_be_directed_is_never_reported_as_applied():
    seg = Segment(0, 0, 1, "hola")
    seg.delivery = "sound exhausted"
    intent = performance.compose(seg, engine="chatterbox")
    assert intent.capability == "unsupported"
    assert intent.unsupported and intent.effective       # asked for, recorded
    assert performance.requested_direction(intent) == "" # but not sent
    assert "not applied" in performance.describe(intent)


def test_an_unknown_engine_is_unknown_not_optimistically_supported():
    seg = Segment(0, 0, 1, "hola")
    assert performance.compose(seg, engine="").capability == "unknown"
    assert VoiceboxClient.supports_direction("qwen")
    assert VoiceboxClient.supports_direction("qwen3-tts")   # alias
    assert not VoiceboxClient.supports_direction("kokoro")


def test_an_intent_edit_bumps_its_revision_only_when_something_changed():
    first = performance.from_edit({"mode": "whisper"})
    assert first.mode == "whisper" and first.revision == 1
    same = performance.from_edit({"mode": "whisper"}, first)
    assert same.revision == 1
    changed = performance.from_edit({"traits": ["urgent"]}, first)
    assert changed.revision == 2 and changed.mode == "whisper"   # mode survives


# -- generation ------------------------------------------------------------

def test_direction_reaches_the_engine_and_is_recorded_on_the_take(tmp_path):
    job = job_with(tmp_path)
    job.segments[0].delivery = "sound exhausted"
    engine = Engine()
    synthesize.run(job, engine, tmp_path / "work", voice_mode="preset",
                   engine="qwen", cast={"SPEAKER_00": {"voice": "voice-1"}})
    assert engine.requests[0]["instruct"] == "sound exhausted"
    take = job.segments[0].audio.selected()
    assert take.direction == "sound exhausted"
    assert take.intent.capability == "supported"


def test_an_undirectable_engine_gets_no_instruction_at_all(tmp_path):
    job = job_with(tmp_path)
    job.segments[0].delivery = "sound exhausted"
    engine = Engine()
    synthesize.run(job, engine, tmp_path / "work", voice_mode="preset",
                   engine="chatterbox", cast={"SPEAKER_00": {"voice": "voice-1"}})
    assert "instruct" not in engine.requests[0]
    assert job.segments[0].intent.unsupported


# -- candidates ------------------------------------------------------------

def generated(tmp_path, lines=1, **kwargs):
    job = job_with(tmp_path, lines)
    engine = Engine(**kwargs)
    synthesize.run(job, engine, tmp_path / "work", voice_mode="preset", engine="qwen",
                   cast={"SPEAKER_00": {"voice": "voice-1"}})
    return job, engine


def test_candidates_are_generated_beside_the_selection_never_over_it(tmp_path):
    job, engine = generated(tmp_path)
    seg = job.segments[0]
    chosen = seg.audio.selection.take_id
    before = len(engine.requests)

    synthesize.candidates(job, engine, tmp_path / "work", {"cue-0": 2},
                          cast={"SPEAKER_00": {"voice": "voice-1"}}, engine="qwen", seed=7)
    assert len(engine.requests) == before + 2
    assert len(seg.audio.takes) == 3
    assert seg.audio.selection.take_id == chosen        # untouched
    assert {t.origin for t in seg.audio.takes} == {"auto", "candidate"}
    assert all(t.checks["state"] == "usable"
               for t in seg.audio.takes if t.origin == "candidate")


def test_resuming_candidate_generation_repeats_no_request(tmp_path):
    job, engine = generated(tmp_path)
    synthesize.candidates(job, engine, tmp_path / "work", {"cue-0": 2},
                          cast={"SPEAKER_00": {"voice": "voice-1"}}, engine="qwen", seed=7)
    spent = len(engine.requests)
    synthesize.candidates(job, engine, tmp_path / "work", {"cue-0": 2},
                          cast={"SPEAKER_00": {"voice": "voice-1"}}, engine="qwen", seed=7)
    assert len(engine.requests) == spent
    assert job.metrics["candidate_reused"] == 2
    assert len(job.segments[0].audio.takes) == 3


def test_candidates_are_bounded_by_the_shared_budget(tmp_path):
    job, engine = generated(tmp_path)
    budget = RequestBudget(limit=1)
    synthesize.candidates(job, engine, tmp_path / "work", {"cue-0": 4}, budget=budget,
                          cast={"SPEAKER_00": {"voice": "voice-1"}}, engine="qwen", seed=7)
    assert budget.spent == 1 and budget.by_kind == {"candidate": 1}
    assert job.metrics["candidates_refused"] == 1
    assert len(job.segments[0].audio.takes) == 2


def test_a_failed_candidate_is_kept_as_evidence(tmp_path):
    job, engine = generated(tmp_path)
    engine.fail_on = {2}      # the first candidate request fails
    synthesize.candidates(job, engine, tmp_path / "work", {"cue-0": 2},
                          cast={"SPEAKER_00": {"voice": "voice-1"}}, engine="qwen", seed=7)
    failed = [t for t in job.segments[0].audio.takes if t.state == "failed"]
    assert len(failed) == 1 and "refused" in failed[0].error
    assert job.metrics["candidates_failed"] == 1
    assert job.segments[0].audio.selected().state == "generated"   # still usable


def test_choosing_a_saved_take_costs_no_generation(tmp_path):
    job, engine = generated(tmp_path)
    synthesize.candidates(job, engine, tmp_path / "work", {"cue-0": 1},
                          cast={"SPEAKER_00": {"voice": "voice-1"}}, engine="qwen", seed=7)
    seg = job.segments[0]
    candidate = next(t for t in seg.audio.takes if t.origin == "candidate")
    spent = len(engine.requests)

    apply_edits(job, {"0": {"cue": "cue-0", "take": candidate.take_id}})
    assert seg.audio.selection.take_id == candidate.take_id
    assert seg.audio.selection.reason == "review"
    assert not seg.audio.renders          # the other take's derivatives are gone

    synthesize.run(job, engine, tmp_path / "work", voice_mode="preset", engine="qwen",
                   cast={"SPEAKER_00": {"voice": "voice-1"}})
    assert len(engine.requests) == spent                    # no new TTS
    assert job.metrics["tts_selection_reused"] == 1
    assert seg.audio.selection.take_id == candidate.take_id  # and it stuck


def test_a_wording_change_retires_a_chosen_take_so_the_line_is_regenerated(tmp_path):
    job, engine = generated(tmp_path)
    synthesize.candidates(job, engine, tmp_path / "work", {"cue-0": 1},
                          cast={"SPEAKER_00": {"voice": "voice-1"}}, engine="qwen", seed=7)
    seg = job.segments[0]
    candidate = next(t for t in seg.audio.takes if t.origin == "candidate")
    apply_edits(job, {"0": {"cue": "cue-0", "take": candidate.take_id}})
    spent = len(engine.requests)

    apply_edits(job, {"0": {"cue": "cue-0", "text": "una linea distinta"}})
    assert seg.audio.selection.reason == "auto"
    synthesize.run(job, engine, tmp_path / "work", voice_mode="preset", engine="qwen",
                   cast={"SPEAKER_00": {"voice": "voice-1"}})
    assert len(engine.requests) == spent + 1     # the edit really produced audio


def test_selection_survives_a_save_and_reload(tmp_path):
    from doblarr.stages.common import load_script, save_script

    job, engine = generated(tmp_path)
    synthesize.candidates(job, engine, tmp_path / "work", {"cue-0": 1},
                          cast={"SPEAKER_00": {"voice": "voice-1"}}, engine="qwen", seed=7)
    seg = job.segments[0]
    candidate = next(t for t in seg.audio.takes if t.origin == "candidate")
    apply_edits(job, {"0": {"cue": "cue-0", "take": candidate.take_id}})
    save_script(job, tmp_path / "work")

    restored = job_with(tmp_path)
    restored.source_audio = job.source_audio
    assert load_script(restored, tmp_path / "work") is not None
    assert restored.segments[0].audio.selection.take_id == candidate.take_id
    assert restored.segments[0].audio.selection.reason == "review"


def test_selecting_a_take_that_is_gone_is_an_actionable_error(tmp_path):
    job, engine = generated(tmp_path)
    with pytest.raises(ValueError, match="no take"):
        apply_edits(job, {"0": {"cue": "cue-0", "take": "not-a-take"}})
    missing = job.segments[0].audio.selected()
    Path(missing.raw.path).unlink()
    with pytest.raises(ValueError, match="no audio on disk"):
        apply_edits(job, {"0": {"cue": "cue-0", "take": missing.take_id}})


def test_a_manual_gain_is_recorded_on_the_job_and_bounded(tmp_path):
    job = job_with(tmp_path)
    apply_edits(job, {"0": {"cue": "cue-0", "gain_db": -3.5}})
    assert job.manual_gains == {"cue-0": -3.5}
    apply_edits(job, {"0": {"cue": "cue-0", "gain_db": None}})
    assert job.manual_gains == {}
    with pytest.raises(ValueError, match="mistake"):
        apply_edits(job, {"0": {"cue": "cue-0", "gain_db": 40}})


def test_structured_intent_survives_a_review_edit(tmp_path):
    job = job_with(tmp_path)
    apply_edits(job, {"0": {"cue": "cue-0", "mode": "thought",
                            "traits": ["restrained"]}})
    intent = job.segments[0].intent
    assert intent.mode == "thought" and intent.traits == ["restrained"]
    assert intent.origin == "manual" and intent.revision == 1


def test_only_the_selected_take_is_retained_as_current_but_none_are_deleted(tmp_path):
    job, engine = generated(tmp_path)
    synthesize.candidates(job, engine, tmp_path / "work", {"cue-0": 2},
                          cast={"SPEAKER_00": {"voice": "voice-1"}}, engine="qwen", seed=7)
    seg = job.segments[0]
    assert len(seg.audio.takes) == 3
    for take in seg.audio.takes:
        assert take.raw.exists()      # every raw take is still on disk
    seg.audio.selection = Selection(take_id=seg.audio.takes[-1].take_id, reason="review")
    assert len(seg.audio.takes) == 3
