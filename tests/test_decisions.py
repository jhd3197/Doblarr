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


# -- 1. cut-off lines ----------------------------------------------------------

@pytest.mark.parametrize("text, ending", [
    ("Wait, I—", "interrupted"), ("Espera, yo--", "interrupted"), ("But then-", "interrupted"),
    ("And so I...", "trailing"), ("待って、私は…", "trailing"), ("«Y entonces…»", "trailing"),
    ("I'm going home.", "complete"), ("¿Qué haces aquí?", "complete"), ("Run!", "complete"),
    ("so we went home", None),
])
def test_line_endings_from_punctuation(text, ending):
    assert decisions.cutoff_rule(text) == ending


def test_the_model_is_asked_only_where_punctuation_is_silent():
    job = job_of("Wait, I—", "I'm home.", "and then we", "maybe later")
    replies = {"and then we": ("interrupted", 0.95), "maybe later": ("trailing", 0.7)}
    oracle, driver = oracle_with(lambda s, qs: {"ending": replies[s["line"]]})
    hints = decisions.cutoffs(job, oracle, decisions.settings(None))
    asked = [state["line"] for state, _q in driver.calls]
    assert asked == ["and then we", "maybe later"]
    cues = [s.cue_id for s in job.segments]
    assert hints == {cues[0]: "interrupted", cues[2]: "interrupted"}  # 0.7 only suggests
    verdicts = job.metrics["decisions"]["cutoffs"]["verdicts"]
    assert [(v["source"], v["applied"]) for v in verdicts] == [
        ("rules", True), ("model", True), ("model", False)]


def test_cutoffs_off_decides_nothing():
    job = job_of("Wait, I—")
    config = decisions.settings({"cutoffs": False})
    assert decisions.cutoffs(job, decisions.Oracle({}), config) == {}


# -- 2. delivery mode ----------------------------------------------------------

@pytest.mark.parametrize("text, mode", [
    ("(whispering) Don't wake him.", "whisper"), ("[shouts] Get down!", "shout"),
    ("(susurrando) No lo despiertes.", "whisper"), ("(thinking) Why me?", "thought"),
    ("(calling out) Over here!", "call"), ("(over radio) Unit two, respond.", "broadcast"),
    ("GET OUT OF HERE!", "shout"), ("NEW YORK, 1987", None), ("I'm fine.", None),
    ("OK!", None),
])
def test_delivery_the_words_state(text, mode):
    assert decisions.delivery_rule(text) == mode


def test_delivery_sets_only_unset_lines_and_asks_only_marked_ones():
    job = job_of("(whispering) Hush.", "Run, now!", "I'm fine.", "Careful!")
    job.segments[3].intent.mode, job.segments[3].intent.origin = "normal", "manual"
    oracle, driver = oracle_with(lambda s, qs: {"mode": ("shout", 0.93)})
    assert decisions.delivery(job, oracle, decisions.settings(None)) == 2
    modes = [(s.intent.mode, s.intent.origin) for s in job.segments]
    assert modes == [("whisper", "decision"), ("shout", "decision"),
                     ("unknown", "unknown"), ("normal", "manual")]
    assert [state["line"] for state, _q in driver.calls] == ["Run, now!"]


def test_delivery_is_recomputed_so_switching_it_off_reverts():
    job = job_of("(whispering) Hush.")
    decisions.delivery(job, decisions.Oracle({}), decisions.settings(None))
    assert job.segments[0].intent.mode == "whisper"
    decisions.delivery(job, decisions.Oracle({}), decisions.settings({"delivery": False}))
    assert (job.segments[0].intent.mode, job.segments[0].intent.origin) == ("unknown", "unknown")


def test_an_unsure_model_only_suggests_a_mode():
    job = job_of("Well!")
    oracle, _driver = oracle_with(lambda s, qs: {"mode": ("shout", 0.7)})
    assert decisions.delivery(job, oracle, decisions.settings(None)) == 0
    assert job.segments[0].intent.mode == "unknown"
    (finding,) = [f for f in job.segments[0].findings if f.code == "decision_delivery"]
    assert finding.evidence == {"value": "shout", "source": "model", "applied": False, "note": ""}


