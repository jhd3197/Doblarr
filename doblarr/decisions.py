"""Small typed decisions about the script: rules first, a model second.

Some questions about a line are answered by its words: is it cut off, is it
whispered, is it a caption rather than dialogue, is it only a laugh. Doblarr
answers them in two layers:

1. **Rules** read the evidence the text actually carries: a trailing dash, a
   `(whispering)` tag, a line in capitals with a year in it. When a rule
   fires, it decides, and the decision says so.
2. **A decision model** (Prompture's typed-decision modality: Laya in-process
   by default, or Kev / TypeSafe) is asked only where the rules are silent. It
   returns a calibrated probability rather than prose. An answer at or above
   `decisions.apply_confidence` is applied; one at or above
   `decisions.suggest_confidence` becomes a review suggestion; anything lower
   is dropped.

The model is optional in every sense. If it is not installed, cannot load, or
fails once, the run carries on with the rules alone and records why. Answers
are cached by model, question and text, so a rerun asks nothing again.

Every decision is recorded with its source (`rules`, `model`) and confidence
in `job.metrics["decisions"]`, and each kind can be switched off on its own.
The model reads text only: nothing here claims to know how a take sounds.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .artifacts import digest, read_json
from .stages.quality import apply_findings

log = logging.getLogger("doblarr.decisions")

# Bump when a question's wording changes: cached answers are keyed by it.
REVISION = "decisions/1"

KINDS = ("cutoffs", "delivery", "title_cards", "reactions", "sound_tags",
         "rewrite_check", "treatments", "review_order")

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "model": "laya/router",
    "apply_confidence": 0.9,
    "suggest_confidence": 0.6,
    **{kind: True for kind in KINDS},
}


def settings(options: dict | None) -> dict:
    """The decision policy from `decisions.*`, with defaults for anything unset."""
    out = {**DEFAULTS, **{k: v for k, v in (options or {}).items() if k in DEFAULTS}}
    out["apply_confidence"] = min(1.0, max(0.0, float(out["apply_confidence"])))
    out["suggest_confidence"] = min(out["apply_confidence"],
                                    max(0.0, float(out["suggest_confidence"])))
    return out


def enabled(config: dict, kind: str) -> bool:
    return bool(config.get("enabled")) and bool(config.get(kind))


def noul(instructions: str) -> dict:
    return {"type": "noul", "instructions": instructions}


def choice(instructions: str, criteria: dict[str, str]) -> dict:
    return {"type": "choice", "instructions": instructions, "criteria": criteria}


def _plain(answer) -> dict:
    """A model answer as a small cacheable dict."""
    kind = getattr(answer, "type", "")
    if kind == "noul":
        return {"type": "noul", "yes": float(answer.noul),
                "confidence": float(answer.confidence)}
    if kind == "choice":
        return {"type": "choice", "choice": str(answer.choice),
                "confidence": float(answer.confidence)}
    return {"type": kind, "value": float(getattr(answer, "score", 0.0)),
            "confidence": float(getattr(answer, "confidence", 0.0))}


class Oracle:
    """The configured decision model, loaded on first use, or nothing at all.

    `factory` builds the driver; tests pass a fake. With no model configured,
    or after one failure, `ask` returns None and the rules decide alone.
    """

    def __init__(self, config: dict | None = None, *, cache_dir: Path | None = None,
                 factory=None, device: str | None = None):
        self.config = settings(config)
        self.model = str(self.config.get("model") or "")
        self.cache_path = (cache_dir / "decisions.cache.json") if cache_dir else None
        self._cache: dict[str, dict] = read_json(self.cache_path) if self.cache_path else {}
        self._dirty = False
        self._factory = factory
        self._device = device
        self._driver: Any = None
        self.state = "idle" if self.model and self.config["enabled"] else "off"
        self.reason = "" if self.state == "idle" else "no decision model configured"
        self.calls = 0

    @property
    def available(self) -> bool:
        return self.state in ("idle", "ready")

    def _load(self) -> bool:
        if self.state == "ready":
            return True
        if self.state != "idle":
            return False
        try:
            if self._factory is not None:
                self._driver = self._factory(self.model)
            else:
                from prompture.drivers.decision_registry import get_decision_driver_for_model

                options: dict[str, Any] = {}
                if self.model.startswith("laya/"):
                    # Load only the checkpoint a line needs, not every one.
                    options["preload"] = False
                    if self._device:
                        options["device"] = self._device
                self._driver = get_decision_driver_for_model(self.model, **options)
        except Exception as exc:  # noqa: BLE001 - optional layer: any failure means rules only
            return self._give_up(f"could not load {self.model}: {exc}")
        self.state = "ready"
        return True

    def _give_up(self, reason: str) -> bool:
        self.state, self.reason, self._driver = "unavailable", reason, None
        log.warning("decisions: %s; continuing with the rules alone", reason)
        return False

    def ask(self, kind: str, state: dict, questions: dict[str, dict]) -> dict[str, dict] | None:
        """Typed answers for one line, from the cache or the model; None without one."""
        if not self.available:
            return None
        key = digest([REVISION, self.model, kind, state, questions])
        if key in self._cache:
            return self._cache[key]
        if not self._load():
            return None
        try:
            response = self._driver.decide(state, questions)
            answers = {qid: _plain(response.answers[qid]) for qid in questions}
        except Exception as exc:  # noqa: BLE001
            self._give_up(f"{self.model} failed: {exc}")
            return None
        self.calls += 1
        self._cache[key] = answers
        self._dirty = True
        return answers

    def close(self, release: bool = True) -> None:
        """Save the answer cache and, if asked, free the model's memory."""
        if self._dirty and self.cache_path is not None:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.cache_path.with_suffix(".partial.json")
            temp.write_text(json.dumps(self._cache, ensure_ascii=False), encoding="utf-8")
            temp.replace(self.cache_path)
            self._dirty = False
        if release and self._driver is not None and hasattr(self._driver, "unload"):
            try:
                self._driver.unload()
            except Exception as exc:  # noqa: BLE001
                log.debug("decisions: unload failed: %s", exc)
            self._driver = None
            if self.state == "ready":
                self.state = "idle"


