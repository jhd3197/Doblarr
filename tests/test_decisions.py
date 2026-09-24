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


# -- 5. unrecognized sound tags ------------------------------------------------

@pytest.mark.parametrize("text, named", [
    ("[door creaks open]", ("door", "background")), ("(nervous chuckle)", ("laugh", "vocal")),
    ("[phone buzzing]", ("unknown", "background")), ("[soft sobbing]", ("cry", "vocal")),
    ("[suspira]", ("sigh", "vocal")), ("[indistinct chatter]", None),
])
def test_sound_tags_the_words_name(text, named):
    assert decisions.sound_rule(text) == named


def test_a_cue_that_is_only_an_unknown_tag_is_named_and_not_spoken():
    from doblarr.stages import prepare

    job = job_of("[door creaks open]", "[eerie hum]", "Hi.", "[quietly]", speaker="A")
    prepare.run(job)
    # the parser does not know these tags, so they are still lines to speak
    assert len(job.segments) == 4 and job.nonverbal == []
    replies = {"[eerie hum]": ("bed", 0.95), "[quietly]": ("voice", 0.5)}
    oracle, driver = oracle_with(lambda s, qs: {"sound": replies[s["tag"]]})
    assert decisions.sound_tags(job, oracle, decisions.settings(None)) == 2
    assert [s.text_src for s in job.segments] == ["Hi.", "[quietly]"]  # unsure: kept
    events = {e.text: e for e in job.nonverbal}
    door, hum = events["[door creaks open]"], events["[eerie hum]"]
    assert (door.type, door.category, door.checks["classified_by"]) == \
        ("door", "background", "rules")
    assert (hum.type, hum.category, hum.checks["classified_by"]) == \
        ("unknown", "background", "model")
    assert [state["tag"] for state, _q in driver.calls] == ["[eerie hum]", "[quietly]"]
    assert all(e.decision == "unresolved" for e in job.nonverbal)
    # a second pass finds nothing left to name
    assert decisions.sound_tags(job, oracle, decisions.settings(None)) == 0


def test_an_unknown_event_on_the_ledger_gets_its_type():
    from doblarr.cues import NonverbalEvent

    job = job_of("Hi.")
    job.nonverbal.append(NonverbalEvent(event_id="e1", cue_id="c", text="[gasps loudly]"))
    oracle, driver = oracle_with(lambda s, qs: {"sound": ("bed", 1.0)})
    assert decisions.sound_tags(job, oracle, decisions.settings(None)) == 1
    assert (job.nonverbal[0].type, job.nonverbal[0].category) == ("gasp", "vocal")
    assert driver.calls == []


# -- 6. timing rewrites --------------------------------------------------------

@pytest.mark.parametrize("original, shorter, rejected", [
    ("Tell Ginko we leave at 5.", "Tell Ginko at 5.", False),
    ("Tell Ginko we leave at 5.", "We leave at 5.", True),        # a name dropped
    ("Tell Ginko we leave at 5.", "Tell Ginko soon.", True),      # a number dropped
    ("Espera. Mañana vamos a Madrid con Ana.", "Mañana, Madrid con Ana.", False),
    ("Well, I'm sure. OK then.", "Sure.", False),                 # grammar capitals
])
def test_rewrites_that_drop_names_or_numbers(original, shorter, rejected):
    assert (decisions.rewrite_rule(original, shorter) is not None) is rejected


def test_the_checker_rejects_by_rule_or_a_confident_model():
    job = job_of("a", "b", "c")
    replies = {"Short one.": 0.02, "Short two.": 0.3}
    oracle, driver = oracle_with(lambda s, qs: {"same": replies[s["shorter"]]})
    accept = decisions.rewrite_checker(job, oracle, decisions.settings(None))
    a, b, c = job.segments
    assert accept(a, "Tell Ginko now.", "Tell him now.") is False     # rule
    assert accept(b, "A long first line.", "Short one.") is False     # model sure: changed
    assert accept(c, "A long second line.", "Short two.") is True     # model unsure: suggest
    assert len(driver.calls) == 2
    assert job.metrics["timing_rewrites_rejected"] == 2
    summary = job.metrics["decisions"]["rewrite_check"]
    assert (summary["applied"], summary["suggested"]) == (2, 1)


def test_the_rewrite_check_can_be_switched_off():
    assert decisions.rewrite_checker(job_of("a"), decisions.Oracle({}),
                                     decisions.settings({"rewrite_check": False})) is None


# -- 7. treatment suggestions --------------------------------------------------

@pytest.mark.parametrize("text, preset", [
    ("(on phone) Where are you?", "phone"), ("[over radio] Copy that.", "radio"),
    ("(on TV) Tonight's weather...", "radio"), ("(distant) Help!", "distant"),
    ("(O.S.) Dinner's ready!", "distant"), ("Give me your phone.", None),
    ("(whispering) Quiet.", None),
])
def test_treatments_a_stage_direction_names(text, preset):
    assert decisions.space_rule(text) == preset


def test_treatments_are_only_ever_suggested():
    job = job_of("(on phone) Where are you?", "[muttering] Fine.", "Plain line.")
    oracle, driver = oracle_with(lambda s, qs: {"space": ("phone", 0.99)})
    assert decisions.treatments(job, oracle, decisions.settings(None)) == 2
    assert [state["line"] for state, _q in driver.calls] == ["[muttering] Fine."]
    summary = job.metrics["decisions"]["treatments"]
    assert summary["applied"] == 0 and summary["suggested"] == 2
    from doblarr.cues import Treatment

    assert all(s.treatment == Treatment() for s in job.segments)  # nothing applied
