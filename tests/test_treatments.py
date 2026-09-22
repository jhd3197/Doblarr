"""Acoustic space and device treatments: the decision, the DSP, and the tail.

The unit tests below fix the *decision* — precedence, capability, bounds — and
the FFmpeg tests fix the *audio*: real filters on a real impulse, so latency,
tail length and channel layout are measured rather than asserted from the
filter names. Neither kind establishes that a treated line sounds like a room;
that is a listening question and it is deliberately not answered here.
"""

import shutil
import wave

import pytest

from doblarr import treatments
from doblarr.cues import TREATED, Artifact, SchemaError, Treatment, validate_cues
from doblarr.models import Segment
from doblarr.stages import treatment as stage

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")


def cue(index=0, start=0.0, end=2.0, cue_id=None):
    seg = Segment(index=index, start=start, end=end, text_src=f"line {index}")
    seg.cue_id = cue_id or f"cue-{index}"
    return seg


def tone(path, seconds=1.0, hz=220.0, amplitude=0.4, channels=2, rate=48000,
         lead=0.0):
    """A short tone with an optional silent lead-in, as 16-bit PCM."""
    import math
    import struct

    frames = []
    for n in range(int(rate * (seconds + lead))):
        t = n / rate
        value = 0.0 if t < lead else amplitude * math.sin(2 * math.pi * hz * (t - lead))
        sample = struct.pack("<h", int(max(-1.0, min(1.0, value)) * 32767))
        frames.append(sample * channels)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as out:
        out.setnchannels(channels)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(b"".join(frames))
    return path


def impulse(path, seconds=0.6, rate=48000, channels=1, at=0.05):
    """One loud sample in silence: the only honest way to measure a tail."""
    import struct

    total = int(rate * seconds)
    spike = int(rate * at)
    frames = []
    for n in range(total):
        value = 32000 if n == spike else 0
        frames.append(struct.pack("<h", value) * channels)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as out:
        out.setnchannels(channels)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(b"".join(frames))
    return path


def peaks(path):
    """Per-sample absolute amplitude of channel 0, 0..1."""
    import array
    import sys

    with wave.open(str(path), "rb") as audio:
        channels = audio.getnchannels()
        rate = audio.getframerate()
        samples = array.array("h", audio.readframes(audio.getnframes()))
        if sys.byteorder != "little":
            samples.byteswap()
    return [abs(samples[i]) / 32768 for i in range(0, len(samples), channels)], rate


# -- the catalogue and what this build can do -------------------------------

def test_every_named_preset_has_a_spec_and_a_summary():
    for name in treatments.TREATMENT_PRESETS:
        spec = treatments.PRESETS[name]
        assert spec.summary
        assert spec.name == name


def test_dry_is_always_supported_and_renders_nothing():
    state, missing = treatments.capability("dry")
    assert (state, missing) == ("supported", [])
    assert treatments.chain("dry", 1.0) == []
    # even with a makeup gain: dry means dry, not "dry plus a volume filter"
    assert treatments.chain("dry", 1.0, -3.0) == []


def test_a_missing_filter_makes_a_preset_unsupported(monkeypatch):
    monkeypatch.setattr(treatments, "available_filters", lambda refresh=False: {"volume"})
    state, missing = treatments.capability("phone")
    assert state == "unsupported"
    assert missing == ["acompressor", "highpass", "lowpass"]


def test_an_unreadable_filter_list_is_unknown_not_supported(monkeypatch):
    monkeypatch.setattr(treatments, "available_filters", lambda refresh=False: set())
    state, missing = treatments.capability("room")
    assert state == "unknown"
    assert missing == ["aecho"]


def test_an_unsupported_preset_is_recorded_as_asked_for_not_applied(monkeypatch):
    monkeypatch.setattr(treatments, "available_filters", lambda refresh=False: {"volume"})
    config = treatments.settings({"mode": "on", "default": "phone"})
    decision = treatments.decide(cue(), config)
    assert decision.preset == "phone"          # what was asked for survives
    assert decision.outcome == "unsupported"   # and it is not called applied
    assert decision.applied is False
    assert decision.tail == 0.0
    assert "highpass" in decision.reason