@dataclass
class Verdict:
    """One decision about one cue, and who made it."""

    kind: str
    cue: str
    value: str
    confidence: float
    source: str            # rules | model
    applied: bool
    note: str = ""


def weigh(config: dict, answer: dict | None, *, value: str | None = None,
          yes: bool = True) -> tuple[str | None, float, str]:
    """(value, confidence, outcome) from a model answer.

    For a yes/no answer, `value` names what a confident yes (or, with
    `yes=False`, a confident no) means. The outcome is `apply`, `suggest` or
    `drop`, from the configured thresholds.
    """
    if not answer:
        return None, 0.0, "drop"
    if answer["type"] == "noul":
        confidence = answer["yes"] if yes else 1.0 - answer["yes"]
        picked = value
    else:
        confidence = answer["confidence"]
        picked = answer.get("choice")
    if confidence >= config["apply_confidence"]:
        return picked, confidence, "apply"
    if confidence >= config["suggest_confidence"]:
        return picked, confidence, "suggest"
    return picked, confidence, "drop"


def record(job, verdicts: list[Verdict], kind: str, oracle: Oracle | None = None,
           segments=None) -> None:
    """Verdicts into the run metrics, and each one onto its cue as a finding.

    Every cue this kind looked at updates its findings, so a decision that no
    longer holds retires instead of staying open.
    """
    summary = job.metrics.setdefault("decisions", {})
    if oracle is not None:
        summary["model"] = oracle.model
        summary["model_state"] = oracle.state if not oracle.reason else \
            f"{oracle.state}: {oracle.reason}"
        summary["model_calls"] = summary.get("model_calls", 0) + oracle.calls
        oracle.calls = 0
    summary[kind] = {
        "applied": sum(v.applied for v in verdicts),
        "suggested": sum(not v.applied for v in verdicts),
        "verdicts": [asdict(v) for v in verdicts],
    }
    by_cue = {v.cue: v for v in verdicts}
    detector = f"decisions/{kind}/1"
    for seg in segments if segments is not None else job.segments:
        verdict = by_cue.get(seg.cue_id)
        observed = []
        if verdict is not None:
            observed.append((f"decision_{kind}", "content", "info", verdict.confidence, {
                "value": verdict.value, "source": verdict.source,
                "applied": verdict.applied, "note": verdict.note}))
        inputs = f"{verdict.value}/{verdict.source}/{verdict.applied}" if verdict else REVISION
        apply_findings(seg, detector, inputs, observed)


