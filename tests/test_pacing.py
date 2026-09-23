"""Pace per character: measured on synthetic takes (tone bursts and silence)."""

import math
import os
import random
import struct
import wave
from pathlib import Path

import pytest

from doblarr import pacing
from doblarr.cues import ensure_identity
from doblarr.models import DubJob, Segment
from doblarr.stages import fit_timing

RATE = 16000


def write_take(path: Path, blocks) -> Path:
    """A mono 16-bit take from (seconds, amplitude) blocks over a faint hiss."""
    noise = random.Random(7)
    frames = bytearray()
    phase = 0
    for seconds, amplitude in blocks:
        for _ in range(int(RATE * seconds)):
            value = amplitude * math.sin(2 * math.pi * 220 * phase / RATE)
            value += noise.uniform(-0.001, 0.001)
            frames += struct.pack("<h", int(max(-1.0, min(1.0, value)) * 32000))
            phase += 1
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as out:
        out.setparams((1, 2, RATE, 0, "NONE", "not compressed"))
        out.writeframes(bytes(frames))
    return path


def test_text_units_ignore_spaces_and_punctuation():
    assert pacing.text_units("¡Hola, mundo!") == 9
    assert pacing.text_units("えっ？ 本当に…") == 5
    assert pacing.text_units("") == 0


def test_voiced_seconds_leave_out_lead_tail_and_pauses(tmp_path):
    take = write_take(tmp_path / "a.wav",
                      [(0.3, 0.0), (1.0, 0.5), (0.4, 0.0), (0.6, 0.5), (0.5, 0.0)])
    voiced = pacing.voiced_seconds(take)
    assert voiced == pytest.approx(1.6, abs=0.05)


def test_silent_or_unreadable_takes_are_unknown(tmp_path):
    assert pacing.voiced_seconds(write_take(tmp_path / "s.wav", [(1.0, 0.0)])) is None
    bad = tmp_path / "bad.wav"
    bad.write_text("not audio", encoding="utf-8")
    assert pacing.voiced_seconds(bad) is None


def test_rates_from_units_voiced_time_and_factor(tmp_path):
    seg = Segment(0, 0.0, 3.0, "src", text_translated="abcdefghij")  # 10 units
    take = write_take(tmp_path / "t.wav", [(0.2, 0.0), (2.0, 0.5), (0.2, 0.0)])
    line = pacing.measure(seg, take, factor=1.25)
    assert line.natural_rate == pytest.approx(5.0, rel=0.05)
    assert line.effective_rate == pytest.approx(6.25, rel=0.05)


def test_groups_split_a_speaker_at_a_long_silence():
    segs = [Segment(0, 0.0, 2.0, "a", speaker="A"), Segment(1, 2.5, 4.0, "b", speaker="B"),
            Segment(2, 5.0, 6.0, "c", speaker="A"), Segment(3, 20.0, 21.0, "d", speaker="A")]
    grouped = pacing.groups(segs, scene_gap=8.0)
    assert {k: [s.index for s in v] for k, v in grouped.items()} == {
        "A#0": [0, 2], "A#1": [3], "B#0": [1]}


def _paced_job(tmp_path, seconds):
    """One speaker, same text length per line; `seconds` of voiced speech each."""
    job = DubJob(input_file=tmp_path / "m.mkv", source_lang="ja", target_lang="es")
    lines = []
    for i, voiced in enumerate(seconds):
        seg = Segment(i, i * 4.0, i * 4.0 + 3.0, "src", text_translated="x" * 20)
        job.segments.append(seg)
        take = write_take(tmp_path / f"{i}.wav", [(0.1, 0.0), (voiced, 0.5), (0.1, 0.0)])
        lines.append((seg, take))
    ensure_identity(job)
    return job, lines