# -- choosing a preset ------------------------------------------------------

def test_precedence_is_line_then_scene_then_default():
    config = treatments.settings({
        "mode": "on", "default": "room",
        "scenes": [{"start": 10.0, "end": 20.0, "preset": "phone"}],
        "lines": {"cue-2": {"preset": "radio"}},
    })
    assert treatments.choose(cue(0, 1.0, 2.0), config)["preset"] == "room"
    assert treatments.choose(cue(1, 12.0, 13.0), config)["preset"] == "phone"
    inside = cue(2, 12.0, 13.0)
    assert treatments.choose(inside, config)["preset"] == "radio"


def test_a_reviewer_bypass_beats_a_scene_rule():
    config = treatments.settings({
        "mode": "on", "default": "room",
        "scenes": [{"start": 0.0, "end": 60.0, "preset": "phone"}],
    })
    chosen = treatments.choose(cue(0, 5.0, 6.0), config, {"cue-0": {"bypass": True}})
    assert chosen["preset"] == "dry"
    assert chosen["origin"] == "line"


def test_an_unknown_requested_preset_falls_back_to_dry_and_says_so():
    config = treatments.settings({"mode": "on"})
    chosen = treatments.choose(cue(0), config, {"cue-0": {"preset": "cathedral"}})
    assert chosen["preset"] == "dry"
    assert "cathedral" in chosen["reason"]


def test_a_line_belongs_to_the_scene_its_start_is_in():
    config = treatments.settings({
        "mode": "on",
        "scenes": [{"start": 0.0, "end": 10.0, "preset": "phone"}]})
    straddling = cue(0, 9.0, 12.0)
    assert treatments.choose(straddling, config)["preset"] == "phone"
    assert treatments.straddles(straddling, config)["preset"] == "phone"
    assert treatments.straddles(cue(1, 1.0, 2.0), config) is None


def test_an_unusable_scene_rule_is_dropped_not_guessed():
    config = treatments.settings({"mode": "on", "scenes": [
        {"start": 5.0, "end": 1.0, "preset": "room"},        # backwards
        {"start": 0.0, "end": 1.0, "preset": "cathedral"},   # unknown
        {"start": "x", "end": 2.0, "preset": "room"},        # not a number
        {"start": 2.0, "end": 4.0, "preset": "room"},        # usable
    ]})
    assert [r["preset"] for r in config["scenes"]] == ["room"]
    assert config["scenes"][0]["start"] == 2.0


def test_intensity_interpolates_between_the_two_parameter_sets():
    spec = treatments.PRESETS["phone"]
    assert spec.at(0.0)["low"] == spec.gentle["low"]
    assert spec.at(1.0)["low"] == spec.full["low"]
    middle = spec.at(0.5)["low"]
    assert spec.gentle["low"] < middle < spec.full["low"]


def test_a_tail_only_exists_for_a_time_based_preset():
    assert treatments.tail_seconds("phone") == 0.0
    assert treatments.tail_seconds("radio") == 0.0
    assert treatments.tail_seconds("room") > 0
    assert treatments.tail_seconds("distant") > treatments.tail_seconds("room")


def test_a_tail_is_the_sum_of_the_echo_stages_and_nothing_else():
    """The recorded tail is arithmetic on the chain, not an estimate."""
    for name in ("room", "distant"):
        spec = treatments.PRESETS[name]
        assert spec.tail == pytest.approx(
            sum(max(stage) for stage in spec.echoes) / 1000, abs=1e-6)


def test_a_tail_does_not_move_with_the_intensity_dial():
    # Intensity changes how loud the reflections are, not how big the room is.
    assert treatments.tail_seconds("room", 0.0) == treatments.tail_seconds("room", 1.0)


# -- the makeup gain is bounded and never invented ---------------------------

def test_makeup_is_skipped_inside_the_deadband():
    gain, why = treatments.makeup_for(-20.0, -20.2, 0.0)
    assert gain == 0.0
    assert "intended" in why


def test_makeup_follows_the_presets_intended_offset():
    # distant means -3.5 dB; an effect that left the line at -20 needs -3.5.
    gain, _why = treatments.makeup_for(-20.0, -20.0, -3.5)
    assert gain == pytest.approx(-3.5, abs=0.01)


