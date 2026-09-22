"""Export validation: open the delivered file and check what is really in it.

The designed-failure cases are the point of this file. A check that only ever
sees correct exports proves nothing, so each one here is fed a container that is
deliberately wrong in exactly one way — truncated, mis-tagged, shifted,
wrong-layout, missing a stream, silent where a line was placed — and has to
name that one thing.
"""

import json
import shutil
import wave

import pytest

from doblarr import delivery
from doblarr.ffmpeg import run_ffmpeg
from doblarr.models import DubJob, Segment

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")


def tone(path, seconds=3.0, hz=440.0, amplitude=0.4, rate=48000, channels=2,
         silent_after=None):
    import math
    import struct

    frames = []
    for n in range(int(rate * seconds)):
        t = n / rate
        loud = amplitude if (silent_after is None or t < silent_after) else 0.0
        value = loud * math.sin(2 * math.pi * hz * t)
        frames.append(struct.pack("<h", int(value * 32767)) * channels)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as out:
        out.setnchannels(channels)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(b"".join(frames))
    return path


def container(root, dub_wav, *, seconds=3.0, title="Spanish AI", language="spa",
              extra_audio=1, subtitle=False):
    """A tiny video with `extra_audio` original tracks plus the dub last."""
    original = tone(root / "original.wav", seconds=seconds, hz=220.0)
    out = root / "export.mkv"
    args = ["-y", "-f", "lavfi", "-i",
            f"color=c=black:s=64x64:d={seconds}:r=5"]
    for _ in range(extra_audio):
        args += ["-i", str(original)]
    args += ["-i", str(dub_wav)]
    if subtitle:
        srt = root / "subs.srt"
        # Spanning the whole clip on purpose: `-shortest` would otherwise cut
        # the container down to the subtitle and the fixture would be testing
        # its own bug rather than the export check.
        whole = f"00:00:{seconds:06.3f}".replace(".", ",")
        srt.write_text(f"1\n00:00:00,000 --> {whole}\nhello\n", encoding="utf-8")
        args += ["-i", str(srt)]
    args += ["-map", "0:v"]
    for i in range(extra_audio):
        args += ["-map", f"{i + 1}:a"]
    args += ["-map", f"{extra_audio + 1}:a"]
    if subtitle:
        args += ["-map", f"{extra_audio + 2}:s", "-c:s", "srt"]
    args += ["-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
             f"-metadata:s:a:{extra_audio}", f"language={language}",
             f"-metadata:s:a:{extra_audio}", f"title={title}",
             "-shortest", str(out)]
    run_ffmpeg(args)
    return out


def job_for(root, export, *, audio_index=1, title="Spanish AI", language="spa",
            lines=(), original_streams=None):
    job = DubJob(input_file=root / "source.mkv", source_lang="ja", target_lang="es")
    job.output_file = export
    job.artifacts_dir = root
    job.metrics["mux"] = {"audio_index": audio_index, "language": language,
                          "title": title, "codec": "aac"}
    if original_streams is not None:
        job.metrics["mux"]["original_streams"] = original_streams
    for index, (start, end) in enumerate(lines):
        seg = Segment(index=index, start=start, end=end, text_src=f"line {index}")
        seg.cue_id = f"cue-{index}"
        clip = tone(root / f"clip{index}.wav", seconds=max(0.2, end - start))
        seg.audio_clip = clip
        job.segments.append(seg)
    return job


def codes(report, severity=None):
    return sorted(f["code"] for f in report["findings"]
                  if severity is None or f["severity"] == severity)


# -- the profile ------------------------------------------------------------

def test_no_target_means_a_measurement_and_explicitly_not_a_pass():
    config = delivery.settings({})
    described = delivery.describe(config)
    assert described["target_lufs"] is None
    assert described["true_peak_db"] is None
    assert "no target is" in described["note"]
    assert described["meter"] == delivery.METER


def test_a_profile_id_changes_when_a_graded_value_changes():
    base = delivery.settings({})
    stricter = delivery.settings({"target_lufs": -18.0})
    assert delivery.profile_id(base) != delivery.profile_id(stricter)
    # the mode is not part of the identity: measuring and enforcing grade the
    # same numbers, they differ only in what they are allowed to stop
    enforcing = delivery.settings({"mode": "enforce"})
    assert delivery.profile_id(base) == delivery.profile_id(enforcing)


def test_an_unknown_mode_degrades_to_measuring(caplog):
    assert delivery.settings({"mode": "obliterate"})["mode"] == "measure"


def test_an_unset_target_is_not_a_target_of_zero():
    assert delivery.settings({"target_lufs": ""})["target_lufs"] is None
    assert delivery.settings({"target_lufs": None})["target_lufs"] is None
    assert delivery.settings({"target_lufs": 0})["target_lufs"] == 0.0


