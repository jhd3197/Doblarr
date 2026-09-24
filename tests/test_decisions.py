"""Typed decisions: the shared layer, with a fake decision model."""

import types
from pathlib import Path

import pytest

from doblarr import decisions
from doblarr.cues import ensure_identity
from doblarr.models import DubJob, Segment


class FakeDriver:
    """Answers from `rules(state, questions) -> {qid: answer}`; counts calls."""

    def __init__(self, rules):
        self.rules = rules
        self.calls = []
        self.unloaded = False

    def decide(self, state, questions):
        self.calls.append((state, questions))
        answers = {}
        for qid, raw in self.rules(state, questions).items():
            if questions[qid]["type"] == "noul":
                answers[qid] = types.SimpleNamespace(type="noul", noul=raw,
                                                     confidence=max(raw, 1 - raw))
            else:
                answers[qid] = types.SimpleNamespace(type="choice", choice=raw[0],
                                                     confidence=raw[1])
        return types.SimpleNamespace(answers=answers)

    def unload(self):
        self.unloaded = True


def oracle_with(rules, tmp_path=None, **config):
    driver = FakeDriver(rules)
    oracle = decisions.Oracle(config, cache_dir=tmp_path, factory=lambda model: driver)
    return oracle, driver


def job_of(*texts, **fields):
    job = DubJob(input_file=Path("film.mkv"), source_lang="en", target_lang="es")
    job.segments = [Segment(i, i * 3.0, i * 3.0 + 2.0, text, **fields)
                    for i, text in enumerate(texts)]
    ensure_identity(job)
    return job


def test_settings_default_on_with_bounded_thresholds():
    config = decisions.settings(None)
    assert config["enabled"] and all(config[k] for k in decisions.KINDS)
    assert config["model"] == "laya/router"
    clamped = decisions.settings({"apply_confidence": 0.5, "suggest_confidence": 0.8})
    assert clamped["suggest_confidence"] == 0.5  # never above the apply threshold
    assert not decisions.enabled(decisions.settings({"enabled": False}), "cutoffs")
    assert not decisions.enabled(decisions.settings({"cutoffs": False}), "cutoffs")


def test_answers_are_cached_across_runs(tmp_path):
    q = {"x": decisions.noul("Is it?")}
    oracle, driver = oracle_with(lambda s, qs: {"x": 0.95}, tmp_path)
    assert oracle.ask("k", {"line": "a"}, q)["x"]["yes"] == 0.95
    oracle.ask("k", {"line": "a"}, q)
    oracle.close()
    assert len(driver.calls) == 1 and driver.unloaded
    again, driver2 = oracle_with(lambda s, qs: {"x": 0.1}, tmp_path)
    assert again.ask("k", {"line": "a"}, q)["x"]["yes"] == 0.95  # from disk
    assert driver2.calls == []


def test_a_model_that_cannot_load_leaves_the_rules_alone():
    def broken(model):
        raise ImportError("laya needs transformers 5")

    oracle = decisions.Oracle({}, factory=broken)
    assert oracle.ask("k", {"line": "a"}, {"x": decisions.noul("?")}) is None
    assert oracle.state == "unavailable" and "transformers" in oracle.reason
    assert oracle.ask("k", {"line": "b"}, {"x": decisions.noul("?")}) is None


def test_a_failing_call_stops_asking():
    driver = FakeDriver(lambda s, qs: (_ for _ in ()).throw(RuntimeError("boom")))
    oracle = decisions.Oracle({}, factory=lambda model: driver)
    assert oracle.ask("k", {"line": "a"}, {"x": decisions.noul("?")}) is None
    oracle.ask("k", {"line": "b"}, {"x": decisions.noul("?")})
    assert len(driver.calls) == 1


def test_disabled_asks_nothing():
    driver = FakeDriver(lambda s, qs: {"x": 1.0})
    oracle = decisions.Oracle({"enabled": False}, factory=lambda model: driver)
    assert oracle.ask("k", {"line": "a"}, {"x": decisions.noul("?")}) is None
    assert driver.calls == [] and oracle.state == "off"


@pytest.mark.parametrize("answer, outcome", [
    ({"type": "noul", "yes": 0.95}, "apply"),
    ({"type": "noul", "yes": 0.7}, "suggest"),
    ({"type": "noul", "yes": 0.5}, "drop"),
    ({"type": "choice", "choice": "shout", "confidence": 0.92}, "apply"),
    (None, "drop"),
])
def test_weigh_uses_the_thresholds(answer, outcome):
    config = decisions.settings(None)
    assert decisions.weigh(config, answer, value="yes")[2] == outcome


def test_a_confident_no_can_be_the_decision():
    value, confidence, outcome = decisions.weigh(
        decisions.settings(None), {"type": "noul", "yes": 0.03}, value="changed", yes=False)
    assert (value, outcome) == ("changed", "apply") and confidence == pytest.approx(0.97)


def test_verdicts_become_findings_that_retire():
    job = job_of("a", "b")
    cue = job.segments[0].cue_id
    decisions.record(job, [decisions.Verdict("cutoffs", cue, "interrupted", 1.0, "rules", True)],
                     "cutoffs")
    (finding,) = [f for f in job.segments[0].findings if f.code == "decision_cutoffs"]
    assert finding.evidence["source"] == "rules" and finding.evidence["applied"] is True
    assert job.metrics["decisions"]["cutoffs"]["applied"] == 1
    decisions.record(job, [], "cutoffs")
    assert finding.disposition == "obsolete"
