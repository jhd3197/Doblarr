"""The phrase planner: what it decides, and what it refuses to decide.

These are arithmetic over measured evidence, so they are exact. None of them
says anything about how the result sounds — that needs speech and a listener,
and is recorded separately.
"""

import pytest

from doblarr import phrases
from doblarr.cues import CLIP, SOURCE, ensure_identity
from doblarr.models import DubJob, Segment
from doblarr.stages.boundaries import Bounds


def _bounds(duration, runs, *, noise=-60.0, speech=-12.0):
    """A measured take with the given speech runs and the gaps between them."""
    silences = [(previous[1], following[0])
                for previous, following in zip(runs, runs[1:], strict=False)]
    return Bounds(duration, int(duration * 100), 48000, noise, speech,
                  runs[0][0], runs[-1][1], False, silences)


def _job(tmp_path):
    job = DubJob(input_file=tmp_path / "movie.mkv", source_lang="ja", target_lang="es")
    return job


def _seg(tmp_path, start, end, text="Primera parte. Segunda parte.", words=None):
    job = _job(tmp_path)
    job.segments = [Segment(0, start, end, text, text_translated=text, words=words or [])]
    ensure_identity(job)
    return job.segments[0]


def _config(**overrides):
    return phrases.settings({"mode": "phrase", **overrides})


def _gap(plan):
    """The one interior pause in a two-phrase plan (not the lead or the tail)."""
    return next(p for p in plan.pauses if p.after and p.origin not in ("tail", "boundary"))


# -- evidence ---------------------------------------------------------------

def test_two_runs_of_speech_become_two_phrases_with_the_gap_between_them(tmp_path):
    seg = _seg(tmp_path, 0.0, 3.0)
    plan = phrases.build(seg, _bounds(2.4, [(0.1, 1.0), (1.6, 2.3)]), _config())
    assert [p.order for p in plan.phrases] == [0, 1]
    assert [p.text for p in plan.phrases] == ["Primera parte.", "Segunda parte."]
    gaps = [p for p in plan.pauses if p.after and p.origin != "tail"]
    assert len(gaps) == 1
    # 0.6s of detected silence, protected because it is longer than the beat
    # the settings call performance.
    assert gaps[0].protected and gaps[0].kind == "pause"


def test_a_short_gap_is_padding_and_a_long_one_is_performance(tmp_path):
    seg = _seg(tmp_path, 0.0, 3.0)
    short = phrases.build(seg, _bounds(2.2, [(0.1, 1.0), (1.2, 2.1)]), _config())
    assert not _gap(short).protected
    long = phrases.build(seg, _bounds(2.6, [(0.1, 1.0), (1.6, 2.5)]), _config())
    assert _gap(long).protected


def test_handles_grow_a_phrase_into_its_silence_but_never_past_its_neighbour(tmp_path):
    seg = _seg(tmp_path, 0.0, 3.0)
    plan = phrases.build(seg, _bounds(2.0, [(0.5, 1.0), (1.1, 1.8)]),
                         _config(handle_ms=200))
    first, second = (p.clip for p in plan.phrases)
    # The 0.1s gap is split at its midpoint, so the two never overlap and no
    # sample can be rendered twice.
    assert first.end <= second.start
    assert first.end == pytest.approx(1.05) and second.start == pytest.approx(1.05)
    assert first.start >= 0.0


def test_a_single_run_of_speech_is_one_phrase_and_says_so(tmp_path):
    seg = _seg(tmp_path, 0.0, 3.0, text="Una sola frase")
    plan = phrases.build(seg, _bounds(2.0, [(0.1, 1.9)]), _config())
    assert len(plan.phrases) == 1 and plan.phrases[0].method == "whole"


# -- correspondence ---------------------------------------------------------

