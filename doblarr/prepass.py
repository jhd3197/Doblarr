"""One bounded look at the whole episode before translating it (optional).

`translate.prepass` asks the translation model, once per window of the
script, for a short synopsis and (with `summary_terms`) for names, places and
terms it should keep consistent. When the transcript came from speech
recognition it also asks for cues that look misheard. It costs provider calls,
so it is off by default, charged to the shared request budget, and cached by
the script's content so a rerun does not pay again.

What comes back is never trusted as fact:

- the **summary** reaches the translator for this job only, marked as
  machine-written background, and is never saved as title knowledge;
- **terms** are candidates. They are recorded for review and never enter the
  glossary on their own; the configured glossary and accepted knowledge win
  over any of them;
- **corrections** become review findings. The source text is never changed.

The subtitle text is untrusted data in these prompts, exactly as it is in
translation.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .artifacts import digest, read_json
from .errors import JobCancelled
from .stages.quality import apply_findings

log = logging.getLogger("doblarr.prepass")

MODES = ("off", "summary", "summary_terms")
# Bump when the prompts or the schema change: cached results are keyed by it.
PROMPT_REVISION = "prepass/1"
DETECTOR = "prepass/1"
# One request never carries more script than this; a longer script is read in
# windows whose summaries are then merged, also within this bound.
WINDOW_CHARS = 12000
SUMMARY_CHARS = 600


class Term(BaseModel):
    model_config = ConfigDict(extra="ignore")

    source: str = Field(min_length=1, max_length=120)
    target: str = Field(min_length=1, max_length=120)
    kind: Literal["name", "place", "term"] = "term"
    confidence: float = Field(default=0.5, ge=0, le=1)
    cue_ids: list[str] = []


class Correction(BaseModel):
    model_config = ConfigDict(extra="ignore")

    cue_id: str = Field(min_length=1)
    heard: str = Field(min_length=1, max_length=300)
    suggested: str = Field(min_length=1, max_length=300)
    reason: str = Field(default="", max_length=300)


class Analysis(BaseModel):
    model_config = ConfigDict(extra="ignore")

    summary: str = Field(default="", max_length=SUMMARY_CHARS * 2)
    terms: list[Term] = []
    corrections: list[Correction] = []


def windows(cues: list[dict], cap: int | None = None) -> list[list[dict]]:
    """Consecutive cues packed into windows of at most `cap` characters."""
    cap = WINDOW_CHARS if cap is None else cap
    out: list[list[dict]] = []
    size = 0
    for cue in cues:
        length = len(json.dumps(cue, ensure_ascii=False))
        if out and size + length <= cap:
            out[-1].append(cue)
            size += length
        else:
            out.append([cue])
            size = length
    return out


def _groups(summaries: list[str], cap: int) -> list[list[str]]:
    out: list[list[str]] = []
    size = 0
    for text in summaries:
        if out and size + len(text) <= cap:
            out[-1].append(text)
            size += len(text)
        else:
            out.append([text])
            size = len(text)
    return out


def cache_key(script: list[dict], translator, mode: str, corrections: bool,
              source_lang: str, target: str) -> str:
    return digest([PROMPT_REVISION, mode, corrections, source_lang, target,
                   getattr(translator, "model", type(translator).__name__),
                   getattr(translator, "endpoint", None), script])


def analyze(job, translator, mode: str, *, work_dir: Path, budget=None, cancel=None,
            corrections: bool = False, title: str = "") -> dict | None:
    """Run (or reuse) the prep pass; the result dict, or None when it is off."""
    if mode not in MODES:
        raise ValueError(f"translate.prepass must be one of {', '.join(MODES)}")
    if mode == "off" or not job.segments:
        return None
    if not hasattr(translator, "analyze_script"):
        job.metrics["prepass"] = {"mode": mode, "state": "unsupported",
                                  "reason": "the translation provider cannot run it"}
        return None
    script = [{"id": s.cue_id or str(s.index), "speaker": s.speaker, "text": s.text_src}
              for s in job.segments if (s.text_src or "").strip()]
    source_lang = job.script_lang or job.source_lang
    target = job.target_locale or job.target_lang
    key = cache_key(script, translator, mode, corrections, source_lang, target)
    cache = work_dir / "prepass" / f"{key[:24]}.json"
    saved = read_json(cache)
    if saved.get("key") == key and saved.get("state") == "complete":
        result = saved
        job.metrics["prepass_cache_hit"] = True
    else:
        from .clients.translator import TranslationError

        try:
            result = _run(job, translator, mode, script, source_lang, target, budget, cancel,
                          corrections, title)
        except TranslationError as exc:
            # Optional context: without it the translation runs as it always did.
            log.warning("prep pass failed, translating without it: %s", exc)
            job.metrics["prepass"] = {"mode": mode, "state": "failed", "reason": str(exc)}
            return None
        result["key"] = key
        if result["state"] == "complete":
            cache.parent.mkdir(parents=True, exist_ok=True)
            temp = cache.with_suffix(".partial.json")
            temp.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
            temp.replace(cache)
    return result


def _charge(budget) -> bool:
    return budget is None or budget.charge("prepass")


def _run(job, translator, mode, script, source_lang, target, budget, cancel,
         corrections, title) -> dict:
    want_terms = mode == "summary_terms"
    parts: list[Analysis] = []
    calls = 0
    state = "complete"
    for window in windows(script):
        if cancel is not None and cancel.is_set():
            raise JobCancelled("cancelled during the translation prep pass")
        if not _charge(budget):
            state = "budget_exhausted"
            break
        calls += 1
        parts.append(_validated(translator.analyze_script(
            window, source_lang, target, want_terms=want_terms,
            want_corrections=corrections, title=title)))
    summaries = [p.summary.strip() for p in parts if p.summary.strip()]
    # Merge window summaries hierarchically, each request within the bound.
    # Every summary is short, so each round at least halves the count.
    while len(summaries) > 1 and state == "complete":
        merged: list[str] = []
        for group in _groups(summaries, WINDOW_CHARS):
            if len(group) > 1 and _charge(budget):
                calls += 1
                group = [_validated(translator.analyze_script(
                    [], source_lang, target, want_terms=False, want_corrections=False,
                    title=title, summaries=group)).summary.strip()]
            elif len(group) > 1:
                state = "budget_exhausted"
            merged.extend(text for text in group if text)
        if len(merged) >= len(summaries):
            break  # nothing merged this round; never loop on it
        summaries = merged
    if len(summaries) > 1:
        summaries = [" ".join(summaries)]
    terms: dict[str, dict] = {}
    for part in parts:
        for term in part.terms if want_terms else []:
            known = terms.get(term.source.casefold())
            row = term.model_dump()
            if known is None or row["confidence"] > known["confidence"]:
                if known is not None:
                    row["cue_ids"] = sorted({*known["cue_ids"], *row["cue_ids"]})
                terms[term.source.casefold()] = row
            else:
                known["cue_ids"] = sorted({*known["cue_ids"], *row["cue_ids"]})
    fixes = [c.model_dump() for part in parts for c in part.corrections] if corrections else []
    return {"mode": mode, "state": state, "revision": PROMPT_REVISION, "calls": calls,
            "windows": len(windows(script)),
            "summary": (summaries[0] if summaries else "")[:SUMMARY_CHARS],
            "terms": list(terms.values()), "corrections": fixes}


def _validated(reply) -> Analysis:
    try:
        return reply if isinstance(reply, Analysis) else Analysis.model_validate(reply)
    except ValidationError as exc:
        log.warning("prep pass: discarding a malformed reply (%s)", exc)
        return Analysis()


def apply(job, result: dict | None, glossary: dict | None) -> str | None:
    """Record the result for review; return the synopsis for this job's translation.

    Term candidates are compared with the glossary the translation will use:
    where the glossary already decides a term, it wins and the candidate says
    so. Nothing here changes the glossary or any source text.
    """
    if not result:
        return None
    # TODO(prepass): route term candidates into the inactive title-draft system
    # (knowledge/title_drafts.py) once drafts can carry a job-derived source
    # identity and localized proposals without an accepted shared revision.
    decided = {k.casefold(): v for k, v in (glossary or {}).items()}
    candidates = []
    for term in result.get("terms", []):
        row = dict(term)
        chosen = decided.get(row["source"].casefold())
        row["status"] = ("superseded" if chosen is not None and chosen != row["target"]
                         else "confirmed" if chosen is not None else "candidate")
        if chosen is not None:
            row["glossary"] = chosen
        candidates.append(row)
    by_id = {(s.cue_id or str(s.index)): s for s in job.segments}
    flagged = set()
    for fix in result.get("corrections", []):
        seg = by_id.get(fix["cue_id"])
        if seg is None or fix["cue_id"] in flagged:
            continue
        flagged.add(fix["cue_id"])
        apply_findings(seg, DETECTOR, result.get("key", "")[:16], [(
            "source_suspect", "content", "info", None,
            {"heard": fix["heard"], "suggested": fix["suggested"], "reason": fix["reason"],
             "generated": True})])
    for seg in job.segments:
        if (seg.cue_id or str(seg.index)) not in flagged:
            apply_findings(seg, DETECTOR, result.get("key", "")[:16], [])
    job.metrics["prepass"] = {
        "mode": result["mode"], "state": result["state"], "calls": result.get("calls", 0),
        "windows": result.get("windows", 0), "summary": result.get("summary", ""),
        "summary_generated": True, "terms": candidates,
        "corrections": len(flagged),
    }
    return result.get("summary") or None
