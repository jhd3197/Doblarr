"""How fast each character speaks from one line to the next.

A dubbed line is heard next to its neighbours, not on its own. When one line
plays at its natural pace and the next is compressed to 1.3x, or a slow take
plays slow with a gap after it, the character sounds "sometimes slow,
sometimes fast". This module measures that.

For every synthesized line:

- **voiced seconds**: speech inside the take, from the same energy detector
  boundary preparation and phrase timing use (`boundaries.inspect`), with the
  lead, tail and internal pauses left out;
- **units**: letters and digits in the spoken text. Language-agnostic, and the
  same measure `translate.chars_per_second` budgets in;
- **natural rate** = units / voiced seconds, the pace the engine produced;
- **applied factor**: the time compression the timing owner applied;
- **effective rate** = natural rate x factor, the pace the audience hears.

Lines are compared inside a **pace group**: one speaker within one scene. The
pipeline has no scene notion at this stage, so a speaker's lines are split
into a new group wherever the gap between two of them exceeds
`timing.pace_scene_gap`.

`pace_jump` is an info finding: a line whose effective rate differs from its
group's median by more than `timing.pace_tolerance`.
"""

from __future__ import annotations

import logging
import statistics
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from .stages import boundaries
from .stages.quality import apply_findings

if TYPE_CHECKING:
    from .models import Segment

log = logging.getLogger("doblarr.pacing")

DETECTOR = "pacing/1"
MODES = ("off", "speaker")

DEFAULTS = {
    "pacing": "speaker",
    "pace_tolerance": 0.18,
    "pace_local_range": 0.10,
    "pace_max_speedup": 1.15,
    "pace_scene_gap": 8.0,
    "max_stretch": 1.3,
    "min_stretch": 1.0,
    "threshold_db": 12.0,
}


def settings(options: dict | None) -> dict:
    """The pacing policy from `timing.*`, with defaults for anything unset."""
    options = options or {}
    out = {}
    for key, default in DEFAULTS.items():
        value = options.get(key, default)
        out[key] = value if isinstance(default, str) else float(value)
    if out["pacing"] not in MODES:
        raise ValueError(f"timing.pacing must be one of {', '.join(MODES)}")
    out["max_stretch"] = max(1.0, out["max_stretch"])
    out["pace_max_speedup"] = max(1.0, out["pace_max_speedup"])
    out["pace_local_range"] = max(0.0, out["pace_local_range"])
    return out


def text_units(text: str) -> int:
    """Letters and digits: punctuation and spaces are not spoken."""
    return sum(1 for ch in text or "" if unicodedata.category(ch)[0] in "LN")


def voiced_seconds(path: Path, threshold_db: float = 12.0, cancel=None) -> float | None:
    """Speech inside a take, pauses excluded; None when it cannot be measured."""
    try:
        bounds = boundaries.inspect(Path(path), threshold_db, cancel=cancel)
    except Exception as exc:  # noqa: BLE001 - an unreadable take is "unknown"
        log.debug("pacing: cannot measure %s: %s", path, exc)
        return None
    if bounds.first is None or bounds.last is None or bounds.last <= bounds.first:
        return None
    voiced = (bounds.last - bounds.first) - sum(b - a for a, b in bounds.silences)
    return voiced if voiced > 0.05 else None


def spoken_text(seg) -> str:
    return seg.tts_text or seg.text_translated or seg.text_src or ""


@dataclass
class Line:
    """One synthesized line's pace, before and after timing."""

    seg: Segment
    units: int = 0
    voiced: float | None = None
    factor: float = 1.0
    group: str = ""

    @property
    def natural_rate(self) -> float | None:
        if not self.voiced or not self.units:
            return None
        return self.units / self.voiced

    @property
    def effective_rate(self) -> float | None:
        rate = self.natural_rate
        return None if rate is None else rate * self.factor


def measure(seg, take: Path, factor: float = 1.0, threshold_db: float = 12.0,
            cancel=None) -> Line:
    return Line(seg, text_units(spoken_text(seg)),
                voiced_seconds(take, threshold_db, cancel), factor)