def test_makeup_is_clamped_and_the_clamp_is_reported():
    gain, why = treatments.makeup_for(-10.0, -30.0, 0.0)
    assert gain == treatments.MAX_MAKEUP_DB
    assert "clamped" in why


def test_an_unmeasurable_line_gets_no_makeup_at_all():
    gain, why = treatments.makeup_for(None, -20.0, 0.0)
    assert gain == 0.0
    assert "could not be measured" in why


# -- the record ------------------------------------------------------------

def test_a_treated_render_without_an_applied_treatment_is_rejected():
    seg = cue()
    seg.audio.put_render(Artifact(role=TREATED, path="x.wav", fingerprint="f"))
    seg.treatment = Treatment(preset="room", outcome="bypassed")
    with pytest.raises(SchemaError, match="treated render"):
        validate_cues([seg])
    seg.treatment = Treatment(preset="room", outcome="applied")
    validate_cues([seg])


def test_an_unknown_preset_or_outcome_cannot_be_persisted():
    with pytest.raises(SchemaError):
        Treatment(preset="cathedral")
    with pytest.raises(SchemaError):
        Treatment(outcome="probably")


def test_a_treatment_round_trips_through_its_codec():
    original = Treatment(preset="distant", intensity=0.4, origin="scene",
                         outcome="applied", tail=0.3, makeup=-1.25, offset=-1.4,
                         filters=["lowpass=f=6000"], scene="1-2:distant")
    restored = Treatment.from_dict(original.as_dict())
    assert restored.as_dict() == original.as_dict()


def test_an_older_payload_has_no_treatment_and_that_is_its_honest_state():
    assert Treatment.from_dict({}).outcome == "unknown"
    assert Treatment.from_dict({}).applied is False


# -- real DSP ---------------------------------------------------------------

@needs_ffmpeg
@pytest.mark.parametrize("preset", ["room", "distant", "phone", "radio"])
def test_a_preset_renders_and_keeps_the_onset_where_it_was(tmp_path, preset):
    """Filter latency, measured rather than assumed.

    Every filter in the catalogue is IIR or a delay line, so the first sample
    of the dry signal must still be the first sample of the treated one. A
    preset that quietly added pre-delay would shift every line against the
    picture, and the record claims a latency of zero.
    """
    from doblarr.ffmpeg import run_ffmpeg

    source = impulse(tmp_path / "in.wav")
    dest = tmp_path / f"{preset}.wav"
    run_ffmpeg(["-y", "-i", str(source), "-af",
                ",".join(treatments.chain(preset, 1.0)),
                "-ar", "48000", "-ac", "1", "-c:a", "pcm_s16le", str(dest)])
    dry_peaks, rate = peaks(source)
    wet_peaks, _rate = peaks(dest)
    onset = next(i for i, v in enumerate(dry_peaks) if v > 0.2)
    wet_onset = next(i for i, v in enumerate(wet_peaks) if v > 0.02)
    # within one millisecond: a band-limited impulse rings a little early
    assert abs(wet_onset - onset) <= rate // 1000