def spoken(seg) -> str:
    return (seg.text_translated or seg.text_src or "").strip()


# -- 1. cut-off lines --------------------------------------------------------

_CLOSERS = "\"'”’»」』)] "
_INTERRUPTED = ("—", "–", "--", "-", "―")
_TRAILING = ("...", "…", "‥")
_COMPLETE = tuple(".!?。！？")

CUTOFF_QUESTION = choice(
    "How does this line of dialogue end?",
    {"complete": "a finished sentence or thought",
     "interrupted": "cut off mid-word or mid-sentence, as if someone broke in",
     "trailing": "trailing off, left unfinished on purpose"})


def cutoff_rule(text: str) -> str | None:
    """How a line ends, from its punctuation; None when the words don't say."""
    body = text.rstrip(_CLOSERS)
    if not body:
        return None
    if body.endswith(_TRAILING):
        return "trailing"
    if body.endswith(_INTERRUPTED):
        return "interrupted"
    if body.endswith(_COMPLETE):
        return "complete"
    return None


def cutoffs(job, oracle: Oracle, config: dict) -> dict[str, str]:
    """Which lines end cut off or trailing away, for the edge fades.

    Returns {cue id: "interrupted" | "trailing"} for the decisions that are
    applied. A cut-off line keeps its hard stop; a trailing one gets a longer
    fade. Lines that end in ordinary punctuation are complete and ask nothing.
    """
    if not enabled(config, "cutoffs"):
        return {}
    verdicts, hints = [], {}
    for seg in job.segments:
        text = spoken(seg)
        if not text or not seg.cue_id:
            continue
        ruled = cutoff_rule(text)
        if ruled is not None:
            if ruled != "complete":
                hints[seg.cue_id] = ruled
                verdicts.append(Verdict("cutoffs", seg.cue_id, ruled, 1.0, "rules", True))
            continue
        answers = oracle.ask("cutoffs", {"line": text}, {"ending": CUTOFF_QUESTION})
        value, confidence, outcome = weigh(config, (answers or {}).get("ending"))
        if outcome == "drop" or value is None or value == "complete":
            continue
        applied = outcome == "apply"
        if applied:
            hints[seg.cue_id] = value
        verdicts.append(Verdict("cutoffs", seg.cue_id, value, round(confidence, 3), "model",
                                applied))
    record(job, verdicts, "cutoffs", oracle)
    return hints


# -- 2. delivery mode -------------------------------------------------------

# A stage direction in brackets, as subtitles write them: "(whispering)".
_TAG = re.compile(r"[\[(]([^\[\]()]{2,40})[\])]")
_MODE_WORDS: tuple[tuple[str, re.Pattern], ...] = tuple(
    (mode, re.compile(pattern, re.IGNORECASE)) for mode, pattern in (
        ("whisper", r"\bwhisper\w*|\bsoftly\b|\bquietly\b|under (?:his|her|their|my) breath"
                    r"|\bsusurr\w*|\bmurmur\w*|\bmumbl\w*"),
        ("shout", r"\bshout\w*|\byell\w*|\bscream\w*|\bgrit\w*|\bhollers?\b"),
        ("thought", r"\bthink\w*|\bthought\b|inner voice|\binternal\b|\bpensando\b"),
        ("call", r"\bcall(?:s|ing)? out\b|\bcalling\b|\bllamando\b"),
        ("broadcast", r"over (?:the )?(?:radio|phone|intercom|speaker|pa)\b"
                      r"|on (?:the )?(?:radio|tv|television)\b|\bbroadcast\w*|\bannouncer\b"),
    ))

DELIVERY_QUESTION = choice(
    "How is this line of dialogue delivered?",
    {"normal": "ordinary spoken dialogue",
     "whisper": "whispered or said very quietly",
     "shout": "shouted, yelled or screamed",
     "thought": "an inner thought, not said aloud",
     "call": "called out to someone far away"})


