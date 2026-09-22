"""D03 — spoken-content verification, coverage policy and bounded repair."""

import pytest

from doblarr import verify
from doblarr.budget import RequestBudget
from doblarr.cues import Verification
from doblarr.models import DubJob, Segment
from doblarr.stages import quality
from tests.test_audio_quality import wav


class Recognizer:
    """A deterministic stand-in for the recognizer half of the voicebox client."""

    def __init__(self, heard, fail=False, confidence=None):
        self.heard = heard
        self.fail = fail
        self.confidence = confidence
        self.calls = 0

    def transcribe(self, path, language=None):
        self.calls += 1
        if self.fail:
            raise RuntimeError("recognition service is unreachable")
        result = {"text": self.heard}
        if self.confidence is not None:
            result["confidence"] = self.confidence
        return result


def line(tmp_path, name, text, expected=None, **kwargs):
    seg = Segment(0, 0, 2, text, text_translated=text,
                  audio_clip=wav(tmp_path / name, 6000, 1.0), **kwargs)
    seg.cue_id = name
    seg.tts_text = expected or text
    return seg


# -- comparison ------------------------------------------------------------

def test_a_dropped_negation_is_a_mismatch_despite_a_high_similarity():
    result = verify.compare("No voy a ir nunca", "Voy a ir nunca", "es")
    assert result["similarity"] > 0.7  # the line as a whole barely moved
    assert result["state"] == "mismatch"
    assert [c["kind"] for c in result["critical"]] == ["negation"]


def test_punctuation_accents_and_case_are_not_word_errors():
    assert verify.compare("¿El gato comió?", "el gato comio", "es")["state"] == "match"


def test_an_approved_pronunciation_spelling_is_what_gets_compared(tmp_path):
    seg = line(tmp_path, "pron", "Ginko llega", expected="Guinko llega")
    assert verify.expected_for(seg) == "Guinko llega"
    assert verify.compare(verify.expected_for(seg), "Guinko llega", "es")["state"] == "match"


def test_an_omitted_word_is_reported_in_order_with_its_position():
    result = verify.compare("El gato come pescado hoy", "El gato pescado hoy", "es")
    assert result["state"] == "uncertain"
    assert result["differences"] == [
        {"op": "omission", "at": 2, "expected": ["come"], "heard": []}]


def test_a_changed_number_is_hard_evidence_and_a_spelled_one_is_not():
    changed = verify.compare("Tengo 23 anos", "Tengo 32 anos", "es")
    spelled = verify.compare("Tengo 23 anos", "Tengo veintitres anos", "es")
    dropped = verify.compare("Tengo 23 anos", "Tengo anos", "es")
    assert changed["state"] == "mismatch"
    assert spelled["state"] == "uncertain"      # same number, said differently
    assert dropped["state"] == "mismatch"


def test_a_mangled_name_is_surfaced_without_becoming_a_hard_failure():
    result = verify.compare("Vino Ginko a la casa", "Vino Guinco a la casa", "es")
    assert result["state"] == "uncertain"
    assert [c["kind"] for c in result["critical"]] == ["name"]


def test_deliberate_repetition_survives_and_a_recognizer_loop_does_not():
    assert verify.compare("Si si si claro", "Si si si claro", "es")["state"] == "match"
    loop = verify.compare("Hola amigo", "ja ja ja ja ja ja", "es")
    assert loop["state"] == "mismatch" and "repeats" in loop["reason"]


def test_a_language_without_word_spacing_says_which_tokenizer_ran():
    tokens, tokenizer = verify.tokenize("これは日本語です", "ja")
    assert tokenizer == "character/ja" and len(tokens) > 3
    result = verify.compare("これは日本語です", "これは日本語です", "ja")
    assert result["state"] == "match" and result["tokenizer"] == "character/ja"


def test_a_language_without_a_negation_table_says_so_instead_of_guessing():
    assert not verify.supports_negation("ko")
    result = verify.compare("hello there", "hello there", "ko")
    assert result["state"] == "match"


def test_empty_low_confidence_and_service_failure_are_four_different_states(tmp_path):
    assert verify.compare("hola", "", "es")["state"] == "empty"
    assert verify.compare("hola amigo mio", "hola amigo tuyo", "es",
                          confidence=0.1)["state"] == "uncertain"
    seg = line(tmp_path, "fail", "hola")
    result, _ = quality.verify_clip(seg, seg.audio_clip, "es", Recognizer("", fail=True),
                                    "all", "", "hola")
    assert result.state == "failed" and "unreachable" in result.reason
    assert not result.checked


def test_a_borrowed_word_is_not_a_wrong_word():
    """Code switching: a brand or a loanword recognized as itself is a match."""
    result = verify.compare("Abre el laptop y busca en Google",
                            "Abre el laptop y busca en Google", "es")
    assert result["state"] == "match"
    # And the same line with the borrowed word actually dropped is not.
    dropped = verify.compare("Abre el laptop y busca en Google",
                             "Abre el y busca en", "es")
    assert dropped["state"] in ("uncertain", "mismatch")
    assert any(d["op"] == "omission" for d in dropped["differences"])


