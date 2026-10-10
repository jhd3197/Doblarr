"""Which envelope templates suit a line: retrieval over measured patterns.

Four kinds of similarity exist in this project and they are kept apart:

- **speaker identity** (voice vectors, doblarr.speakers) says *who*; it is used
  here only as an eligibility filter (a character's preferences and examples),
  never as a score of how a line should move;
- **visual identity** is not used here at all;
- **curve similarity** compares shapes: the original actor's measured energy
  curve, the generated take's curve and a template's shape, as 32-point
  relative-dB curves aligned with a constrained warp (a Sakoe-Chiba band), so
  a translated line of a different length is compared shape to shape without
  being forced syllable to syllable;
- **semantic similarity** compares tags (speech mode, level band, delivery
  traits, scene intent words) as sets.

Order of work, always: hard eligibility filters first (source role and held-out
data, scope, timeline, language, capability and evidence, manual exclusions),
then ranking. Every candidate carries its score components, supporting and
conflicting evidence and a parameter proposal. ``voice/preserve`` is always a
candidate. Examples come only from approved feedback (doblarr.feedback); a
recommendation never becomes evidence for itself.
"""

from __future__ import annotations

import json
import math
import time
from pathlib import Path

from . import features, templates
from .artifacts import digest
from .fileio import write_json

INDEX_VERSION = "patterns/1"
BAND = 0.15               # warping band, share of the curve length
WEIGHTS = {"source_shape": 0.40, "semantic": 0.20, "examples": 0.25, "preference": 0.15}
MAX_CANDIDATES = 4


def namespace() -> str:
    """Index namespace: changes whenever the feature or index definition does."""
    return digest([INDEX_VERSION, features.FEATURES, templates.CATALOGUE])[:12]


def _z(curve: list[float]) -> list[float]:
    if not curve:
        return []
    mean = sum(curve) / len(curve)
    spread = math.sqrt(sum((c - mean) ** 2 for c in curve) / len(curve))
    return [(c - mean) / spread for c in curve] if spread > 1e-6 else [0.0] * len(curve)


def dtw(a: list[float], b: list[float], band: float = BAND) -> float:
    """Banded DTW distance between two equal-rate curves, normalised by length."""
    n, m = len(a), len(b)
    if not n or not m:
        return float("inf")
    width = max(1, int(round(band * max(n, m))))
    inf = float("inf")
    cost = [[inf] * (m + 1) for _ in range(n + 1)]
    cost[0][0] = 0.0
    for i in range(1, n + 1):
        centre = int(round(i * m / n))
        for j in range(max(1, centre - width), min(m, centre + width) + 1):
            d = abs(a[i - 1] - b[j - 1])
            cost[i][j] = d + min(cost[i - 1][j], cost[i][j - 1], cost[i - 1][j - 1])
    return cost[n][m] / (n + m)


def shape_similarity(a: list[float], b: list[float]) -> float | None:
    """0..1 similarity of two shapes (z-normalised, banded warp); None if unknown."""
    if not a or not b:
        return None
    za, zb = _z(a), _z(b)
    if not any(za) or not any(zb):
        return 0.5 if not any(za) and not any(zb) else 0.3
    return round(math.exp(-dtw(za, zb) / 0.6), 3)


def template_shape(template: dict, take: dict | None = None) -> list[float]:
    """A template's expected 32-point shape (flat for families that follow the take)."""
    family = template["family"]
    if family == "curve":
        from .envelopes import template_curve

        return [y for _x, y in template_curve(template, 32)]
    if family == "peak_linked" and take and take.get("peaks"):
        curve = [0.0] * 32
        for peak in take["peaks"][:int((template.get("params") or {}).get(
                "max_peaks", {}).get("default", 4))]:
            k = min(31, max(0, int(round(float(peak.get("position", 0)) * 31))))
            curve[k] = 2.0
        return curve
    return [0.0] * 32


def line_tags(line: dict) -> set[str]:
    """Semantic tags of a line from what is recorded about it."""
    tags = set()
    for key in ("mode", "band", "ending", "screen"):
        value = str(line.get(key) or "")
        if value and value not in ("unknown", "normal", "calm", "unmeasured"):
            tags.add(value)
    tags |= {str(t) for t in line.get("traits") or []}
    tags |= {str(t) for t in line.get("intent_words") or []}
    ending = line.get("ending")
    if ending == "interrupted":
        tags.add("intentional_cutoff")
    if ending == "trailing":
        tags.add("trailing")
    if line.get("band") == "intense":
        tags.add("shout")
    if line.get("band") == "quiet":
        tags.add("whisper")
    return tags