def delivery_rule(text: str) -> str | None:
    """A speech mode the text states outright, else None."""
    for tag in _TAG.findall(text or ""):
        for mode, pattern in _MODE_WORDS:
            if pattern.search(tag):
                return mode
    letters = [ch for ch in text or "" if ch.isalpha() and ch.lower() != ch.upper()]
    if (len(letters) >= 4 and all(ch.isupper() for ch in letters)
            and len(text.split()) >= 2 and "!" in text):
        return "shout"  # a whole line in capitals, exclaimed
    return None


def _may_be_marked(text: str) -> bool:
    """Only lines with some sign of a delivery are worth asking about."""
    return bool(re.search(r"[!¡]|[\[(]", text or ""))


def delivery(job, oracle: Oracle, config: dict) -> int:
    """Set the speech mode on lines whose words say how they are delivered.

    Never touches a line a reviewer, a cast or knowledge gave a mode. A mode
    this layer set before is recomputed each run, so switching it off or
    changing the model takes effect instead of sticking.
    """
    for seg in job.segments:
        if seg.intent.origin == "decision":
            seg.intent.mode, seg.intent.origin = "unknown", "unknown"
    if not enabled(config, "delivery"):
        record(job, [], "delivery")
        return 0
    verdicts = []
    for seg in job.segments:
        if not seg.cue_id or seg.intent.mode not in ("", "unknown") or \
                seg.intent.origin not in ("", "unknown"):
            continue
        source, target = seg.text_src or "", seg.text_translated or ""
        mode = delivery_rule(source) or delivery_rule(target)
        if mode is not None:
            verdict = Verdict("delivery", seg.cue_id, mode, 1.0, "rules", True)
        elif _may_be_marked(source) or _may_be_marked(target):
            answers = oracle.ask("delivery", {"line": source, "translation": target},
                                 {"mode": DELIVERY_QUESTION})
            value, confidence, outcome = weigh(config, (answers or {}).get("mode"))
            if outcome == "drop" or value is None or value == "normal":
                continue
            verdict = Verdict("delivery", seg.cue_id, value, round(confidence, 3), "model",
                              outcome == "apply")
        else:
            continue
        if verdict.applied:
            seg.intent.mode, seg.intent.origin = verdict.value, "decision"
        verdicts.append(verdict)
    record(job, verdicts, "delivery", oracle)
    return sum(v.applied for v in verdicts)


# -- 3. captions and title cards ----------------------------------------------

_YEAR = re.compile(r"\b(1[0-9]{3}|20[0-9]{2})\b")
_CARD_WORDS = re.compile(
    r"\b(?:LATER|EARLIER|AGO|CHAPTER|EPISODE|PART|PROLOGUE|EPILOGUE|THE END|MEANWHILE"
    r"|DESPUÉS|ANTES|CAPÍTULO|EPISODIO|PARTE|MIENTRAS TANTO|FIN)\b")

CARD_QUESTION = noul(
    "Is this text an on-screen caption, sign, location or date card, rather than "
    "something a character says out loud?")


def _cased_upper(text: str) -> bool:
    letters = [ch for ch in text if ch.isalpha() and ch.lower() != ch.upper()]
    return len(letters) >= 3 and all(ch.isupper() for ch in letters)


def card_candidate(text: str) -> bool:
    """Short, in capitals, and not exclaimed or asked: could be a caption."""
    body = text.strip()
    return (bool(body) and _cased_upper(body) and len(body.split()) <= 8
            and not re.search(r"[!?¡¿！？]", body))


def card_rule(text: str) -> bool:
    """A caption the text shows plainly: a date, "3 YEARS LATER", "PARIS, FRANCE"."""
    body = text.strip().rstrip(".")
    if not card_candidate(body):
        return False
    if _YEAR.search(body) or _CARD_WORDS.search(body):
        return True
    parts = [p.strip() for p in body.split(",")]
    return len(parts) == 2 and all(p and len(p.split()) <= 3 for p in parts)