@needs_ffmpeg
@pytest.mark.parametrize("preset", ["room", "distant"])
def test_a_time_based_preset_rings_for_the_tail_it_declared(tmp_path, preset):
    from doblarr.ffmpeg import run_ffmpeg

    source = impulse(tmp_path / "in.wav", seconds=0.5, at=0.02)
    dest = tmp_path / f"{preset}.wav"
    run_ffmpeg(["-y", "-i", str(source), "-af",
                ",".join(treatments.chain(preset, 1.0)),
                "-ar", "48000", "-ac", "1", "-c:a", "pcm_s16le", str(dest)])
    declared = treatments.tail_seconds(preset)
    dry_peaks, rate = peaks(source)
    wet_peaks, _ = peaks(dest)
    # The rendered length is the dry length plus exactly the declared tail.
    # This is the property the mix and the conversation check both depend on:
    # if it drifted, a reverb would start being reported as an interruption.
    assert len(wet_peaks) / rate == pytest.approx(
        len(dry_peaks) / rate + declared, abs=0.005)
    # and the reflections really are audible after the impulse
    spike = max(i for i, v in enumerate(dry_peaks) if v > 0.2)
    after = wet_peaks[spike + rate // 100:]
    assert max(after, default=0.0) > 0.001


@needs_ffmpeg
def test_a_phone_preset_adds_no_tail(tmp_path):
    from doblarr.ffmpeg import run_ffmpeg

    source = impulse(tmp_path / "in.wav", seconds=0.4)
    dest = tmp_path / "phone.wav"
    run_ffmpeg(["-y", "-i", str(source), "-af",
                ",".join(treatments.chain("phone", 1.0)),
                "-ar", "48000", "-ac", "1", "-c:a", "pcm_s16le", str(dest)])
    dry_peaks, rate = peaks(source)
    wet_peaks, _ = peaks(dest)
    assert len(wet_peaks) == pytest.approx(len(dry_peaks), abs=rate // 100)


@needs_ffmpeg
def test_the_stage_preserves_the_channel_layout(tmp_path):
    seg = cue()
    source = tone(tmp_path / "line.wav", seconds=1.0, channels=2)
    seg.audio.put_render(Artifact(role="normalized", path=str(source),
                                  fingerprint="dry"))
    job = _job(tmp_path, [seg])
    stage.run(job, {"mode": "on", "default": "room"}, work_dir=tmp_path)
    treated = seg.audio.render(TREATED)
    assert treated is not None
    with wave.open(treated.path, "rb") as audio:
        assert audio.getnchannels() == 2
        assert audio.getframerate() == 48000


@needs_ffmpeg
def test_the_effect_does_not_become_a_level_decision(tmp_path):
    """A room raises the measured level; the makeup gain puts it back."""
    from doblarr.levels import analyze

    seg = cue()
    source = tone(tmp_path / "line.wav", seconds=1.2, channels=1)
    seg.audio.put_render(Artifact(role="normalized", path=str(source), fingerprint="dry"))
    job = _job(tmp_path, [seg])
    stage.run(job, {"mode": "on", "default": "room"}, work_dir=tmp_path)
    assert seg.treatment.applied
    dry_db = analyze(source)["speech_db"]
    wet_db = analyze(seg.audio.render(TREATED).path)["speech_db"]
    assert abs(wet_db - dry_db) <= treatments.LEVEL_DEADBAND_DB + 0.4


@needs_ffmpeg
def test_distance_is_actually_quieter(tmp_path):
    from doblarr.levels import analyze

    seg = cue()
    source = tone(tmp_path / "line.wav", seconds=1.2, channels=1)
    seg.audio.put_render(Artifact(role="normalized", path=str(source), fingerprint="dry"))
    job = _job(tmp_path, [seg])
    stage.run(job, {"mode": "on", "default": "distant"}, work_dir=tmp_path)
    dry_db = analyze(source)["speech_db"]
    wet_db = analyze(seg.audio.render(TREATED).path)["speech_db"]
    assert wet_db < dry_db - 2.0        # the preset intends -3.5 dB
    assert seg.treatment.offset == pytest.approx(-3.5, abs=0.01)


@needs_ffmpeg
def test_turning_treatment_off_gives_the_dry_line_back(tmp_path):
    seg = cue()
    source = tone(tmp_path / "line.wav", seconds=1.0, channels=1)
    seg.audio.put_render(Artifact(role="normalized", path=str(source), fingerprint="dry"))
    job = _job(tmp_path, [seg])
    stage.run(job, {"mode": "on", "default": "room"}, work_dir=tmp_path)
    assert seg.audio.render(TREATED) is not None
    stage.run(job, {"mode": "off"}, work_dir=tmp_path)
    assert seg.audio.render(TREATED) is None
    assert seg.audio_clip == source
    assert seg.treatment.outcome == "bypassed"


@needs_ffmpeg
def test_changing_preset_reprocesses_the_dry_line_and_never_stacks(tmp_path):
    from doblarr.levels import analyze

    seg = cue()
    source = tone(tmp_path / "line.wav", seconds=1.2, channels=1)
    seg.audio.put_render(Artifact(role="normalized", path=str(source), fingerprint="dry"))
    job = _job(tmp_path, [seg])
    stage.run(job, {"mode": "on", "default": "distant"}, work_dir=tmp_path)
    once = analyze(seg.audio.render(TREATED).path)["speech_db"]
    stage.run(job, {"mode": "on", "default": "distant"}, work_dir=tmp_path)
    twice = analyze(seg.audio.render(TREATED).path)["speech_db"]
    assert once == pytest.approx(twice, abs=0.05)
    assert seg.audio.render(TREATED).derived_from == "normalized"


@needs_ffmpeg
def test_a_changed_space_between_two_close_lines_is_offered_for_listening(tmp_path):
    first, second = cue(0, 0.0, 2.0), cue(1, 2.4, 4.4)
    for seg in (first, second):
        source = tone(tmp_path / f"line{seg.index}.wav", seconds=1.0, channels=1)
        seg.audio.put_render(Artifact(role="normalized", path=str(source),
                                      fingerprint=f"dry{seg.index}"))
    job = _job(tmp_path, [first, second])
    stage.run(job, {"mode": "on", "default": "room",
                    "lines": {"cue-1": {"preset": "phone"}}}, work_dir=tmp_path)
    codes = {f.code for f in second.findings if f.disposition != "obsolete"}
    assert "treatment_transition" in codes
    joined = next(f for f in second.findings if f.code == "treatment_transition")
    assert joined.severity == "info"
    assert joined.evidence["from"] == "room"
    assert joined.evidence["to"] == "phone"


@needs_ffmpeg
def test_a_tail_past_the_end_of_the_programme_is_reported(tmp_path):
    seg = cue(0, 9.0, 10.0)
    source = tone(tmp_path / "line.wav", seconds=1.0, channels=1)
    seg.audio.put_render(Artifact(role="normalized", path=str(source), fingerprint="dry"))
    job = _job(tmp_path, [seg])
    stage.run(job, {"mode": "on", "default": "distant"}, work_dir=tmp_path,
              program_seconds=10.0)
    codes = {f.code for f in seg.findings if f.disposition != "obsolete"}
    assert "treatment_tail_clipped" in codes


@needs_ffmpeg
def test_a_line_with_no_rendered_audio_is_unavailable_not_applied(tmp_path):
    seg = cue()
    job = _job(tmp_path, [seg])
    stage.run(job, {"mode": "on", "default": "room"}, work_dir=tmp_path)
    assert seg.treatment.outcome == "unavailable"
    assert seg.treatment.applied is False
    assert seg.audio.render(TREATED) is None


def test_the_summary_counts_decisions_not_intentions(tmp_path):
    applied, refused = cue(0), cue(1)
    applied.treatment = Treatment(preset="room", outcome="applied", tail=0.2)
    refused.treatment = Treatment(preset="phone", outcome="unsupported")
    job = _job(tmp_path, [applied, refused])
    found = treatments.summary(job)
    assert found["applied"] == 1
    assert found["unsupported"] == 1
    assert found["presets"] == {"room": 1}
    assert found["longest_tail"] == 0.2


def test_spoken_render_excludes_a_tail_so_it_is_not_an_interruption(tmp_path):
    seg = cue()
    seg.audio.put_render(Artifact(role="normalized", path=str(tmp_path / "dry.wav"),
                                  fingerprint="dry", duration=1.0))
    seg.audio.put_render(Artifact(role=TREATED, path=str(tmp_path / "wet.wav"),
                                  fingerprint="wet", duration=1.3))
    seg.treatment = Treatment(preset="room", outcome="applied", tail=0.3)
    assert treatments.spoken_render(seg).role == "normalized"
    seg.treatment = Treatment(preset="room", outcome="bypassed")
    seg.audio.drop_renders((TREATED,))
    assert treatments.spoken_render(seg).role == "normalized"


def _job(root, segments):
    from doblarr.models import DubJob

    job = DubJob(input_file=root / "movie.mkv", source_lang="ja", target_lang="es")
    job.segments = list(segments)
    job.artifacts_dir = root
    return job