def eligible(template: dict, line: dict, take: dict, profile: dict | None) -> tuple[bool, str]:
    """Hard filters: capability, evidence, contraindications, manual exclusions."""
    if template["kind"] != "voice":
        return False, "not a voice template"
    duration = float(take.get("duration") or line.get("duration") or 0.0)
    ok, why = templates.applicable(template, duration=duration,
                                   quality=str(take.get("quality") or "missing"),
                                   active_seconds=float(take.get("active_seconds") or 0.0),
                                   tags=line_tags(line))
    if not ok:
        return False, why
    for pref in ((profile or {}).get("templates") or {}).get("discouraged") or []:
        if pref.get("template") == template["id"] and not pref.get("conditions"):
            return False, "discouraged for this character"
    if template["id"] in set(line.get("excluded_templates") or []):
        return False, "excluded for this line by hand"
    return True, ""


def example_eligible(example: dict, query: dict) -> tuple[bool, str]:
    """Approved examples pass role, holdout, scope, timeline and language filters."""
    if example.get("holdout") or example.get("retired"):
        return False, "held out or retired"
    if example.get("revision_id") in set(query.get("held_out") or ()):
        return False, "from a held-out revision"
    if example.get("revision_id") == query.get("revision_id") and \
            example.get("cue") == query.get("cue"):
        return False, "the line itself"
    if example.get("series_id") and query.get("series_id") and \
            example["series_id"] != query["series_id"] and example.get("scope") != "global":
        return False, "another series, not shared"
    order, mine = example.get("order"), query.get("order")
    if order and mine and tuple(order) > tuple(mine):
        return False, "from a later episode"
    lang = str(example.get("target_lang") or "").split("-")[0]
    want = str(query.get("target_lang") or "").split("-")[0]
    if lang and want and lang != want:
        return False, "another target language"
    if example.get("features_version") and example["features_version"] != features.FEATURES:
        return False, "measured with another feature version"
    return True, ""


def propose_strength(template: dict, source: dict, take: dict) -> float:
    """How much of the template to ask for: the contrast the original had that
    the take lacks, in units of the template's own range (bounded)."""
    shape = template_shape(template, take)
    span = (max(shape) - min(shape)) if shape else 0.0
    if template["family"] in ("preserve",) or span <= 0:
        return 0.0 if template["family"] == "preserve" else 0.6
    gap = float(source.get("range_db") or 0.0) - float(take.get("range_db") or 0.0)
    spec = ((template.get("params") or {}).get("strength") or {})
    low, high = float(spec.get("min", 0.0)), float(spec.get("max", 1.0))
    return round(min(high, max(low, max(0.3, gap / span) if gap > 0 else 0.3)), 2)