def title_cards(job, oracle: Oracle, config: dict) -> int:
    """Take captions out of the spoken script, keeping each as a timed event.

    A caption is not dialogue: voiced, "NEW YORK, 1987" is an announcer who
    was never in the film. It leaves synthesis the way a non-spoken cue does,
    as an event on the ledger with its original text, so nothing is lost and
    review can see it. Only short lines in capitals that are not exclaimed or
    asked are ever considered.
    """
    from .stages.prepare import _event

    if not enabled(config, "title_cards"):
        return 0
    verdicts, kept, removed = [], [], 0
    known = {event.event_id for event in job.nonverbal}
    for seg in job.segments:
        text = (seg.text_src or "").strip()
        verdict = None
        if seg.cue_id and card_candidate(text):
            if card_rule(text):
                verdict = Verdict("title_cards", seg.cue_id, "caption", 1.0, "rules", True)
            else:
                answers = oracle.ask("title_cards", {"line": text}, {"card": CARD_QUESTION})
                _value, confidence, outcome = weigh(config, (answers or {}).get("card"),
                                                    value="caption")
                if outcome != "drop":
                    verdict = Verdict("title_cards", seg.cue_id, "caption",
                                      round(confidence, 3), "model", outcome == "apply")
        if verdict is not None:
            verdicts.append(verdict)
        if verdict is None or not verdict.applied or not seg.end > seg.start >= 0:
            kept.append(seg)
            continue
        event = _event(seg, text, 0, "whole", text, detected_by=verdict.source)
        event.type, event.category, event.speaker = "unknown", "background", None
        event.reason = "an on-screen caption, not dialogue"
        event.checks["decided"] = "caption"
        if event.event_id not in known:
            job.nonverbal.append(event)
            known.add(event.event_id)
        removed += 1
    job.segments = kept
    job.metrics["captions_removed"] = removed
    record(job, verdicts, "title_cards", oracle)
    return removed


# -- 4. reaction-only cues ------------------------------------------------------

REACTION_QUESTIONS = {
    "reaction": noul("Is this subtitle only a vocal reaction sound (a laugh, gasp, sigh, "
                     "scream or interjection) with no words in it?"),
    "kind": choice("Which reaction sound is it?", {
        "laugh": "laughing or giggling", "gasp": "a sharp breath in, surprise",
        "sigh": "a long breath out", "scream": "a scream or cry out",
        "interjection": "a short sound like oh, eh, huh, tsk"}),
}


def reactions(job, oracle: Oracle, config: dict, interjections: bool = True) -> int:
    """Cues the rules did not know but that are only a reaction sound.

    Asks the model only about cues that the conservative local check
    (`prepare.wordless`) already finds wordless, and converts one only when the
    model is confident too, through the same path the translator's flag uses.
    Without the model this does nothing: the local check alone is not enough.
    """
    from .stages.prepare import from_translator, wordless

    if not interjections or not enabled(config, "reactions"):
        return 0
    flags, verdicts = {}, []
    for seg in job.segments:
        text = (seg.text_src or "").strip()
        if not seg.cue_id or not wordless(text):
            continue
        answers = oracle.ask("reactions", {"line": text}, REACTION_QUESTIONS) or {}
        _value, confidence, outcome = weigh(config, answers.get("reaction"), value="reaction")
        if outcome == "drop":
            continue
        kind = (answers.get("kind") or {}).get("choice") or "interjection"
        applied = outcome == "apply"
        if applied:
            flags[seg.cue_id] = {"delivery": "reaction", "reaction_kind": kind}
        verdicts.append(Verdict("reactions", seg.cue_id, kind, round(confidence, 3), "model",
                                applied))
    record(job, verdicts, "reactions", oracle)
    return from_translator(job, flags, detected_by="model") if flags else 0


# -- 5. unrecognized sound tags --------------------------------------------------