def test_source_runs_are_paired_only_when_both_sides_group_the_same_way(tmp_path):
    words = [{"word": "a", "start": 1.0, "end": 1.4},
             {"word": "b", "start": 1.4, "end": 1.8},
             {"word": "c", "start": 2.6, "end": 3.0}]
    seg = _seg(tmp_path, 1.0, 4.0, words=words)
    seg.source.spans = [phrases.Span(1.0, 3.0, SOURCE)]
    seg.source.word_domain = SOURCE
    plan = phrases.build(seg, _bounds(2.4, [(0.1, 1.0), (1.6, 2.3)]), _config())
    assert [bool(p.source) for p in plan.phrases] == [True, True]
    # And the association becomes a *soft* suggestion about where the original
    # speaker started, never a hard requirement.
    kinds = {a.kind for a in plan.anchors}
    assert kinds == {"soft"}
    assert all(a.origin == "source" for a in plan.anchors)


def test_a_different_number_of_source_runs_leaves_the_pairing_unknown(tmp_path):
    """Translated word order is not the original's. An unknown pairing stays unknown."""
    words = [{"word": "a", "start": 1.0, "end": 1.4},
             {"word": "b", "start": 2.0, "end": 2.4},
             {"word": "c", "start": 3.0, "end": 3.4}]  # three source runs
    seg = _seg(tmp_path, 1.0, 4.0, words=words)
    seg.source.spans = [phrases.Span(1.0, 3.4, SOURCE)]
    seg.source.word_domain = SOURCE
    plan = phrases.build(seg, _bounds(2.4, [(0.1, 1.0), (1.6, 2.3)]), _config())
    assert [p.source for p in plan.phrases] == [[], []]
    assert plan.anchors == []


def test_untimed_words_produce_no_correspondence_rather_than_one_big_run(tmp_path):
    seg = _seg(tmp_path, 1.0, 4.0, words=[{"word": "a"}, {"word": "b"}])
    assert phrases.source_runs(seg, _config()) == []


# -- planning ---------------------------------------------------------------

def test_padding_is_spent_before_any_speech_is_compressed(tmp_path):
    seg = _seg(tmp_path, 0.0, 2.0)
    # 1.7s of speech and 0.4s of padding in a 2.0s window: shrinking the gap
    # alone is enough, so nothing is stretched.
    plan = phrases.plan_pieces(
        phrases.build(seg, _bounds(2.1, [(0.0, 0.85), (1.25, 2.1)]), _config(handle_ms=0)),
        _config(handle_ms=0))
    assert plan.max_stretch == 1.0
    assert plan.moved > 0
    assert plan.planned_duration <= 2.0 * phrases.FIT_SLACK


def test_a_protected_pause_is_kept_and_speech_is_compressed_instead(tmp_path):
    seg = _seg(tmp_path, 0.0, 2.0)
    config = _config(handle_ms=0)
    plan = phrases.plan_pieces(
        phrases.build(seg, _bounds(2.4, [(0.0, 0.85), (1.45, 2.4)]), config), config)
    gap = _gap(plan)
    assert gap.protected and gap.planned == pytest.approx(gap.measured)
    assert plan.max_stretch > 1.0
    assert plan.protected_kept == pytest.approx(0.6, abs=0.01)


def test_a_line_that_already_fits_is_left_completely_alone(tmp_path):
    seg = _seg(tmp_path, 0.0, 4.0)
    config = _config(handle_ms=0)
    plan = phrases.plan_pieces(
        phrases.build(seg, _bounds(2.4, [(0.0, 1.0), (1.6, 2.4)]), config), config)
    assert plan.max_stretch == 1.0 and plan.moved == 0.0
    assert plan.state == "planned"


def test_speech_is_never_slowed_down_to_fill_a_slot(tmp_path):
    seg = _seg(tmp_path, 0.0, 10.0)   # a very generous window
    config = _config(handle_ms=0)
    plan = phrases.plan_pieces(
        phrases.build(seg, _bounds(2.4, [(0.0, 1.0), (1.6, 2.4)]), config), config)
    assert plan.min_stretch == 1.0
    assert plan.planned_duration == pytest.approx(2.4, abs=0.01)