def groups(segments, scene_gap: float = 8.0) -> dict[str, list]:
    """Pace groups: one speaker's lines, split where they fall silent a while."""
    by_speaker: dict[str, list] = {}
    for seg in sorted(segments, key=lambda s: (s.start, s.index)):
        by_speaker.setdefault(seg.speaker or "", []).append(seg)
    out: dict[str, list] = {}
    for speaker, lines in by_speaker.items():
        scene, previous = 0, None
        for seg in lines:
            if previous is not None and seg.start - previous.end > scene_gap:
                scene += 1
            out.setdefault(f"{speaker}#{scene}", []).append(seg)
            previous = seg
    return out


def assign_groups(lines: list[Line], scene_gap: float) -> dict[str, list[Line]]:
    by_seg = {id(line.seg): line for line in lines}
    grouped: dict[str, list[Line]] = {}
    for name, segs in groups([line.seg for line in lines], scene_gap).items():
        for seg in segs:
            line = by_seg[id(seg)]
            line.group = name
            grouped.setdefault(name, []).append(line)
    return grouped


def median_rate(lines: list[Line], effective: bool = True) -> float | None:
    rates = [line.effective_rate if effective else line.natural_rate for line in lines]
    known = [r for r in rates if r]
    return statistics.median(known) if known else None


@dataclass
class Summary:
    groups: dict = field(default_factory=dict)
    lines: dict = field(default_factory=dict)
    jumps: int = 0


def record(job, lines: list[Line], options: dict | None = None,
           bases: dict[str, float] | None = None) -> Summary:
    """Pace metrics into `job.metrics["pacing"]` and `pace_jump` findings.

    Every measured line updates its findings, so a line that now keeps pace
    retires its old jump instead of leaving a stale one open.
    """
    config = settings(options)
    tolerance = config["pace_tolerance"]
    summary = Summary()
    for name, members in assign_groups(lines, config["pace_scene_gap"]).items():
        median = median_rate(members)
        rates = [m.effective_rate for m in members if m.effective_rate]
        jumps = [abs(b - a) / median for a, b in zip(rates, rates[1:], strict=False)
                 if median]
        summary.groups[name] = {
            "speaker": members[0].seg.speaker,
            "lines": len(members),
            "median_rate": _round(median),
            "spread": _round((max(rates) - min(rates)) / median) if rates and median else None,
            "max_jump": _round(max(jumps)) if jumps else None,
            **({"base": round(bases[name], 4)} if bases and name in bases else {}),
        }
        for member in members:
            effective = member.effective_rate
            deviation = (effective - median) / median if effective and median else None
            summary.lines[member.seg.cue_id or str(member.seg.index)] = {
                "group": name, "units": member.units, "voiced": _round(member.voiced),
                "natural_rate": _round(member.natural_rate), "factor": round(member.factor, 4),
                "effective_rate": _round(effective), "deviation": _round(deviation),
            }
            observed = []
            if deviation is not None and abs(deviation) > tolerance:
                summary.jumps += 1
                observed.append(("pace_jump", "timing", "info", None, {
                    "group": name, "effective_rate": _round(effective),
                    "group_median": _round(median), "deviation": _round(deviation),
                    "factor": round(member.factor, 4), "tolerance": tolerance,
                    "direction": "fast" if deviation > 0 else "slow"}))
            apply_findings(member.seg, DETECTOR,
                           f"{effective or 0:.3f}/{median or 0:.3f}/{tolerance}", observed)
    job.metrics["pacing"] = {
        "mode": config["pacing"],
        "lines_measured": sum(1 for line in lines if line.natural_rate),
        "pace_jumps": summary.jumps,
        "groups": summary.groups,
        "lines": summary.lines,
    }
    if summary.jumps:
        log.info("pacing: %d line(s) off their character's pace by more than %.0f%%",
                 summary.jumps, tolerance * 100)
    return summary


def _round(value, places: int = 3):
    return None if value is None else round(value, places)