# Words inside a tag that name an event type, checked in order. The parser in
# stages/prepare only accepts a tag that *is* one of its phrases; these find
# the type inside a longer description ("[door creaks open]").
_SOUND_WORDS: tuple[tuple[str, str, re.Pattern], ...] = tuple(
    (kind, category, re.compile(pattern, re.IGNORECASE)) for kind, category, pattern in (
        ("laugh", "vocal", r"\blaugh|\bchuckl|\bgiggl|\bsnicker|\bris[ae]"),
        ("sigh", "vocal", r"\bsigh|\bsuspir"),
        ("gasp", "vocal", r"\bgasp|\bjadea"),
        ("cry", "vocal", r"\bsob|\bcry|\bcries|\bweep|\bwhimper|\bllor"),
        ("scream", "vocal", r"\bscream|\bshriek|\byell|\bshout|\bgrit"),
        ("cough", "vocal", r"\bcough|\btos\b|\btose"),
        ("breath", "vocal", r"\bbreath|\bpant|\bexhal|\binhal|\bsniff|\brespir"),
        ("effort", "vocal", r"\bgrunt|\bgroan|\bstrain|\bmoan|\bgime"),
        ("applause", "background", r"\bapplau|\bclap|\bcheer|\baplaus"),
        ("music", "background", r"\bmusic|\bsong|\bsing|\bmelod|♪|\bmúsica|\bcanci"),
        ("footsteps", "background", r"\bfootstep|\bfootfall|\bpasos\b"),
        ("door", "background", r"\bdoor|\bknock|\bpuerta|\btoca"),
    ))
# Sounds of the world that are no speaker's voice but have no type of their
# own: kept `unknown`, but known to belong to the bed.
_BED_WORDS = re.compile(
    r"\bphone|\bring|\bbuzz|\bbeep|\balarm|\bsiren|\bengine|\bcar\b|\bgun|\bshot|"
    r"\bexplo|\bthunder|\brain\b|\bwind\b|\bdog|\bbark|\bcrash|\bbang|\bglass|\bclock|"
    r"\bbell|\bhorn|\bbird|\bwater|\btraffic|\bteléfono|\bdisparo|\bperro", re.IGNORECASE)

SOUND_QUESTION = choice("What is this subtitle sound tag describing?", {
    "laugh": "laughing", "sigh": "sighing", "gasp": "a gasp", "cry": "crying or sobbing",
    "scream": "screaming or shouting", "cough": "coughing", "breath": "breathing",
    "effort": "a grunt, groan or effort sound", "applause": "applause or cheering",
    "music": "music or singing", "footsteps": "footsteps", "door": "a door or knocking",
    "bed": "some other sound of the scene, not a person's voice",
    "voice": "some other sound a person makes"})


def sound_rule(text: str) -> tuple[str, str] | None:
    """(type, category) the tag's own words name, else None."""
    for kind, category, pattern in _SOUND_WORDS:
        if pattern.search(text):
            return kind, category
    if _BED_WORDS.search(text):
        return "unknown", "background"
    return None


_WHOLE_TAG = re.compile(r"^\s*[\[(][^\[\]()]{1,60}[\])]\s*$")


def _name_tag(text: str, oracle: Oracle, config: dict):
    """(type, category, confidence, source, applied) for a tag, or None."""
    ruled = sound_rule(text)
    if ruled is not None:
        return (*ruled, 1.0, "rules", True)
    answers = oracle.ask("sound_tags", {"tag": text}, {"sound": SOUND_QUESTION})
    value, confidence, outcome = weigh(config, (answers or {}).get("sound"))
    if outcome == "drop" or value is None:
        return None
    kind, category = ("unknown", "background") if value == "bed" else \
        ("unknown", "vocal") if value == "voice" else \
        next(((k, c) for k, c, _p in _SOUND_WORDS if k == value), ("unknown", "unknown"))
    return kind, category, round(confidence, 3), "model", outcome == "apply"