# -- identifying the added track -------------------------------------------

LAYOUT = {"streams": [
    {"codec_type": "video", "index": 0},
    {"codec_type": "audio", "index": 1, "tags": {"language": "jpn", "title": "Japanese"}},
    {"codec_type": "audio", "index": 2, "tags": {"language": "spa", "title": "Spanish AI"}},
]}


def test_the_dub_is_found_by_the_index_the_muxer_reported():
    row, how = delivery.find_dub(LAYOUT, {"audio_index": 1, "language": "spa",
                                          "title": "Spanish AI"})
    assert row["index"] == 2
    assert "expected language and title" in how


def test_a_mis_tagged_stream_is_a_finding_not_a_measurement():
    row, how = delivery.find_dub(LAYOUT, {"audio_index": 1, "language": "fra",
                                          "title": "French AI"})
    assert row is None
    assert "rather than fra/French AI" in how


def test_the_first_audio_stream_is_never_assumed_to_be_the_dub():
    only_original = {"streams": [
        {"codec_type": "audio", "index": 1, "tags": {"language": "jpn", "title": "JP"}}]}
    row, how = delivery.find_dub(only_original, {"language": "spa", "title": "Spanish AI"})
    assert row is None
    assert "not in this file" in how


def test_two_streams_with_the_same_tags_are_ambiguous_not_guessed():
    twice = {"streams": [
        {"codec_type": "audio", "index": 1, "tags": {"language": "spa", "title": "S"}},
        {"codec_type": "audio", "index": 2, "tags": {"language": "spa", "title": "S"}}]}
    row, how = delivery.find_dub(twice, {"language": "spa", "title": "S"})
    assert row is None
    assert "without ambiguity" in how


def test_a_file_with_no_audio_at_all_says_so():
    row, how = delivery.find_dub({"streams": [{"codec_type": "video", "index": 0}]}, {})
    assert row is None
    assert "no audio streams" in how


# -- a correct export -------------------------------------------------------

@needs_ffmpeg
def test_a_correct_export_passes_the_configured_structural_checks(tmp_path):
    dub = tone(tmp_path / "dub.wav", seconds=3.0)
    export = container(tmp_path, dub, subtitle=True)
    job = job_for(tmp_path, export, lines=[(0.2, 1.0), (1.4, 2.4)],
                  original_streams={"video": 1, "audio": 1, "subtitle": 1})
    report = delivery.validate(job, {}, work_dir=tmp_path)
    assert report["state"] == "passed", report["findings"]
    assert report["publishable"] is True
    assert codes(report, "failure") == []
    assert report["stream"]["language"] == "spa"
    assert report["loudness"]["lufs"] is not None
    assert report["loudness"]["true_peak_db"] is not None
    assert report["loudness"]["meter"] == delivery.METER
    assert all(row["state"] == "present" for row in report["placement"])
    assert job.delivery is report


@needs_ffmpeg
def test_loudness_with_no_target_is_reported_as_information(tmp_path):
    dub = tone(tmp_path / "dub.wav", seconds=2.0)
    job = job_for(tmp_path, container(tmp_path, dub, seconds=2.0),
                  lines=[(0.2, 1.0)])
    report = delivery.validate(job, {}, work_dir=tmp_path)
    measured = next(f for f in report["findings"] if f["code"] == "loudness_measured")
    assert measured["severity"] == "info"
    assert "not a pass" in measured["detail"]


@needs_ffmpeg
def test_a_configured_target_is_graded_and_a_drift_is_a_warning(tmp_path):
    dub = tone(tmp_path / "dub.wav", seconds=2.0, amplitude=0.05)
    job = job_for(tmp_path, container(tmp_path, dub, seconds=2.0), lines=[(0.2, 1.0)])
    report = delivery.validate(job, {"target_lufs": -14.0, "lufs_tolerance": 1.0},
                               work_dir=tmp_path)
    assert "loudness_off_target" in codes(report, "warning")
    assert report["state"] == "warned"
    assert report["publishable"] is True   # a warning never withholds anything


@needs_ffmpeg
def test_a_true_peak_ceiling_is_graded_against_a_named_meter(tmp_path):
    dub = tone(tmp_path / "dub.wav", seconds=2.0, amplitude=0.95)
    job = job_for(tmp_path, container(tmp_path, dub, seconds=2.0), lines=[(0.2, 1.0)])
    report = delivery.validate(job, {"true_peak_db": -20.0}, work_dir=tmp_path)
    over = next(f for f in report["findings"] if f["code"] == "true_peak_over")
    assert over["evidence"]["meter"] == delivery.METER


# -- designed failures ------------------------------------------------------