def rank(line: dict, source: dict, take: dict, catalogue: list[dict], *,
         profile: dict | None = None, examples: list[dict] | None = None,
         query: dict | None = None, limit: int = MAX_CANDIDATES) -> dict:
    """Ranked, diverse envelope candidates for one line with every component.

    `source` is the original line's features, `take` the fitted take's
    features (doblarr.features). Returns ``{candidates, excluded, warnings,
    namespace}``; the first candidate is the retrieval-only choice.
    """
    started = time.perf_counter()
    query = dict(query or {})
    tags = line_tags(line)
    warnings = []
    usable_source = source.get("quality") == "ok" and source.get("curve")
    if not usable_source:
        warnings.append(f"the original line's shape is {source.get('quality') or 'missing'}; "
                        "it is not used as evidence")
    ratio = (float(take.get("duration") or 0) / float(source.get("duration") or 1)
             if source.get("duration") else 1.0)
    structure = abs(len(source.get("pauses") or []) - len(take.get("pauses") or []))
    if usable_source and not 0.7 <= ratio <= 1.45:
        warnings.append(f"the take is {ratio:.2f}× the original's length; its shape is "
                        "compared with a wider margin of doubt")
    if usable_source and structure >= 2:
        warnings.append("the take and the original pause differently; phrase structure "
                        "changed in translation")
    source_weight = 0.0 if not usable_source else (0.5 if (not 0.7 <= ratio <= 1.45
                                                           or structure >= 2) else 1.0)
    excluded = []
    pool = []
    for template in catalogue:
        ok, why = eligible(template, line, take, profile)
        if not ok:
            excluded.append({"template": template["id"], "reason": why})
            continue
        pool.append(template)
    usable_examples = []
    for example in examples or []:
        ok, why = example_eligible(example, query)
        if ok:
            usable_examples.append(example)
    favored = {p["template"]: p for p in ((profile or {}).get("templates") or {})
               .get("favored") or []}
    preserve_source = shape_similarity(take.get("curve") or [], source.get("curve") or []) \
        if usable_source else None
    rows = []
    for template in pool:
        shape = template_shape(template, take)
        components: dict[str, float | None] = {}
        support: list[str] = []
        conflict: list[str] = []
        if template["family"] == "preserve":
            components["source_shape"] = preserve_source
            if preserve_source is not None and preserve_source >= 0.7:
                support.append(f"the take already moves like the original "
                               f"(shape similarity {preserve_source:.2f})")
        elif template["family"] == "flatten":
            spread = float(take.get("range_db") or 0.0)
            want = float(source.get("range_db") or spread)
            components["source_shape"] = (round(max(0.0, min(1.0, (spread - want) / 12.0)), 3)
                                          if usable_source else None)
            if usable_source and spread > want + 3:
                support.append(f"the take wanders {spread - want:.1f} dB more than the "
                               "original")
        else:
            components["source_shape"] = (shape_similarity(source.get("curve") or [], shape)
                                          if usable_source else None)
            if components["source_shape"] is not None and components["source_shape"] >= 0.6:
                support.append(f"the original actor's emphasis has this shape "
                               f"({components['source_shape']:.2f})")
            take_like = shape_similarity(take.get("curve") or [], shape)
            if take_like is not None and take_like >= 0.75:
                conflict.append("the take already has most of this shape; little to add")
        template_tags = set(template.get("tags") or [])
        components["semantic"] = (round(len(template_tags & tags) / len(template_tags | tags), 3)
                                  if template_tags | tags else 0.0)
        if template_tags & tags:
            support.append(f"matches the line's {', '.join(sorted(template_tags & tags))}")
        votes = [e for e in usable_examples if e.get("template") == template["id"]]
        if votes:
            sims = [shape_similarity(source.get("curve") or [], e.get("source_curve") or [])
                    or 0.0 for e in votes]
            components["examples"] = round(max(sims), 3)
            support.append(f"{len(votes)} approved example(s) used it on similar lines")
        else:
            components["examples"] = None
        preference = 0.0
        if template["id"] in favored:
            preference = 1.0
            support.append("favoured for this character")
        components["preference"] = preference
        weights = dict(WEIGHTS)
        weights["source_shape"] *= source_weight if template["family"] != "preserve" else \
            max(source_weight, 0.5)
        total = sum(weights[k] * (v if v is not None else 0.0) for k, v in components.items())
        known = sum(weights[k] for k, v in components.items() if v is not None)
        score = round(total / known, 3) if known else 0.0
        if template["family"] == "preserve" and not usable_source:
            score = max(score, 0.5)     # with no evidence, leaving the take alone is safest
            support.append("no usable evidence for a change: preserve is the safe default")
        strength = propose_strength(template, source, take)
        rows.append({"template": templates.pin(template), "title": template["title"],
                     "family": template["family"], "score": score,
                     "components": components, "support": support, "conflicts": conflict,
                     "params": {"strength": strength} if "strength" in (
                         template.get("params") or {}) else {},
                     "shape": [round(v, 2) for v in shape]})
    rows.sort(key=lambda r: -r["score"])
    picked: list[dict] = []
    seen_shapes: list[list[float]] = []
    preserve = next((r for r in rows if r["family"] == "preserve"), None)
    for row in rows:
        if row is preserve:
            continue
        if any((shape_similarity(row["shape"], other) or 0.0) > 0.95 for other in seen_shapes):
            continue                # a near-duplicate shape adds no choice
        picked.append(row)
        seen_shapes.append(row["shape"])
        if len(picked) >= limit:
            break
    if preserve is not None:
        picked.append(preserve)
    picked.sort(key=lambda r: -r["score"])
    for i, row in enumerate(picked):
        following = picked[i + 1]["score"] if i + 1 < len(picked) else None
        row["margin"] = round(row["score"] - following, 3) if following is not None else None
    return {"namespace": namespace(), "candidates": picked, "excluded": excluded,
            "warnings": warnings, "examples_considered": len(usable_examples),
            "latency_ms": round((time.perf_counter() - started) * 1000, 2)}


def retrieval_choice(ranked: dict, min_margin: float = 0.03) -> dict:
    """The retrieval-only decision: the top candidate when it clearly leads,
    otherwise preserve (a near tie is not evidence for a change)."""
    candidates = ranked["candidates"]
    if not candidates:
        return {"template": None, "reason": "no eligible template"}
    top = candidates[0]
    if top["family"] != "preserve" and (top.get("margin") is None
                                        or top["margin"] < min_margin):
        preserve = next((c for c in candidates if c["family"] == "preserve"), None)
        if preserve is not None:
            return {**preserve, "reason": "the leading templates are too close to call; "
                                          "keeping the take as generated"}
    return {**top, "reason": "highest retrieval score"}


def save_index(path: Path, entries: list[dict]) -> None:
    payload = {"namespace": namespace(), "version": INDEX_VERSION, "entries": entries}
    write_json(path, payload, indent=None)


def load_index(path: Path) -> list[dict]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return payload.get("entries") or [] if payload.get("namespace") == namespace() else []