def sound_tags(job, oracle: Oracle, config: dict) -> int:
    """Name the sound tags the parser did not recognise.

    Two places: a cue that is nothing but a tag the parser did not know
    (`[door creaks open]`) would otherwise be spoken as dialogue; once its
    sound is named it leaves synthesis as an event, like a known tag. And an
    event already on the ledger as `unknown` gets its type. Only type and
    category change: the original text stays, the decision stays
    `unresolved`, and nothing is inserted. Captions are left alone.
    """
    from .stages.prepare import _event

    if not enabled(config, "sound_tags"):
        return 0
    verdicts, named, kept = [], 0, []
    known = {event.event_id for event in job.nonverbal}
    for seg in job.segments:
        text = (seg.text_src or "").strip()
        named_tag = (_name_tag(text, oracle, config)
                     if seg.cue_id and _WHOLE_TAG.match(text) and seg.end > seg.start >= 0
                     else None)
        if named_tag is None:
            kept.append(seg)
            continue
        kind, category, confidence, source, applied = named_tag
        verdicts.append(Verdict("sound_tags", seg.cue_id, f"{kind}/{category}", confidence,
                                source, applied))
        if not applied:
            kept.append(seg)
            continue
        event = _event(seg, text, 0, "whole", text, detected_by=source)
        event.type, event.category = kind, category
        event.speaker = seg.speaker if category == "vocal" else None
        event.checks["classified_by"] = source
        if event.event_id not in known:
            job.nonverbal.append(event)
            known.add(event.event_id)
        named += 1
    job.segments = kept
    for event in job.nonverbal:
        if event.type != "unknown" or event.category != "unknown" or \
                event.checks.get("decided") == "caption" or event.origin == "manual":
            continue
        named_event = _name_tag((event.text or "").strip(), oracle, config)
        if named_event is None:
            continue
        kind, category, confidence, source, applied = named_event
        if applied:
            event.type, event.category = kind, category
            event.checks["classified_by"] = source
            if category == "vocal" and event.speaker is None:
                seg = next((s for s in job.segments if s.cue_id == event.cue_id), None)
                event.speaker = seg.speaker if seg is not None else None
            named += 1
        verdicts.append(Verdict("sound_tags", event.event_id, f"{kind}/{category}",
                                confidence, source, applied))
    record(job, verdicts, "sound_tags", oracle)
    return named


# -- 6. timing rewrites ------------------------------------------------------------

_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")
_NAME = re.compile(r"(?<![.!?¿¡:;]\s)(?<!^)\b([A-ZÁÉÍÓÚÑÜ][\w'’-]+)")

REWRITE_QUESTION = noul("Does the shorter line keep the meaning of the original line, "
                        "with no key fact, name or number lost?")


def rewrite_rule(original: str, shorter: str) -> str | None:
    """Why a rewrite must be rejected on its words alone, or None."""
    kept = shorter.casefold()
    for number in _NUMBER.findall(original):
        if number not in shorter:
            return f"dropped the number {number}"
    for name in _NAME.findall(original.strip()):
        if name.startswith("I'") or name.startswith("I’") or name.upper() == "OK":
            continue  # a capital that is grammar, not a name
        if name.casefold() not in kept:
            return f"dropped the name {name}"
    return None


def rewrite_checker(job, oracle: Oracle, config: dict):
    """A check for timing repairs, or None when the decision is off.

    Called with (cue, original, shorter) after the translator shortened a line
    and before the line is regenerated. A rewrite that dropped a number or a
    name is rejected by rule; otherwise one the model is confident changed
    the meaning is rejected. Rejecting saves the regeneration, and the line
    keeps its original words and simply stays compressed or over.
    """
    if not enabled(config, "rewrite_check"):
        return None
    verdicts: list[Verdict] = []

    def accept(seg, original: str, shorter: str) -> bool:
        reason = rewrite_rule(original, shorter)
        if reason is not None:
            verdict = Verdict("rewrite_check", seg.cue_id, "rejected", 1.0, "rules", True,
                              f"{reason}: {shorter}")
        else:
            answers = oracle.ask("rewrite_check", {"original": original, "shorter": shorter},
                                 {"same": REWRITE_QUESTION})
            _value, confidence, outcome = weigh(config, (answers or {}).get("same"),
                                                value="rejected", yes=False)
            if outcome == "drop":
                return True
            verdict = Verdict("rewrite_check", seg.cue_id, "rejected", round(confidence, 3),
                              "model", outcome == "apply", f"meaning may have changed: {shorter}")
        verdicts[:] = [v for v in verdicts if v.cue != seg.cue_id] + [verdict]
        record(job, verdicts, "rewrite_check", oracle)
        if verdict.applied:
            job.metrics["timing_rewrites_rejected"] = (
                job.metrics.get("timing_rewrites_rejected", 0) + 1)
            log.info("line %s: timing rewrite rejected (%s)", seg.index, verdict.note)
        return not verdict.applied

    return accept