# -- 3. captions and title cards -----------------------------------------------

@pytest.mark.parametrize("text, caption", [
    ("NEW YORK, 1987", True), ("THREE YEARS LATER", True), ("PARIS, FRANCE", True),
    ("CHAPTER 2", True), ("TRES AÑOS DESPUÉS", True),
    ("GET OUT OF HERE!", False), ("WHERE ARE YOU?", False), ("I'm in New York, 1987.", False),
    ("HELLO THERE", False),  # capitals, but nothing marks it as a caption
])
def test_captions_the_text_shows_plainly(text, caption):
    assert decisions.card_rule(text) is caption


def test_a_caption_leaves_the_script_as_a_timed_event():
    job = job_of("NEW YORK, 1987", "Where were you?", "OLD MILL ROAD")
    oracle, driver = oracle_with(lambda s, qs: {"card": 0.96})
    assert decisions.title_cards(job, oracle, decisions.settings(None)) == 2
    assert [s.text_src for s in job.segments] == ["Where were you?"]
    assert [state["line"] for state, _q in driver.calls] == ["OLD MILL ROAD"]
    events = {e.text: e for e in job.nonverbal}
    assert events["NEW YORK, 1987"].checks["detected_by"] == "rules"
    assert events["OLD MILL ROAD"].checks["detected_by"] == "model"
    assert all(e.category == "background" and e.decision == "unresolved"
               for e in events.values())


def test_an_unsure_caption_stays_a_line_with_a_suggestion():
    job = job_of("OLD MILL ROAD")
    oracle, _driver = oracle_with(lambda s, qs: {"card": 0.7})
    assert decisions.title_cards(job, oracle, decisions.settings(None)) == 0
    assert [s.text_src for s in job.segments] == ["OLD MILL ROAD"]
    assert any(f.code == "decision_title_cards" for f in job.segments[0].findings)


def test_without_the_model_only_plain_captions_leave():
    job = job_of("NEW YORK, 1987", "OLD MILL ROAD")
    decisions.title_cards(job, decisions.Oracle({"model": ""}), decisions.settings(None))
    assert [s.text_src for s in job.segments] == ["OLD MILL ROAD"]


# -- 4. reaction-only cues -----------------------------------------------------

def test_a_wordless_cue_the_model_is_sure_of_becomes_a_reaction():
    job = job_of("えっ", "Where is he?", "ハハハ")
    replies = {"えっ": (0.95, ("gasp", 0.9)), "ハハハ": (0.7, ("laugh", 0.9))}
    oracle, driver = oracle_with(lambda s, qs: {"reaction": replies[s["line"]][0],
                                                "kind": replies[s["line"]][1]})
    assert decisions.reactions(job, oracle, decisions.settings(None)) == 1
    assert [s.text_src for s in job.segments] == ["Where is he?", "ハハハ"]
    (event,) = job.nonverbal
    assert (event.type, event.checks["detected_by"]) == ("gasp", "model")
    assert event.decision == "unresolved"
    assert sorted(state["line"] for state, _q in driver.calls) == ["えっ", "ハハハ"]
    assert job.metrics["reaction_cues_by_model"] == 1


def test_a_cue_with_words_is_never_asked_about_or_converted():
    job = job_of("Oh no, not again")
    oracle, driver = oracle_with(lambda s, qs: {"reaction": 1.0, "kind": ("laugh", 1.0)})
    assert decisions.reactions(job, oracle, decisions.settings(None)) == 0
    assert driver.calls == [] and job.nonverbal == []


def test_reactions_follow_the_interjections_setting():
    job = job_of("えっ")
    oracle, driver = oracle_with(lambda s, qs: {"reaction": 1.0, "kind": ("gasp", 1.0)})
    assert decisions.reactions(job, oracle, decisions.settings(None), interjections=False) == 0
    assert driver.calls == []
