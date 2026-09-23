"""Sounds nobody asked for, and reactions nobody should re-act."""

import math
import struct
import wave

import pytest

from doblarr import vocalization
from doblarr.cues import SOURCE, Span, ensure_identity, validate_events
from doblarr.models import DubJob, Segment
from doblarr.stages import prepare

RATE = 16000


def _take(path, pieces):
    """A take made of (seconds, voiced) pieces: a tone where voiced, else silence."""
    frames = []
    for seconds, voiced in pieces:
        for index in range(int(RATE * seconds)):
            value = 0.3 * math.sin(2 * math.pi * 220 * index / RATE) if voiced else 0.0
            frames.append(struct.pack("<h", int(value * 32000)))
    with wave.open(str(path), "wb") as out:
        out.setparams((1, 2, RATE, 0, "NONE", "not compressed"))
        out.writeframes(b"".join(frames))
    return path


class Hears:
    """A recognizer that answers each call with the next scripted text."""

    def __init__(self, *texts):
        self.texts = list(texts)
        self.calls = 0

    def transcribe(self, path, language=None):
        self.calls += 1
        return {"text": self.texts.pop(0) if self.texts else ""}


def _job(tmp_path, lines):
    media = tmp_path / "movie.mkv"
    media.write_text("video", encoding="utf-8")
    job = DubJob(input_file=media, source_lang="ja", target_lang="es")
    job.segments = [Segment(index, start, end, text)
                    for index, (start, end, text) in enumerate(lines)]
    for seg in job.segments:
        seg.source.spans = [Span(seg.start, seg.end, SOURCE)]
    ensure_identity(job)
    return job


# -- invented sound ----------------------------------------------------------

def test_a_giggle_before_the_words_is_found_and_placed(tmp_path):
    take = _take(tmp_path / "take.wav", [(0.1, False), (1.0, True), (0.4, False),
                                         (1.6, True), (0.1, False)])
    heard = Hears("Jajaja", "Sí, con mis ojos.")
    result = vocalization.check(take, "Sí, con mis ojos.", "es", heard)
    assert result["state"] == "extra"
    [extra] = result["unaccounted"]
    assert extra["position"] == "leading" and extra["heard"] == "Jajaja"
    assert extra["start"] == pytest.approx(0.1, abs=0.05)
    assert "leading" in vocalization.describe(result)


def test_words_the_recognizer_runs_together_are_still_the_line(tmp_path):
    take = _take(tmp_path / "take.wav", [(1.2, True), (0.5, False), (1.4, True)])
    heard = Hears("Sí", "¡Comisojos!")
    result = vocalization.check(take, "Sí, con mis ojos.", "es", heard)
    assert result["state"] == "clean" and result["unaccounted"] == []


def test_a_take_that_fits_its_words_is_never_sent_to_the_recognizer(tmp_path):
    take = _take(tmp_path / "take.wav", [(0.1, False), (1.0, True), (0.1, False)])
    heard = Hears()
    result = vocalization.check(take, "Sí, con mis ojos.", "es", heard)
    assert result["state"] == "clean" and heard.calls == 0


def test_without_a_recognizer_only_an_extreme_excess_is_suspicious(tmp_path):
    long = _take(tmp_path / "long.wav", [(4.0, True)])
    assert vocalization.check(long, "Sí.", "es")["state"] == "suspicious"
    modest = _take(tmp_path / "modest.wav", [(1.4, True)])
    assert vocalization.check(modest, "Sí, con mis ojos.", "es")["state"] == "clean"


def test_a_recognizer_failure_is_unchecked_not_clean(tmp_path):
    class Broken:
        def transcribe(self, path, language=None):
            raise RuntimeError("offline")

    take = _take(tmp_path / "take.wav", [(1.0, True), (0.4, False), (1.6, True)])
    assert vocalization.check(take, "Sí.", "es", Broken())["state"] == "unchecked"


# -- reactions written as words ------------------------------------------------

@pytest.mark.parametrize("text, kind", [
    ("Tsk!", "interjection"), ("Tch.", "interjection"), ("Oh!", "interjection"),
    ("Huh?", "interjection"), ("Hmm...", "interjection"), ("Ugh...", "interjection"),
    ("Heh heh...", "laugh"), ("Hahaha!", "laugh"), ("Fufufu", "laugh"),
    ("¡Jajaja!", "laugh"), ("Ahaha", "laugh"),
])
def test_a_cue_made_only_of_reaction_sounds_is_recognised(text, kind):
    assert prepare.interjection(text) == kind


@pytest.mark.parametrize("text", [
    "Huh? Yeah...", "Ah! I see.", "Oh, then", "Ink?", "Wow!", "Hey!", "Aha!",
    "Yeah.", "No!", "Hi!", "[laughs]", "",
])
def test_anything_with_a_word_in_it_stays_speech(text):
    assert prepare.interjection(text) is None


def test_a_bare_reaction_becomes_an_event_instead_of_a_line(tmp_path):
    job = _job(tmp_path, [(1.0, 2.0, "Tsk!"), (3.0, 4.0, "Huh? Yeah...")])
    prepare.run(job)
    assert [s.text_src for s in job.segments] == ["Huh? Yeah..."]
    [event] = job.nonverbal
    assert (event.type, event.category, event.text) == ("interjection", "vocal", "Tsk!")
    assert job.metrics["interjection_cues"] == 1
    validate_events(job.nonverbal)


def test_bare_reactions_can_be_left_as_lines(tmp_path):
    job = _job(tmp_path, [(1.0, 2.0, "Tsk!")])
    prepare.run(job, interjections=False)
    assert [s.text_src for s in job.segments] == ["Tsk!"]