def test_no_phrase_is_ever_dropped_to_make_a_line_fit(tmp_path):
    seg = _seg(tmp_path, 0.0, 0.8)   # far too small for 2.4s of speech
    config = _config(handle_ms=0)
    plan = phrases.plan_pieces(
        phrases.build(seg, _bounds(2.4, [(0.0, 1.0), (1.6, 2.4)]), config), config)
    assert plan.state == "infeasible"
    placed = {piece["phrase"] for piece in plan.pieces if piece["kind"] == "speech"}
    assert placed == {p.phrase_id for p in plan.phrases}
    assert plan.max_stretch <= config["max_stretch"] + 1e-9
    assert any(c["code"] == "phrase_infeasible" for c in plan.conflicts)


def test_the_conflict_names_the_numbers_rather_than_saying_it_failed(tmp_path):
    seg = _seg(tmp_path, 0.0, 0.8)
    config = _config(handle_ms=0)
    plan = phrases.plan_pieces(
        phrases.build(seg, _bounds(2.4, [(0.0, 1.0), (1.6, 2.4)]), config), config)
    detail = next(c["detail"] for c in plan.conflicts if c["code"] == "phrase_infeasible")
    assert "of speech needs" in detail and "is available" in detail


# -- anchors ----------------------------------------------------------------

def test_a_hard_anchor_bounds_the_run_it_sits_between(tmp_path):
    seg = _seg(tmp_path, 0.0, 4.0)
    config = _config(handle_ms=0)
    edits = {"anchors": [{"order": 1, "edge": "start", "at": 2.0}]}
    plan = phrases.plan_pieces(
        phrases.build(seg, _bounds(2.4, [(0.0, 1.0), (1.6, 2.4)]), config, edits), config)
    second = next(p for p in plan.pieces
                  if p["kind"] == "speech" and p["phrase"].endswith(":p1"))
    assert second["at"] == pytest.approx(2.0)
    anchor = next(a for a in plan.anchors if a.kind == "hard")
    assert anchor.observed == pytest.approx(2.0) and abs(anchor.error) < 1e-6


def test_contradictory_hard_anchors_produce_an_actionable_finding(tmp_path):
    seg = _seg(tmp_path, 0.0, 4.0)
    config = _config(handle_ms=0)
    edits = {"anchors": [{"order": 0, "edge": "start", "at": 2.0},
                         {"order": 1, "edge": "start", "at": 0.5}]}
    plan = phrases.plan_pieces(
        phrases.build(seg, _bounds(2.4, [(0.0, 1.0), (1.6, 2.4)]), config, edits), config)
    assert plan.state == "infeasible"
    conflict = next(c for c in plan.conflicts if c["code"] == "anchor_order")
    assert "before" in conflict["detail"]


def test_an_anchor_past_the_end_of_the_window_is_a_conflict_not_a_clamp(tmp_path):
    seg = _seg(tmp_path, 0.0, 2.0)
    config = _config(handle_ms=0)
    edits = {"anchors": [{"order": 1, "edge": "start", "at": 5.0}]}
    plan = phrases.plan_pieces(
        phrases.build(seg, _bounds(2.4, [(0.0, 1.0), (1.6, 2.4)]), config, edits), config)
    assert any(c["code"] == "anchor_outside_slot" for c in plan.conflicts)


def test_an_anchor_for_a_phrase_that_is_gone_is_reported_not_dropped(tmp_path):
    seg = _seg(tmp_path, 0.0, 4.0)
    config = _config(handle_ms=0)
    edits = {"anchors": [{"phrase": "someone-elses:p7", "edge": "start", "at": 1.0}]}
    plan = phrases.build(seg, _bounds(2.4, [(0.0, 1.0), (1.6, 2.4)]), config, edits)
    assert any(c["code"] == "anchor_orphaned" for c in plan.conflicts)


# -- reviewer overrides -----------------------------------------------------