def test_a_line_off_its_characters_pace_is_an_info_finding(tmp_path):
    job, rows = _paced_job(tmp_path, [2.0, 2.0, 2.0, 1.4])  # the last is 43% faster
    lines = [pacing.measure(seg, take) for seg, take in rows]
    pacing.record(job, lines, {"pace_tolerance": 0.18})
    flagged = [s.index for s in job.segments
               for f in s.findings if f.code == "pace_jump" and f.disposition == "open"]
    assert flagged == [3]
    finding = next(f for f in job.segments[3].findings if f.code == "pace_jump")
    assert finding.severity == "info" and finding.kind == "timing"
    assert finding.evidence["direction"] == "fast"
    summary = job.metrics["pacing"]
    assert summary["pace_jumps"] == 1 and summary["lines_measured"] == 4
    group = summary["groups"]["SPEAKER_00#0"]
    assert group["lines"] == 4 and group["max_jump"] > 0.3
    row = summary["lines"][job.segments[3].cue_id]
    assert row["group"] == "SPEAKER_00#0" and row["deviation"] > 0.18


def test_a_line_that_now_keeps_pace_retires_its_finding(tmp_path):
    job, rows = _paced_job(tmp_path, [2.0, 2.0, 2.0, 1.4])
    pacing.record(job, [pacing.measure(seg, take) for seg, take in rows])
    lines = [pacing.measure(seg, take) for seg, take in rows]
    lines[3].voiced = 2.0  # a new take at the group's pace
    pacing.record(job, lines)
    finding = next(f for f in job.segments[3].findings if f.code == "pace_jump")
    assert finding.disposition == "obsolete"


def test_fit_timing_records_pace_without_changing_audio_when_off(tmp_path, monkeypatch):
    job, rows = _paced_job(tmp_path, [2.0, 2.0, 1.4])
    for seg, take in rows:
        seg.audio_clip = take
        os.utime(take, (2000, 2000))
    calls = []
    monkeypatch.setattr(fit_timing, "run_ffmpeg", lambda args, **k: calls.append(args))
    fit_timing.run(job, tmp_path / "work", options={"pacing": "off"})
    assert calls == []  # every take fits its 3s slot: nothing is rendered
    assert job.metrics["pacing"]["pace_jumps"] == 1
    assert job.metrics["pacing"]["mode"] == "off"


def _fit_paced(tmp_path, monkeypatch, voiced, options):
    job, rows = _paced_job(tmp_path, voiced)
    for seg, take in rows:
        seg.audio_clip = take
        os.utime(take, (2000, 2000))
    rendered = {}

    def render(args, **kwargs):
        rendered[Path(args[2]).stem] = args[args.index("-af") + 1]
        Path(args[-1]).write_bytes(b"audio")

    monkeypatch.setattr(fit_timing, "run_ffmpeg", render)
    fit_timing.run(job, tmp_path / "work", options=options)
    return job, rendered


def test_a_slow_take_is_sped_toward_its_character_capped(tmp_path, monkeypatch):
    # Same text, 3 s slots; the last take is ~30% slower than the other two.
    job, rendered = _fit_paced(tmp_path, monkeypatch, [2.0, 2.0, 2.6],
                               {"pacing": "speaker", "pace_local_range": 0.3})
    assert rendered == {"2": "atempo=1.1500"}      # pace_max_speedup, not the full 1.3
    assert not any("timing_overflow" in s.issues for s in job.segments)


def test_the_slow_take_speedup_also_respects_the_local_range(tmp_path, monkeypatch):
    _job, rendered = _fit_paced(tmp_path, monkeypatch, [2.0, 2.0, 2.6], {"pacing": "speaker"})
    assert rendered == {"2": "atempo=1.1000"}      # base 1.0 + pace_local_range 0.10


def test_with_pacing_off_a_slow_take_is_left_slow(tmp_path, monkeypatch):
    job, rendered = _fit_paced(tmp_path, monkeypatch, [2.0, 2.0, 2.6], {"pacing": "off"})
    assert rendered == {}
    assert job.metrics["pacing"]["pace_jumps"] == 1  # still reported
