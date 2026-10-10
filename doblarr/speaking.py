"""Who talks in a show, how much, and how.

The first thing to know about a cast is its weight: the lead who speaks 40% of
an episode needs a voice chosen with care, the guard with one line does not.
The second is range. A character heard only calmly can be cast from any clean
line; one who shouts half the time needs a voice (and a reference) that can
shout, and the lines where they do are the material a voice bank is built from.

Both come from scripts Doblarr already wrote: each line's speaker and time, and
the original actor's level against their own average (`relative_db`, measured
on the separated dialogue). Nothing is generated or re-measured here.
"""

from __future__ import annotations

import json
from pathlib import Path

from .identity import norm_path

# The same performance bands the studio auditions use, so a line called
# "intense" here is the line an audition would pick as intense.
QUIET_DB, INTENSE_DB = -3.5, 3.5
HIGHLIGHTS = 3


def band(relative_db) -> str:
    if not isinstance(relative_db, int | float):
        return "unmeasured"
    if relative_db <= QUIET_DB:
        return "quiet"
    if relative_db >= INTENSE_DB:
        return "intense"
    return "calm"


def find_script(work_dir: Path, episode_file: str | Path) -> Path | None:
    """The newest full-run script for an episode file, wherever its run lived.

    See `locate_script`; this returns the path alone.
    """
    return locate_script(work_dir, episode_file)[0]


def locate_script(work_dir: Path, episode_file: str | Path) -> tuple[Path | None, str]:
    """The script for an episode file and how it was found.

    A script records the media path its run read. A script made from exactly
    this path is the answer (``exact``). Only when none was is a script with
    the same file stem offered (``name``), because a copy of the episode run
    from elsewhere shares the name; if scripts with that stem were made from
    several different inputs, the match is ``ambiguous`` and a caller must not
    treat it as this file's identity. Teasers and auditions are skipped.
    """
    stem = Path(str(episode_file).replace("\\", "/")).stem
    media = Path(work_dir) / "media"
    if not stem or not media.is_dir():
        return None, "none"
    found = [p for p in media.glob(f"*/**/{glob_escape(stem)}.script.json") if p.is_file()]
    if not found:
        return None, "none"
    wanted = norm_path(episode_file)
    inputs = {p: norm_path(_input_of(p)) for p in found}
    exact = [p for p in found if inputs[p] == wanted]
    # A script whose lines carry the original actor's level can say how a
    # character talks, not only how much; an older run without it is a fallback.
    def rank(p):
        return (_measured(p), p.stat().st_mtime)

    if exact:
        return max(exact, key=rank), "exact"
    distinct = {inputs[p] for p in found if inputs[p]}
    return max(found, key=rank), ("ambiguous" if len(distinct) > 1 else "name")


def _input_of(script: Path) -> str:
    try:
        return str((json.loads(Path(script).read_text(encoding="utf-8")).get("identity")
                    or {}).get("input") or "")
    except (OSError, ValueError):
        return ""


def _measured(path: Path) -> bool:
    return any(isinstance(((s.get("cue") or {}).get("measurement") or {}).get("relative_db"),
                          int | float) for s in load_segments(path))


def glob_escape(text: str) -> str:
    return "".join(f"[{c}]" if c in "[]*?" else c for c in text)


def load_segments(path: Path) -> list[dict]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [s for s in data.get("segments") or [] if isinstance(s, dict)]


def talk_share(episodes: list[dict]) -> dict:
    """Aggregate speakers across episodes.

    `episodes` is `[{"id", "label", "segments"}]`. Returns every speaker with
    their seconds, share of all dialogue, lines, episodes, seconds by band and
    their most intense lines, largest share first.
    """
    speakers: dict[str, dict] = {}
    total = 0.0
    for episode in episodes:
        for seg in episode.get("segments") or []:
            name = str(seg.get("speaker") or "UNKNOWN")
            seconds = max(0.0, float(seg.get("end", 0)) - float(seg.get("start", 0)))
            if not seconds:
                continue
            total += seconds
            row = speakers.setdefault(name, {
                "speaker": name, "seconds": 0.0, "lines": 0, "episodes": set(),
                "bands": {"quiet": 0.0, "calm": 0.0, "intense": 0.0, "unmeasured": 0.0},
                "candidates": []})
            level = ((seg.get("cue") or {}).get("measurement") or {}).get("relative_db")
            row["seconds"] += seconds
            row["lines"] += 1
            row["episodes"].add(episode.get("id"))
            row["bands"][band(level)] += seconds
            if isinstance(level, int | float):
                row["candidates"].append({
                    "episode": episode.get("id"), "episode_label": episode.get("label", ""),
                    "start": round(float(seg["start"]), 2), "end": round(float(seg["end"]), 2),
                    "relative_db": round(float(level), 1),
                    "text": seg.get("text_translated") or seg.get("text_src") or "",
                    "source_text": seg.get("text_src") or ""})
    rows = []
    for row in speakers.values():
        seconds = row["seconds"]
        # Loudest first, then longest: an intense line long enough to clone
        # (2 s) is worth more than a short shout.
        highlights = sorted(row.pop("candidates"),
                            key=lambda c: (c["relative_db"], c["end"] - c["start"]),
                            reverse=True)
        rows.append({
            **row,
            "seconds": round(seconds, 1),
            "share": round(seconds / total, 4) if total else 0.0,
            "episodes": sorted(e for e in row["episodes"] if e is not None),
            "bands": {k: round(v / seconds, 3) if seconds else 0.0
                      for k, v in row["bands"].items()},
            "highlights": [h for h in highlights if h["relative_db"] >= INTENSE_DB][:HIGHLIGHTS],
        })
    rows.sort(key=lambda r: r["seconds"], reverse=True)
    return {"speakers": rows, "total_seconds": round(total, 1),
            "episodes": len(episodes),
            "note": ("Shares count spoken time in the scripts Doblarr has; an episode that "
                     "was never run is not in them. Bands come from the original actor's "
                     "level against their own average.")}
