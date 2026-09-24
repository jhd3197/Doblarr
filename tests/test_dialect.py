from pathlib import Path

import pytest

from doblarr import dialect
from doblarr.cues import ensure_identity
from doblarr.models import DubJob, Segment
from doblarr.stages import translate


def _job(lines, target_lang="es", target_locale="es-419"):
    job = DubJob(input_file=Path("film.mkv"), source_lang="en", target_lang=target_lang)
    job.target_locale = target_locale
    job.segments = [Segment(i, i * 3.0, i * 3.0 + 2.0, f"line {i}") for i in range(len(lines))]
    for seg, text in zip(job.segments, lines, strict=True):
        seg.text_translated = text
    ensure_identity(job)
    return job


def _open(seg):
    return [f for f in seg.findings
            if f.detector == dialect.DETECTOR and f.disposition == "open"]


@pytest.mark.parametrize("locale, expected", [
    ("es-419", True), ("es-MX", True), ("es-VE", True), ("es-AR", True),
    ("es-ES", False), ("es", False), ("", False), (None, False), ("pt-BR", False),
])
def test_only_latin_american_targets_are_checked(locale, expected):
    assert dialect.applies(locale) is expected


def test_markers_name_the_word_and_a_latin_american_alternative():
    hits = dialect.markers("¿Sabéis dónde está mi móvil? Cógelo.")
    assert [(h["word"], h["category"]) for h in hits] == [
        ("Sabéis", "vosotros"), ("móvil", "vocabulary"), ("Cógelo", "coger")]
    assert hits[1]["suggest"] == "celular"


@pytest.mark.parametrize("text, flagged", [
    ("Vale.", True),
    ("Vale, gracias.", True),
    ("Trato hecho. ¿Vale?", True),
    ("Bueno, vale.", True),
    ("No vale la pena.", False),     # "worth it" — shared by every dialect
    ("¿Cuánto vale?", False),        # "how much is it"
    ("Eso no vale.", False),         # "that doesn't count"
    ("El Norte, Vale, y el Dominio.", False),  # a place name
])
def test_vale_is_flagged_only_as_a_standalone_okay(text, flagged):
    assert any(h["word"] == "vale" for h in dialect.markers(text)) is flagged


def test_a_por_is_flagged_as_spain_grammar():
    [hit] = dialect.markers("Voy a por agua.")
    assert hit["category"] == "grammar" and "voy por agua" in hit["suggest"]
    assert dialect.markers("Voy por agua.") == []


def test_shared_or_ambiguous_words_are_not_markers():
    # tío (uncle), rollo (everyday Mexican), ID, cojo (lame), and the strong
    # profanity Latin American subtitles use on purpose.
    for text in ("Mi tío llegó.", "¿Qué rollo?", "Tu ID, por favor.",
                 "Hijo de puta.", "Está cojo.", "Te amo, mamá.",
                 "¡Es un puto bebé!", "Esto está jodido."):
        assert dialect.markers(text) == [], text


def test_check_records_one_finding_per_flagged_line():
    job = _job(["Vale, vámonos.", "Está bien, vámonos.", "¿Habéis visto el coche?"])
    assert dialect.check(job) == 2
    assert [len(_open(s)) for s in job.segments] == [1, 0, 1]
    finding = _open(job.segments[2])[0]
    assert finding.code == "spain_spanish" and finding.severity == "warning"
    assert [m["word"] for m in finding.evidence["markers"]] == ["Habéis", "coche"]
    assert job.metrics["spain_spanish_lines"] == 2


def test_a_fixed_line_makes_its_finding_obsolete():
    job = _job(["Vale."])
    dialect.check(job)
    job.segments[0].text_translated = "Está bien."
    assert dialect.check(job) == 0
    [finding] = [f for f in job.segments[0].findings if f.detector == dialect.DETECTOR]
    assert finding.disposition == "obsolete"


def test_spain_targets_are_left_alone():
    job = _job(["Vale, ¿qué hacéis?"], target_locale="es-ES")
    assert dialect.check(job) == 0
    assert _open(job.segments[0]) == []
    assert "spain_spanish_lines" not in job.metrics


def test_translate_stage_runs_the_check():
    class Translator:
        def translate_batch(self, segments, source_lang, target_lang, context=None,
                            glossary=None, **kwargs):
            return ["Vale, os espero." for _ in segments]

    job = _job(["", ""])
    for seg in job.segments:
        seg.text_translated = None
    translate.run(job, Translator())
    assert job.metrics["spain_spanish_lines"] == 2
    assert {m["word"] for m in _open(job.segments[0])[0].evidence["markers"]} == {"os", "vale"}