def test_a_very_short_line_is_judged_as_a_short_line():
    """One word against one word: no room for a similarity score to hide in."""
    assert verify.compare("Si", "Si", "es")["state"] == "match"
    wrong = verify.compare("Si", "No", "es")
    assert wrong["state"] == "mismatch"      # a negation appeared
    assert verify.compare("Vamos", "Vemos", "es")["state"] == "mismatch"


def test_a_recognition_in_another_language_is_unsupported_not_wrong():
    result = verify.compare("El gato come pescado", "The cat eats fish", "es",
                            heard_language="en")
    assert result["state"] == "unsupported"
    assert "not es" in result["reason"]
    codes = [code for code, *_ in verify.findings_for(Verification(**{
        k: v for k, v in result.items() if k in {
            "state", "reason", "expected", "heard", "language", "tokenizer",
            "similarity", "confidence", "differences", "critical", "checker"}}))]
    assert codes == ["content_unverified"]


# -- policy and coverage ---------------------------------------------------

def test_off_never_checks_and_all_checks_every_line(tmp_path):
    seg = line(tmp_path, "policy", "una linea normal")
    assert verify.should_check(seg, "off", seg.cue_id)[0] is False
    assert verify.should_check(seg, "all", seg.cue_id)[0] is True


def test_suspicious_triggers_are_explicit_and_each_says_why(tmp_path):
    plain = line(tmp_path, "plain", "una linea normal")
    assert verify.should_check(plain, "suspicious", plain.cue_id) == (
        False, "no suspicion and not sampled")

    numbers = line(tmp_path, "numbers", "llego a las 3")
    checked, why = verify.should_check(numbers, "suspicious", numbers.cue_id)
    assert checked and "name, number or negation" in why

    rewritten = line(tmp_path, "rewritten", "una linea normal")
    rewritten.translation_provenance = {"timing_rewritten": True}
    assert "timing repair" in verify.should_check(rewritten, "suspicious",
                                                  rewritten.cue_id)[1]

    regenerated = line(tmp_path, "regen", "una linea normal", revision=2)
    assert "regenerated" in verify.should_check(regenerated, "suspicious",
                                                regenerated.cue_id)[1]

    flagged = line(tmp_path, "flagged", "una linea normal")
    assert verify.should_check(flagged, "suspicious", flagged.cue_id,
                               acoustic_issues=["clipping"])[0] is True


def test_sampling_is_deterministic_and_bounded(tmp_path):
    from doblarr.cues import imported_cue_id

    # Real cue ids are digests, so the sample is spread across the material
    # rather than landing on a run of neighbouring lines.
    keys = [imported_cue_id("script", n) for n in range(400)]
    half = [k for k in keys if verify.sampled(k, 0.5)]
    assert 0 < len(half) < len(keys)
    assert 0.35 < len(half) / len(keys) < 0.65
    assert half == [k for k in keys if verify.sampled(k, 0.5)]   # stable across calls
    assert all(verify.sampled(k, 1.0) for k in keys)
    assert not any(verify.sampled(k, 0.0) for k in keys)


def test_an_unchecked_line_never_reports_as_verified(tmp_path):
    job = DubJob(tmp_path / "movie.mkv", "en", "es")
    job.segments = [line(tmp_path, "a", "una linea normal"),
                    line(tmp_path, "b", "otra linea")]
    job.segments[1].index = 1
    quality.run(job, Recognizer("una linea normal"), asr="suspicious", normalize=False)
    coverage = job.metrics["verification"]
    assert coverage["checked"] == 0 and coverage["unchecked"] == 2
    assert all(s.verification.state == "skipped" for s in job.segments)
    assert all(not s.verification.checked for s in job.segments)


def test_all_mode_catches_a_clean_sounding_but_wrong_clip(tmp_path):
    job = DubJob(tmp_path / "movie.mkv", "en", "es")
    seg = line(tmp_path, "wrong", "el perro duerme")
    job.segments = [seg]
    quality.run(job, Recognizer("el gato vuela"), asr="all", normalize=False,
                max_retries=0)
    assert seg.verification.state == "mismatch"
    assert "text_mismatch" in seg.issues
    codes = {f.code for f in seg.findings if f.detector == verify.CHECKER}
    assert codes == {"content_mismatch"}
    assert job.metrics["verification"]["checked"] == 1


# -- caching and invalidation ---------------------------------------------

