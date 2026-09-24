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
        if outcome == "drop" or value in (None, "complete"):
            continue
        applied = outcome == "apply"
        if applied:
            hints[seg.cue_id] = value
        verdicts.append(Verdict("cutoffs", seg.cue_id, value, round(confidence, 3), "model",
                                applied))
    record(job, verdicts, "cutoffs", oracle)
    return hints
