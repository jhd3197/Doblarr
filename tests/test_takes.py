"""Raw takes, processed derivatives and identity across cold/warm/edit/resume."""

import os
import shutil
from pathlib import Path

import pytest

from doblarr.budget import RequestBudget
from doblarr.cues import CUE_SCHEMA_VERSION, FITTED, NORMALIZED, RAW, UNKNOWN, Artifact
from doblarr.models import DubJob, Segment, Speaker
from doblarr.stages import fit_timing, quality, synthesize
from doblarr.stages.common import load_script, save_script
from tests.test_audio_quality import wav


class Engine:
    """Counts generations so a processing-only change can be shown to cost none."""

    def __init__(self, amplitude=6000, duration=1.0):
        self.calls = []
        self.amplitude = amplitude
        self.duration = duration

    def synthesize_to_file(self, profile, text, lang, dest, **kwargs):
        self.calls.append(text)
        dest.parent.mkdir(parents=True, exist_ok=True)
        return wav(dest, self.amplitude, self.duration)


def _job(tmp_path: Path, mtime: float = 1000.0) -> DubJob:
    src = tmp_path / "movie.mkv"
    src.write_text("fake video", encoding="utf-8")
    os.utime(src, (mtime, mtime))
    job = DubJob(input_file=src, source_lang="ja", target_lang="es")
    job.source_audio = tmp_path / "source.wav"
    job.speakers = {"SPEAKER_00": Speaker("SPEAKER_00", voicebox_profile_id="voice")}
    job.segments = [Segment(0, 0.0, 2.0, "uno", text_translated="uno")]
    return job


def test_synthesis_registers_one_raw_take_and_reuses_it(tmp_path):
    job = _job(tmp_path)
    engine = Engine()
    work = tmp_path / "work"

    synthesize.run(job, engine, work)
    seg = job.segments[0]
    take = seg.audio.selected()
    assert take is not None and take.state == "generated"
    assert take.raw.role == RAW and Path(take.raw.path).is_file()
    assert take.text == "uno" and take.profile == "voice"
    assert seg.audio_clip == Path(take.raw.path)  # the compatibility projection
    first_take = take.take_id

    # Warm rerun: same identity, no new generation.
    synthesize.run(job, engine, work)
    assert engine.calls == ["uno"]
    assert job.segments[0].audio.selected().take_id == first_take
    assert job.segments[0].audio.selected().state == "reused"
    assert len(job.segments[0].audio.takes) == 1


def test_wording_edit_mints_a_new_take_and_keeps_the_old_raw(tmp_path):
    job = _job(tmp_path)
    engine = Engine()
    work = tmp_path / "work"
    synthesize.run(job, engine, work)
    seg = job.segments[0]
    original = seg.audio.selected()
    seg.audio.put_render(Artifact(role=NORMALIZED, path=str(tmp_path / "norm.wav")))

    seg.text_translated = "otra cosa"
    synthesize.run(job, engine, work)
    assert engine.calls == ["uno", "otra cosa"]
    assert len(seg.audio.takes) == 2
    assert seg.audio.selection.previous == original.take_id
    # The earlier raw generation survives; only derived audio is invalidated.
    assert seg.audio.take(original.take_id).raw.path == original.raw.path
    assert seg.audio.renders == []


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")
def test_processing_chain_keeps_raw_and_normalizes_from_it(tmp_path):
    job = _job(tmp_path)
    engine = Engine(duration=3.0)  # 3s of speech in a 2s slot: timing has work
    work = tmp_path / "work"
    synthesize.run(job, engine, work)
    seg = job.segments[0]
    raw_path = Path(seg.audio.raw().path)
    raw_bytes = raw_path.read_bytes()

    quality.run(job)
    normalized = seg.audio.render(NORMALIZED)
    assert normalized is not None and normalized.derived_from == RAW
    assert seg.audio_clip == Path(normalized.path)
    assert raw_path.read_bytes() == raw_bytes  # the raw take is never overwritten

    fit_timing.run(job, work)
    fitted = seg.audio.render(FITTED)
    assert fitted is not None and fitted.derived_from == NORMALIZED
    assert seg.audio_clip == Path(fitted.path)
    assert seg.audio.current().role == FITTED
    assert raw_path.read_bytes() == raw_bytes

    # A second processing pass reuses everything and asks for no new speech.
    quality.run(job)
    fit_timing.run(job, work)
    assert engine.calls == ["uno"]
    assert {a.role for a in seg.audio.renders} == {NORMALIZED, FITTED}


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")
def test_resume_restores_artifact_references_and_identity(tmp_path):
    job = _job(tmp_path)
    engine = Engine(duration=3.0)
    work = tmp_path / "work"
    synthesize.run(job, engine, work)
    quality.run(job)
    fit_timing.run(job, work)
    save_script(job, work)
    before = {
        "cue": job.segments[0].cue_id,
        "take": job.segments[0].audio.selected().take_id,
        "roles": {a.role: a.path for a in job.segments[0].audio.renders},
        "clip": job.segments[0].audio_clip,
    }

    resumed = _job(tmp_path)
    assert load_script(resumed, work)
    # Profile ids are re-resolved per run, exactly as the real pipeline does.
    resumed.speakers["SPEAKER_00"].voicebox_profile_id = "voice"
    seg = resumed.segments[0]
    assert seg.cue_id == before["cue"]
    assert seg.audio.selected().take_id == before["take"]
    assert {a.role: a.path for a in seg.audio.renders} == before["roles"]
    assert seg.audio_clip == before["clip"]  # projection survives a restart

    # Continuing the resumed job generates nothing new.
    synthesize.run(resumed, engine, work)
    assert engine.calls == ["uno"]
    assert resumed.segments[0].audio.selected().take_id == before["take"]