def test_recognition_is_reused_and_a_checker_policy_change_costs_no_audio(tmp_path):
    seg = line(tmp_path, "cache", "el perro duerme")
    engine = Recognizer("el perro duerme")
    first, reused = quality.verify_clip(seg, seg.audio_clip, "es", engine, "all", "",
                                        "el perro duerme", audio_fingerprint="gen-1")
    seg.verification = first
    assert engine.calls == 1 and not reused
    again, reused = quality.verify_clip(seg, seg.audio_clip, "es", engine, "all", "",
                                        "el perro duerme", audio_fingerprint="gen-1")
    assert reused and engine.calls == 1 and again.state == "match"
    # New audio for the same words is a different check.
    quality.verify_clip(seg, seg.audio_clip, "es", engine, "all", "",
                        "el perro duerme", audio_fingerprint="gen-2")
    assert engine.calls == 2


def test_changed_wording_invalidates_the_evidence(tmp_path):
    seg = line(tmp_path, "words", "el perro duerme")
    engine = Recognizer("el perro duerme")
    seg.verification, _ = quality.verify_clip(seg, seg.audio_clip, "es", engine, "all",
                                              "", "el perro duerme",
                                              audio_fingerprint="gen-1")
    quality.verify_clip(seg, seg.audio_clip, "es", engine, "all", "",
                        "el gato duerme", audio_fingerprint="gen-1")
    assert engine.calls == 2


# -- budget and repair -----------------------------------------------------

def test_recognition_and_repair_share_one_budget(tmp_path):
    job = DubJob(tmp_path / "movie.mkv", "en", "es")
    job.segments = [line(tmp_path, f"cue{i}", "el perro duerme") for i in range(3)]
    for i, seg in enumerate(job.segments):
        seg.index = i
    budget = RequestBudget(limit=2)
    quality.run(job, Recognizer("el gato vuela"), asr="all", normalize=False,
                max_retries=0, budget=budget)
    assert budget.spent == 2 and budget.by_kind == {"asr": 2}
    assert job.segments[2].verification.state == "skipped"
    assert "budget is exhausted" in job.segments[2].verification.reason


def test_a_failed_repair_leaves_usable_audio_and_an_open_finding(tmp_path):
    job = DubJob(tmp_path / "movie.mkv", "en", "es")
    seg = line(tmp_path, "repair", "el perro duerme")
    job.segments = [seg]
    tries = []

    def regenerate(target):
        tries.append(target.revision)
        wav(target.audio_clip, 6000, 1.0)   # a new take that is still wrong

    quality.run(job, Recognizer("el gato vuela"), asr="all", normalize=False,
                max_retries=2, regenerate=regenerate)
    assert tries == [1, 2]                       # bounded, not a loop
    assert seg.audio_clip.is_file()              # the audio is still there
    assert seg.verification.state == "mismatch"
    assert seg.verification.attempts == 2
    open_findings = [f for f in seg.findings
                     if f.detector == verify.CHECKER and f.disposition == "open"]
    assert open_findings


def test_repair_stops_at_the_budget_and_says_so(tmp_path):
    job = DubJob(tmp_path / "movie.mkv", "en", "es")
    seg = line(tmp_path, "bounded", "el perro duerme")
    job.segments = [seg]
    budget = RequestBudget(limit=1)   # one recognition; nothing left for a retry

    quality.run(job, Recognizer("el gato vuela"), asr="all", normalize=False,
                max_retries=2, regenerate=lambda s: wav(s.audio_clip, 6000, 1.0),
                budget=budget)
    assert budget.refused >= 1
    assert "budget exhausted" in seg.verification.reason


# -- findings --------------------------------------------------------------

def test_a_service_failure_is_reviewable_and_never_an_approval():
    result = Verification(state="failed", reason="recognition failed", expected="hola")
    codes = [code for code, *_ in verify.findings_for(result)]
    assert codes == ["content_unverified"]
    assert not result.checked


def test_an_accepted_content_finding_reopens_when_the_audio_changes(tmp_path):
    job = DubJob(tmp_path / "movie.mkv", "en", "es")
    seg = line(tmp_path, "reopen", "el perro duerme")
    job.segments = [seg]
    quality.run(job, Recognizer("el gato vuela"), asr="all", normalize=False,
                max_retries=0)
    finding = next(f for f in seg.findings if f.detector == verify.CHECKER)
    finding.disposition = "accepted"

    seg.verification.inputs = "different-inputs"
    quality.verification_findings(seg)
    assert finding.disposition == "open"
    assert finding.history[-1]["reason"] == "inputs changed"


@pytest.mark.parametrize("policy", ["off", "suspicious", "all"])
def test_every_policy_is_accepted_and_anything_else_is_rejected(tmp_path, policy):
    job = DubJob(tmp_path / "movie.mkv", "en", "es")
    job.segments = [line(tmp_path, "p", "hola")]
    quality.run(job, Recognizer("hola"), asr=policy, normalize=False, max_retries=0)
    with pytest.raises(ValueError):
        quality.run(job, Recognizer("hola"), asr="sometimes", normalize=False)