@needs_ffmpeg
def test_a_truncated_dub_is_caught(tmp_path):
    short = tone(tmp_path / "dub.wav", seconds=1.0)
    long_original = tone(tmp_path / "original.wav", seconds=5.0, hz=220.0)
    out = tmp_path / "export.mkv"
    run_ffmpeg(["-y", "-f", "lavfi", "-i", "color=c=black:s=64x64:d=5:r=5",
                "-i", str(long_original), "-i", str(short),
                "-map", "0:v", "-map", "1:a", "-map", "2:a",
                "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
                "-metadata:s:a:1", "language=spa",
                "-metadata:s:a:1", "title=Spanish AI", str(out)])
    job = job_for(tmp_path, out, lines=[(0.2, 0.9)])
    report = delivery.validate(job, {"duration_tolerance": 0.5}, work_dir=tmp_path)
    assert "duration_mismatch" in codes(report, "failure")
    assert report["state"] == "failed"


@needs_ffmpeg
def test_measuring_the_wrong_stream_is_refused_rather_than_done(tmp_path):
    dub = tone(tmp_path / "dub.wav", seconds=2.0)
    export = container(tmp_path, dub, seconds=2.0)
    # The muxer says the dub is audio stream 0; it is not, and stream 0 is a
    # perfectly measurable original track. Measuring it anyway would produce a
    # confident number about the wrong audio.
    job = job_for(tmp_path, export, audio_index=0, lines=[(0.2, 1.0)])
    report = delivery.validate(job, {}, work_dir=tmp_path)
    assert codes(report, "failure") == ["dub_stream_missing"]
    assert report["loudness"] == {}


@needs_ffmpeg
def test_a_channel_layout_mismatch_is_caught(tmp_path):
    mono = tone(tmp_path / "dub.wav", seconds=2.0, channels=1)
    job = job_for(tmp_path, container(tmp_path, mono, seconds=2.0), lines=[(0.2, 1.0)])
    report = delivery.validate(job, {"channels": 2}, work_dir=tmp_path)
    assert "channel_mismatch" in codes(report, "failure")


@needs_ffmpeg
def test_a_sample_rate_mismatch_is_caught(tmp_path):
    dub = tone(tmp_path / "dub.wav", seconds=2.0, rate=48000)
    job = job_for(tmp_path, container(tmp_path, dub, seconds=2.0), lines=[(0.2, 1.0)])
    report = delivery.validate(job, {"sample_rate": 44100}, work_dir=tmp_path)
    assert "sample_rate_mismatch" in codes(report, "failure")


@needs_ffmpeg
def test_a_line_that_is_silent_in_the_delivered_track_is_caught(tmp_path):
    # The dub goes quiet after one second; a line was scheduled at two.
    dub = tone(tmp_path / "dub.wav", seconds=3.0, silent_after=1.0)
    job = job_for(tmp_path, container(tmp_path, dub), lines=[(0.2, 0.9), (2.0, 2.8)])
    report = delivery.validate(job, {"placement_samples": 2}, work_dir=tmp_path)
    missing = [f for f in report["findings"] if f["code"] == "cue_missing_in_export"]
    assert [f["evidence"]["line"] for f in missing] == [1]
    assert report["state"] == "failed"


@needs_ffmpeg
def test_intentional_silence_outside_a_placed_line_is_not_a_defect(tmp_path):
    # Two thirds of this export is silence and nothing was scheduled there.
    dub = tone(tmp_path / "dub.wav", seconds=3.0, silent_after=1.0)
    job = job_for(tmp_path, container(tmp_path, dub), lines=[(0.1, 0.9)])
    report = delivery.validate(job, {}, work_dir=tmp_path)
    assert codes(report, "failure") == []


@needs_ffmpeg
def test_a_dropped_original_stream_is_caught(tmp_path):
    dub = tone(tmp_path / "dub.wav", seconds=2.0)
    # The source had two audio tracks and a subtitle; this export kept one.
    export = container(tmp_path, dub, seconds=2.0, extra_audio=1)
    job = job_for(tmp_path, export, lines=[(0.2, 1.0)],
                  original_streams={"video": 1, "audio": 2, "subtitle": 1})
    report = delivery.validate(job, {}, work_dir=tmp_path)
    assert "original_streams_lost" in codes(report, "failure")


@needs_ffmpeg
def test_an_unreadable_container_fails_without_pretending_to_measure(tmp_path):
    broken = tmp_path / "export.mkv"
    broken.write_bytes(b"not a container")
    job = job_for(tmp_path, broken)
    report = delivery.validate(job, {}, work_dir=tmp_path)
    assert codes(report, "failure") == ["export_unreadable"]
    assert report["loudness"] == {}


def test_a_missing_export_is_unavailable_not_failed(tmp_path):
    job = job_for(tmp_path, tmp_path / "nothing.mkv")
    report = delivery.validate(job, {}, work_dir=tmp_path)
    assert report["state"] == "unavailable"
    assert report["findings"] == []