def test_quality_retry_and_timing_repair_share_one_budget(tmp_path):
    job = _job(tmp_path)
    job.segments = [
        Segment(0, 0, 1, "a", text_translated="a", audio_clip=wav(tmp_path / "a.wav")),
        Segment(1, 2, 3, "b", text_translated="b", audio_clip=wav(tmp_path / "b.wav")),
    ]
    retried = []

    def regenerate(seg):
        retried.append(seg.index)
        wav(seg.audio_clip, 5000)

    budget = RequestBudget(limit=1)
    quality.run(job, normalize=False, regenerate=regenerate, budget=budget)
    # Both clips are silent, but only one retry is affordable.
    assert retried == [0]
    assert job.metrics["quality_retries"] == 1
    assert job.metrics["quality_retries_refused"] == 1
    assert budget.exhausted
    assert "silence" in job.segments[1].issues


def test_findings_track_disposition_instead_of_vanishing(tmp_path):
    seg = Segment(0, 0, 1, "a", audio_clip=wav(tmp_path / "a.wav"))
    job = _job(tmp_path)
    job.segments = [seg]
    quality.run(job, normalize=False, max_retries=0)
    silence = next(f for f in seg.findings if f.code == "silence")
    assert silence.disposition == "open" and silence.kind == "technical"
    assert silence.detector == quality.DETECTOR and silence.inputs

    # A reviewer accepts it, then new audio arrives: acceptance is not reused.
    silence.disposition = "accepted"
    wav(seg.audio_clip, 6000)
    quality.run(job, normalize=False, max_retries=0)
    silence = next(f for f in seg.findings if f.code == "silence")
    assert silence.disposition == "obsolete"
    assert silence.history[-1]["reason"] == "no longer detected"


def test_review_snapshot_carries_cue_records_and_a_revision(tmp_path):
    from doblarr.review import snapshot_revision, write_review
    from doblarr.telemetry import RunReport

    job = _job(tmp_path)
    engine = Engine()
    synthesize.run(job, engine, tmp_path / "work")
    RunReport(job, tmp_path / "work")
    write_review(job, tmp_path / "work")
    import json
    data = json.loads(job.review_file.read_text(encoding="utf-8"))
    assert data["cue_schema"] == CUE_SCHEMA_VERSION
    assert data["revision"] == snapshot_revision(job)
    row = data["segments"][0]
    assert row["cue"]["cue_id"] == job.segments[0].cue_id
    assert row["cue"]["audio"]["takes"][0]["raw"]["role"] == RAW
    # The records live under "cue" only, never duplicated on the row.
    assert "audio" not in row and "source" not in row

    # A changed decision changes the revision, so a stale edit can be detected.
    job.segments[0].text_translated = "algo distinto"
    assert snapshot_revision(job) != data["revision"]


def test_unknown_role_is_a_real_state(tmp_path):
    seg = Segment(0, 0, 1, "a")
    seg.audio.put_render(Artifact(role=UNKNOWN, path=str(tmp_path / "x.wav"), proven=False))
    assert seg.audio.current().role == UNKNOWN
    assert seg.audio.raw() is None