def test_a_reviewer_can_protect_a_gap_the_planner_called_padding(tmp_path):
    seg = _seg(tmp_path, 0.0, 2.0)
    config = _config(handle_ms=0)
    plain = phrases.build(seg, _bounds(2.1, [(0.0, 0.85), (1.25, 2.1)]), config)
    gap = _gap(plain)
    assert not gap.protected
    guarded = phrases.plan_pieces(
        phrases.build(seg, _bounds(2.1, [(0.0, 0.85), (1.25, 2.1)]), config,
                      {"pauses": {gap.pause_id: {"protected": True}}}), config)
    kept = _gap(guarded)
    assert kept.protected and kept.origin == "review"
    assert guarded.moved == 0.0 and guarded.max_stretch > 1.0


def test_a_bypass_makes_no_plan_at_all(tmp_path):
    seg = _seg(tmp_path, 0.0, 2.0)
    plan = phrases.build(seg, _bounds(2.4, [(0.0, 1.0), (1.6, 2.4)]), _config(),
                         {"bypass": True})
    assert plan.state == "bypassed" and plan.bypassed and not plan.pieces


# -- the whole-line fallback ------------------------------------------------

def test_the_fallback_is_a_real_plan_that_says_it_is_a_fallback(tmp_path):
    seg = _seg(tmp_path, 0.0, 1.5)
    config = _config(handle_ms=0)
    plan = phrases.whole_line(seg, _bounds(1.8, [(0.1, 1.75)]), config,
                              "the evidence was not usable")
    assert plan.state == "fallback"
    assert plan.reason.startswith("the evidence was not usable")
    assert len(plan.phrases) == 1 and plan.phrases[0].method == "whole"
    assert plan.max_stretch > 1.0   # it still fits, as one bounded whole


def test_a_fallback_that_still_does_not_fit_keeps_both_facts(tmp_path):
    seg = _seg(tmp_path, 0.0, 0.8)
    config = _config(handle_ms=0)
    plan = phrases.whole_line(seg, _bounds(2.0, [(0.1, 2.0)]), config,
                              "the evidence was not usable")
    assert plan.state == "infeasible"
    assert plan.reason.startswith("the evidence was not usable; ")
    assert "of speech needs" in plan.reason


def test_the_fallback_without_any_measurable_speech_is_unavailable(tmp_path):
    seg = _seg(tmp_path, 0.0, 1.5)
    empty = Bounds(2.0, 200, 48000, -60.0, -58.0, None, None, False, [])
    plan = phrases.whole_line(seg, empty, _config(), "nothing audible")
    assert plan.state == "unavailable" and not plan.pieces


# -- settings ---------------------------------------------------------------

def test_the_default_timing_owner_is_still_whole_clip_fitting():
    assert phrases.settings({})["mode"] == "whole"
    assert not phrases.owns_timing({})
    assert phrases.owns_timing({"mode": "phrase"})


def test_an_unknown_timing_mode_is_refused_rather_than_guessed():
    with pytest.raises(ValueError, match="whole or phrase"):
        phrases.settings({"mode": "sideways"})


def test_text_is_split_at_punctuation_not_at_a_word_count():
    assert phrases.split_text("Uno, dos. Tres") == ["Uno,", "dos.", "Tres"]
    assert phrases.split_text("") == []
    assert phrases.split_text("sin puntuacion alguna aqui") == [
        "sin puntuacion alguna aqui"]


def test_a_target_span_is_on_the_target_timeline(tmp_path):
    seg = _seg(tmp_path, 10.0, 13.0)
    config = _config(handle_ms=0)
    plan = phrases.plan_pieces(
        phrases.build(seg, _bounds(2.4, [(0.2, 1.0), (1.6, 2.4)]), config), config)
    span = phrases.target_span(seg, plan)
    assert span.domain == "target"
    assert span.start == pytest.approx(10.2, abs=0.01)
    assert plan.phrases[0].clip.domain == CLIP
