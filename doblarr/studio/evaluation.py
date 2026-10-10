"""Judge frozen candidates blind, then reveal — and keep the order of events honest.

1. An evaluation session is built from frozen variants only, with a blind label
   mapping fixed for the session.
2. Judgments are recorded per unit and dimension, with same / neither /
   uncertain / not-applicable as real answers.
3. Revealing the labels, or the held-out official adaptation, is an event with
   a time. A judgment made after a reveal says so.
4. An edit to a candidate is a new revision. Once the official adaptation has
   been revealed, every later revision is *assisted*: it may be better, but it
   is no longer a clean result, and the clean one stays where it was.

The summary counts what people answered. It does not turn a handful of
excerpts into a statistical claim, and it reports a mixed result as mixed.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..artifacts import digest
from ..cues import now
from ..errors import DoblarrError
from . import records
from .sources import read_utterances

DIMENSIONS = ("source_meaning", "character_consistency", "regional_naturalness", "humor",
              "visual_consistency", "timing", "listening_preference")
NEUTRAL = ("same", "neither", "uncertain", "n/a")


class EvaluationError(DoblarrError):
    http_status = 409


def blind_labels(variant_ids: list[str], seed: str) -> dict:
    """Stable neutral labels for one session; the order is not the condition order.

    Numbers, not letters: the conditions are already called A, B and C, and a
    blind label that could be read as a condition name is not blind.
    """
    order = sorted(variant_ids, key=lambda v: digest({"seed": seed, "variant": v}))
    return {str(i + 1): v for i, v in enumerate(order)}


class JudgmentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    unit_id: str = Field(min_length=1, max_length=64)
    dimension: Literal["source_meaning", "character_consistency", "regional_naturalness",
                       "humor", "visual_consistency", "timing", "listening_preference"]
    choice: str = Field(min_length=1, max_length=16)   # a blind label or a neutral answer
    critical: list[str] = Field(default_factory=list, max_length=8)  # labels with a
    # critical meaning regression in this unit
    note: str = Field(default="", max_length=2000)
    reviewer: str = Field(default="", max_length=100)
    source_competent: bool = False   # the reviewer reads the source language

    @field_validator("choice")
    @classmethod
    def plain(cls, value):
        return value.strip()


class RevisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1, max_length=4)
    unit_id: str = Field(min_length=1, max_length=64)
    slot_id: str = Field(min_length=1, max_length=64)
    text: str = Field(min_length=1, max_length=2000)
    reason: str = Field(default="", max_length=1000)
    editing_seconds: float | None = Field(default=None, ge=0, le=36000)
    reviewer: str = Field(default="", max_length=100)


def create(db, experiment: dict, name: str = "") -> dict:
    variants = [v for v in records.list_latest(db, "variant", experiment["scope"])
                if v.get("experiment") == experiment["id"]]
    frozen = [v for v in variants if v.get("frozen")]
    if not frozen:
        raise EvaluationError("no finished, frozen variant to judge yet")
    session_id = "eval-" + digest([experiment["id"], name or "default"])[:12]
    labels = blind_labels([v["id"] for v in frozen], session_id)
    return records.put(db, "evaluation", session_id, {
        "experiment": experiment["id"], "name": name or "Evaluation",
        "labels": labels,
        "variant_revisions": {v["id"]: v["revision"] for v in frozen},
        "judgments": [], "revisions": [], "events": [{"kind": "created", "at": now()}],
        "revealed": {"labels": False, "holdout": False},
    }, scope=experiment["scope"], create_only=True)


def view(db, session: dict) -> dict:
    """The blind view: labels, never condition names until the labels are revealed."""
    rows: dict[str, dict] = {}
    for label, variant_id in session["labels"].items():
        variant = records.get(db, "variant", variant_id,
                              session["variant_revisions"].get(variant_id))
        if variant is None:
            continue
        for unit_id, result in (variant.get("outputs") or {}).items():
            unit = rows.setdefault(unit_id, {"unit_id": unit_id, "candidates": {}})
            unit["candidates"][label] = result["lines"]
    manifest_units: dict[str, dict] = {}
    for variant_id in session["labels"].values():
        variant = records.get(db, "variant", variant_id) or {}
        for unit in (variant.get("manifest") or {}).get("units", []):
            manifest_units.setdefault(unit["unit_id"], unit)
    revealed = session.get("revealed") or {}
    mapping = ({label: _condition(db, vid) for label, vid in session["labels"].items()}
               if revealed.get("labels") else None)
    return {"id": session["id"], "revision": session["revision"],
            "experiment": session["experiment"], "labels": sorted(session["labels"]),
            "mapping": mapping, "revealed": revealed,
            "units": [{**rows[k], "slots": manifest_units.get(k, {}).get("slots", [])}
                      for k in sorted(rows)],
            "judgments": session.get("judgments") or [],
            "revisions": session.get("revisions") or [],
            "events": session.get("events") or []}


def _condition(db, variant_id: str) -> str:
    variant = records.get(db, "variant", variant_id) or {}
    return f"{variant.get('condition', '?')} (repeat {variant.get('repeat', 1)})"


def judge(db, session: dict, body: JudgmentIn, base_revision: int) -> dict:
    labels = set(session["labels"])
    if body.choice not in labels and body.choice not in NEUTRAL:
        raise EvaluationError("a judgment picks a blind label or same / neither / "
                              "uncertain / n/a")
    if set(body.critical) - labels:
        raise EvaluationError("a critical regression must name a blind label")
    revealed = session.get("revealed") or {}
    entry = {**body.model_dump(), "at": now(),
             "after_label_reveal": bool(revealed.get("labels")),
             "after_holdout_reveal": bool(revealed.get("holdout"))}
    judgments = [j for j in session.get("judgments") or []
                 if not (j["unit_id"] == body.unit_id and j["dimension"] == body.dimension
                         and j.get("reviewer", "") == body.reviewer)]
    judgments.append(entry)
    return records.put(db, "evaluation", session["id"], {**session, "judgments": judgments},
                       base_revision=base_revision)


def reveal(db, session: dict, what: str, actor: str, base_revision: int) -> dict:
    if what not in ("labels", "holdout"):
        raise EvaluationError("reveal the blind labels or the held-out adaptation")
    revealed = dict(session.get("revealed") or {})
    events = list(session.get("events") or [])
    if not revealed.get(what):
        revealed[what] = True
        events.append({"kind": f"reveal_{what}", "at": now(), "actor": actor})
    return records.put(db, "evaluation", session["id"],
                       {**session, "revealed": revealed, "events": events},
                       base_revision=base_revision)


def holdout_lines(db, session: dict, experiment: dict, alignments: dict) -> dict:
    """The official adaptation for each unit — only after the reveal was recorded."""
    if not (session.get("revealed") or {}).get("holdout"):
        raise EvaluationError("record the reveal first; the held-out adaptation is only "
                              "shown after candidates are frozen and the reveal is logged")
    out: dict[str, dict] = {}
    for ref_id in experiment.get("evaluation") or []:
        reference = records.get(db, "reference", ref_id)
        if reference is None:
            continue
        rows = {u["utt_id"]: u for u in read_utterances(reference)}
        body = alignments.get(ref_id)
        if not body:
            out[ref_id] = {"label": reference["label"], "units": {},
                           "note": "no alignment for this adaptation; nothing to show per unit"}
            continue
        by_source: dict[str, list[str]] = {}
        for group in body.get("groups", []):
            if group.get("state") not in ("matched", "uncertain"):
                continue
            for sid in group["source"]:
                by_source.setdefault(sid, []).extend(group["reference"])
        units = {}
        for variant_id in session["labels"].values():
            variant = records.get(db, "variant", variant_id) or {}
            for unit in (variant.get("manifest") or {}).get("units", []):
                ids: list[str] = []
                for slot in unit["slots"]:
                    ids.extend(i for i in by_source.get(slot, []) if i not in ids)
                units[unit["unit_id"]] = " ".join(rows[i]["text"] for i in ids if i in rows)
        out[ref_id] = {"label": reference["label"], "units": units}
    return out


def revise(db, session: dict, body: RevisionIn, base_revision: int) -> dict:
    """An edited candidate line. After a holdout reveal it is an assisted revision."""
    if body.label not in session["labels"]:
        raise EvaluationError("revise a candidate by its blind label")
    assisted = bool((session.get("revealed") or {}).get("holdout"))
    entry = {**body.model_dump(), "variant": session["labels"][body.label],
             "at": now(), "assisted": assisted,
             "note": ("made after the official adaptation was revealed — assisted, not a "
                      "clean result" if assisted else "human correction before any reveal")}
    revisions = list(session.get("revisions") or []) + [entry]
    return records.put(db, "evaluation", session["id"], {**session, "revisions": revisions},
                       base_revision=base_revision)


def summarize(db, session: dict) -> dict:
    """What the answers say, per condition, without inventing significance."""
    conditions = {label: (records.get(db, "variant", vid) or {}).get("condition", "?")
                  for label, vid in session["labels"].items()}
    per: dict[str, dict] = {c: {"preferred": {}, "critical": 0, "corrections": 0,
                                "assisted_revisions": 0, "editing_seconds": 0.0}
                            for c in set(conditions.values())}
    neutral: dict[str, int] = {n: 0 for n in NEUTRAL}
    clean_judgments = 0
    for j in session.get("judgments") or []:
        if not j.get("after_label_reveal") and not j.get("after_holdout_reveal"):
            clean_judgments += 1
        if j["choice"] in conditions:
            bucket = per[conditions[j["choice"]]]["preferred"]
            bucket[j["dimension"]] = bucket.get(j["dimension"], 0) + 1
        elif j["choice"] in neutral:
            neutral[j["choice"]] += 1
        for label in j.get("critical") or []:
            per[conditions[label]]["critical"] += 1
    for r in session.get("revisions") or []:
        key = "assisted_revisions" if r.get("assisted") else "corrections"
        per[conditions[r["label"]]][key] += 1
        per[conditions[r["label"]]]["editing_seconds"] += float(r.get("editing_seconds") or 0)
    usage: dict[str, dict] = {}
    for label, vid in session["labels"].items():
        variant = records.get(db, "variant", vid) or {}
        cond = conditions[label]
        u = variant.get("usage") or {}
        row = usage.setdefault(cond, {"provider_calls": 0, "seconds": 0.0, "cost": 0.0,
                                      "cost_known": True})
        row["provider_calls"] += int(u.get("provider_calls") or 0)
        row["seconds"] += float(u.get("seconds") or 0)
        if u.get("cost") is None:
            row["cost_known"] = False
        else:
            row["cost"] += float(u["cost"])
    for row in usage.values():
        if not row["cost_known"]:
            row["cost"] = None
    units = {j["unit_id"] for j in session.get("judgments") or []}
    gate = _gate(per, len(units))
    return {"conditions": per, "neutral": neutral, "usage": usage,
            "judged_units": len(units), "clean_judgments": clean_judgments,
            "gate": gate,
            "competent_source_review": any(j.get("source_competent")
                                           for j in session.get("judgments") or []
                                           if j["dimension"] == "source_meaning"),
            "note": ("Counts of human answers on a few excerpts. They cannot show that a "
                     "condition is better in general, and source fidelity needs a reviewer "
                     "who reads the source language.")}


def _gate(per: dict, judged_units: int) -> dict:
    """The predeclared pilot gate for C against A, stated as what was observed."""
    a, c = per.get("A"), per.get("C")
    if not a or not c or judged_units < 3:
        return {"state": "insufficient", "reason": "fewer than three judged units, or A or C "
                "was not judged"}
    if c["critical"]:
        return {"state": "not passed", "reason": f"C has {c['critical']} critical meaning "
                "regression(s)"}
    c_pref = sum(c["preferred"].values())
    a_pref = sum(a["preferred"].values())
    fewer_corrections = c["corrections"] < a["corrections"]
    if c_pref > a_pref or fewer_corrections:
        return {"state": "passed for this pilot",
                "reason": ("no critical regressions; C was preferred more often"
                           if c_pref > a_pref else "no critical regressions; C needed fewer "
                           "corrections") + ". Added requests and time are listed under usage."}
    if c_pref == a_pref:
        return {"state": "mixed", "reason": "C and A were preferred equally often"}
    return {"state": "not passed", "reason": "A was preferred more often than C"}


def export(db, session: dict, experiment: dict) -> dict:
    """A filled results document: judgments, reveal history, usage and limitations."""
    return {"experiment": {k: experiment.get(k) for k in (
                "id", "name", "conditions", "policy", "target_locale", "decision_criteria",
                "excerpts", "prompt_revision", "limitations")},
            "evaluation": view(db, session), "summary": summarize(db, session)}