def test_validation_off_reports_nothing_and_blocks_nothing(tmp_path):
    job = job_for(tmp_path, tmp_path / "nothing.mkv")
    report = delivery.validate(job, {"mode": "off"}, work_dir=tmp_path)
    assert report["state"] == "skipped"
    assert "off for this run" in report["reason"]


@needs_ffmpeg
def test_enforce_withholds_publication_and_measure_does_not(tmp_path):
    dub = tone(tmp_path / "dub.wav", seconds=2.0)
    export = container(tmp_path, dub, seconds=2.0)
    job = job_for(tmp_path, export, audio_index=0, lines=[(0.2, 1.0)])
    measuring = delivery.validate(job, {"mode": "measure"}, work_dir=tmp_path)
    assert measuring["state"] == "failed"
    assert measuring["publishable"] is True
    enforcing = delivery.validate(job, {"mode": "enforce"}, work_dir=tmp_path)
    assert enforcing["state"] == "failed"
    assert enforcing["publishable"] is False


# -- revision comparison ----------------------------------------------------

def _manifest(version, cues, settings=None, output="sha-a"):
    return {"version_id": version, "output_sha256": output,
            "settings": settings or {}, "cues": cues}


def _cue(cue_id, *, take="take-1", renders=None, takes=None, treatment=None, index=0):
    return {
        "cue_id": cue_id, "index": index,
        "audio": {"selection": {"take_id": take},
                  "takes": takes or [{"take_id": take, "fingerprint": "f1"}],
                  "renders": renders or [{"role": "fitted", "fingerprint": "r1"}]},
        "treatment": treatment or {},
        "placement": {"onset": 1.0},
    }


def test_a_new_take_is_classified_as_generation():
    before = _manifest("v1", [_cue("a")])
    after = _manifest("v2", [_cue("a", take="take-2",
                                  takes=[{"take_id": "take-1", "fingerprint": "f1"},
                                         {"take_id": "take-2", "fingerprint": "f2"}])],
                      output="sha-b")
    result = delivery.compare(before, after)
    assert [row["cue"] for row in result["regenerated"]] == ["a"]
    assert result["reprocessed"] == []


def test_a_changed_render_is_classified_as_processing():
    before = _manifest("v1", [_cue("a")])
    after = _manifest("v2", [_cue("a", renders=[{"role": "fitted", "fingerprint": "r2"}])],
                      output="sha-b")
    result = delivery.compare(before, after)
    assert [row["cue"] for row in result["reprocessed"]] == ["a"]
    assert result["regenerated"] == []


def test_a_treatment_change_is_processing_and_names_its_tail():
    before = _manifest("v1", [_cue("a", treatment={"preset": "dry", "outcome": "bypassed"})])
    after = _manifest("v2", [_cue("a", treatment={"preset": "room", "outcome": "applied",
                                                  "tail": 0.072})], output="sha-b")
    result = delivery.compare(before, after)
    assert result["reprocessed"][0]["treatment"]["preset"] == ["dry", "room"]
    window = result["expected_changed_windows"][0]
    assert window["tail"] == 0.072
    assert "rings out" in window["note"]


def test_an_identical_output_is_reported_as_identical():
    before = _manifest("v1", [_cue("a")])
    after = _manifest("v1", [_cue("a")])
    assert delivery.compare(before, after)["identical_output"] is True


def test_added_and_removed_cues_are_separated():
    before = _manifest("v1", [_cue("a"), _cue("b", index=1)])
    after = _manifest("v2", [_cue("a"), _cue("c", index=1)], output="sha-b")
    result = delivery.compare(before, after)
    assert [row["cue"] for row in result["added"]] == ["c"]
    assert result["removed"] == ["b"]


def test_a_settings_difference_is_named_without_dumping_both_configs():
    before = _manifest("v1", [_cue("a")], settings={"timing": {"mode": "whole"},
                                                    "levels": {"mode": "legacy"}})
    after = _manifest("v2", [_cue("a")], settings={"timing": {"mode": "phrase"},
                                                   "levels": {"mode": "legacy"}},
                      output="sha-b")
    result = delivery.compare(before, after)
    assert set(result["settings"]) == {"timing"}
    assert result["settings"]["timing"]["current"] == {"mode": "phrase"}


@needs_ffmpeg
def test_the_report_survives_a_json_round_trip(tmp_path):
    """A report is stored in a snapshot and a manifest; it has to serialize."""
    dub = tone(tmp_path / "dub.wav", seconds=2.0)
    job = job_for(tmp_path, container(tmp_path, dub, seconds=2.0), lines=[(0.2, 1.0)])
    report = delivery.validate(job, {}, work_dir=tmp_path)
    assert json.loads(json.dumps(report)) == report
