"""quality.voice_check — flag a take that drifted from its voice, keep the best attempt."""

from pathlib import Path

from doblarr import cues, voice_check
from doblarr.cues import RAW, Artifact, Selection, Take
from doblarr.models import DubJob, Segment, Speaker
from doblarr.stages import quality
from tests.test_audio_quality import wav


def _job(tmp_path: Path) -> DubJob:
    job = DubJob(tmp_path / "movie.mkv", "ja", "es")
    job.speakers = {"SPEAKER_00": Speaker("SPEAKER_00", reference_clip=tmp_path / "ref.wav")}
    first = wav(tmp_path / "take0.wav", 5000)
    seg = Segment(0, 0, 1, "Mina, ven", speaker="SPEAKER_00", audio_clip=first)
    seg.audio.takes.append(Take(take_id="t0", raw=Artifact(role=RAW, path=str(first))))
    seg.audio.selection = Selection(take_id="t0")
    job.segments = [seg]
    return job


def _similarity(monkeypatch, by_file: dict[str, float]):
    monkeypatch.setattr(voice_check.VoiceCheck, "similarity",
                        lambda self, speaker, take: by_file[Path(take).name])


def _regenerate(tmp_path):
    def regenerate(seg):
        n = len(seg.audio.takes)
        path = wav(tmp_path / f"take{n}.wav", 5000)
        seg.audio.takes.append(Take(take_id=f"t{n}", raw=Artifact(role=RAW, path=str(path))))
        seg.audio.selection = Selection(take_id=f"t{n}")
        seg.audio_clip = path
    return regenerate


def test_off_by_default_measures_nothing(tmp_path, monkeypatch):
    job = _job(tmp_path)
    monkeypatch.setattr(voice_check.VoiceCheck, "similarity",
                        lambda *a: (_ for _ in ()).throw(AssertionError("measured")))
    quality.run(job, normalize=False)
    assert "voice_drift" not in job.segments[0].issues


def test_drifted_take_is_retried_and_the_closer_one_kept(tmp_path, monkeypatch):
    job = _job(tmp_path)
    _similarity(monkeypatch, {"take0.wav": 0.1, "take1.wav": 0.55})
    quality.run(job, normalize=False, regenerate=_regenerate(tmp_path), voice_check=True)
    seg = job.segments[0]
    assert seg.audio.selection.take_id == "t1"
    assert "voice_drift" not in seg.issues
    assert seg.audio.take("t1").checks["voice_similarity"] == 0.55


def test_a_worse_retry_does_not_replace_the_better_take(tmp_path, monkeypatch):
    job = _job(tmp_path)
    # Both drift; the first is closer to the voice, so it stays.
    _similarity(monkeypatch, {"take0.wav": 0.2, "take0.t0.wav": 0.2, "take1.wav": 0.05})
    quality.run(job, normalize=False, regenerate=_regenerate(tmp_path), voice_check=True)
    seg = job.segments[0]
    assert seg.audio.selection.take_id == "t0"
    assert seg.audio.selection.reason == "best of attempts"
    assert Path(seg.audio_clip).name == "take0.t0.wav"
    assert "voice_drift" in seg.issues
    assert job.metrics["quality_best_kept"] == 1


def test_unknown_similarity_is_not_a_drift(tmp_path, monkeypatch):
    job = _job(tmp_path)
    monkeypatch.setattr(voice_check.VoiceCheck, "similarity", lambda *a: None)
    quality.run(job, normalize=False, voice_check=True)
    assert "voice_drift" not in job.segments[0].issues


def test_missing_reference_measures_as_unknown(tmp_path):
    check = voice_check.VoiceCheck({"SPEAKER_00": tmp_path / "missing.wav"})
    assert check.similarity("SPEAKER_00", tmp_path / "take.wav") is None
    assert not check.drifted(None) and check.drifted(0.1) and not check.drifted(0.4)


def test_a_take_far_longer_than_its_line_is_retried(tmp_path):
    job = DubJob(tmp_path / "movie.mkv", "ja", "es")
    first = wav(tmp_path / "take0.wav", 5000, 5)        # 5 s for a 1 s line
    seg = Segment(0, 0, 1, "Ya voy", audio_clip=first)
    seg.audio.takes.append(Take(take_id="t0", raw=Artifact(role=RAW, path=str(first))))
    seg.audio.selection = Selection(take_id="t0")
    job.segments = [seg]

    def regenerate(seg):
        path = wav(tmp_path / "take1.wav", 5000, 1)
        seg.audio.takes.append(Take(take_id="t1", raw=Artifact(role=RAW, path=str(path))))
        seg.audio.selection = Selection(take_id="t1")
        seg.audio_clip = path

    quality.run(job, normalize=False, regenerate=regenerate)
    assert seg.audio.selection.take_id == "t1"
    assert "unexpected_duration" not in seg.issues


def test_a_voice_that_scores_low_everywhere_is_not_drifting():
    check = voice_check.VoiceCheck({})
    # A borrowed voice: every take sits near 0.15 against this run's reference.
    check.calibrate([("borrowed", 0.14), ("borrowed", 0.16), ("borrowed", 0.15),
                     ("borrowed", 0.17), ("own", 0.5)])
    assert not check.drifted(0.13, "borrowed")
    assert check.drifted(-0.05, "borrowed")      # far below its own typical score
    assert check.drifted(0.2, "own")             # too few samples: the plain floor
    assert not check.drifted(0.3, None)


def test_a_finding_seen_again_on_new_audio_carries_the_new_evidence():
    seg = Segment(0, 0, 1, "Ya voy")
    cues.apply_findings(seg, "d/1", "a", [("timing_overflow", "timing", "warning", None,
                                              {"clip_seconds": 10.8})])
    cues.apply_findings(seg, "d/1", "b", [("timing_overflow", "timing", "warning", None,
                                              {"clip_seconds": 1.7})])
    (found,) = seg.findings
    assert found.evidence == {"clip_seconds": 1.7} and found.inputs == "b"


def test_the_kept_attempt_is_its_own_audio_when_retries_reuse_the_file(tmp_path, monkeypatch):
    job = DubJob(tmp_path / "movie.mkv", "ja", "es")
    job.speakers = {"SPEAKER_00": Speaker("SPEAKER_00", reference_clip=tmp_path / "ref.wav")}
    line = wav(tmp_path / "line_0000.wav", 5000, 1)
    seg = Segment(0, 0, 1, "Mina, ven", speaker="SPEAKER_00", audio_clip=line)
    seg.audio.takes.append(Take(take_id="first", raw=Artifact(role=RAW, path=str(line))))
    seg.audio.selection = Selection(take_id="first")
    job.segments = [seg]
    # Score by length: the first take (1 s) is close to the voice, retries (2 s) are not.
    def by_length(self, speaker, take):
        return 0.5 if quality.inspect_pcm(take)["duration"] < 1.5 else 0.05

    monkeypatch.setattr(voice_check.VoiceCheck, "similarity", by_length)

    def regenerate(seg):   # like synthesis: the same file name, new audio
        wav(line, 5000, 2)
        n = len(seg.audio.takes)
        seg.audio.takes.append(Take(take_id=f"retry{n}", raw=Artifact(role=RAW, path=str(line))))
        seg.audio.selection = Selection(take_id=f"retry{n}")
        seg.audio_clip = line

    quality.run(job, normalize=False, regenerate=regenerate, voice_check=True, max_retries=2)
    assert seg.audio.selection.take_id == "first"
    assert quality.inspect_pcm(Path(seg.audio_clip))["duration"] == 1
