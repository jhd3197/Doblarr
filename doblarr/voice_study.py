"""A voice study: profiles from film 1, tested held out on film 2.

Profiles are built from film 1's named lines alone, so film 2 can never teach
the profiles it is tested on. Nothing here translates, clones or speaks; it
reads two `speakers.json` sidecars and the names a person gave.
"""

from __future__ import annotations

from pathlib import Path

from . import speaker_eval, voice_tags


class StudyError(ValueError):
    """The two films cannot be compared; the message says why."""


def named_lines(sidecar: dict, names: dict[str, str]) -> list[dict]:
    """Lines with a vector, long enough to count, whose group has a name."""
    return [{**line, "name": names[line["speaker"]]}
            for line in voice_tags._anchors(sidecar.get("lines") or [])
            if names.get(line.get("speaker") or "")]


def profiles(lines: list[dict]) -> dict[str, dict]:
    """Per character: lines and seconds used, spread (mean pairwise cosine) and
    prototype sums. The only vectors kept are the prototype sums."""
    import numpy as np

    out = {}
    for name in sorted({line["name"] for line in lines}):
        own = [line for line in lines if line["name"] == name]
        unit = np.asarray([line["vector"] for line in own], dtype=np.float64)
        unit = unit / np.maximum(np.linalg.norm(unit, axis=1, keepdims=True), 1e-12)
        n = len(own)
        spread = float((unit @ unit.T).sum() - n) / (n * (n - 1)) if n > 1 else None
        out[name] = {"lines": n, "seconds": round(sum(float(x["end"]) - float(x["start"])
                                                       for x in own), 2),
                     "spread": None if spread is None else round(spread, 3),
                     "protos": voice_tags.prototypes(own)}
    return out


def run(source: dict, source_names: dict[str, str], probe: dict,
        probe_names: dict[str, str], *, floor: float = voice_tags.FLOOR,
        margin: float = 0.05, use_prototypes: bool = False) -> dict:
    """Profiles from `source` (a parsed sidecar), and each named line of `probe`
    recognised against them. Lines of `probe` without a name are skipped."""
    if source.get("model") != probe.get("model"):
        raise StudyError(f"the films were embedded with different voice models "
                         f"({source.get('model')} and {probe.get('model')}); analyse both "
                         "with the same one")
    known = named_lines(source, source_names)
    held = named_lines(probe, probe_names)
    if not known:
        raise StudyError("film 1 has no named lines; name its voice groups first")
    if not held:
        raise StudyError("film 2 has no named lines to test against")
    found = speaker_eval.recognition(
        [x["vector"] for x in known], [x["name"] for x in known],
        [float(x["end"]) - float(x["start"]) for x in known], floor=floor, margin=margin,
        use_prototypes=use_prototypes, min_seconds=0.0,
        held_vectors=[x["vector"] for x in held], held_truth=[x["name"] for x in held],
        detail=True)
    per_line = [{"cue": held[r.pop("index")]["cue"], **r} for r in found.pop("per_line")]
    return {"model": source.get("model"), "floor": floor, "margin": margin,
            "prototypes": use_prototypes, "profiles": profiles(known), "per_line": per_line,
            **speaker_eval.study_summary(per_line),
            "unseen": sorted({x["name"] for x in held} - {x["name"] for x in known})}


def read(path: str | Path, work_dir: str | Path | None = None) -> dict:
    """A sidecar from its own path, or from a media file's analysis in `work_dir`."""
    from .speaking import find_script

    path = Path(path)
    if not path.name.endswith(".speakers.json") and work_dir is not None:
        script = find_script(Path(work_dir), path)
        if script:
            stem = script.name.removesuffix(".script.json")
            path = next((f for folder in (script.parent, *script.parents[:3])
                         if (f := folder / f"{stem}.speakers.json").is_file()), path)
    data = voice_tags.read_sidecar(path)
    if not data or not data.get("lines"):
        raise StudyError(f"no voice analysis found for {path}; run `doblarr analyze` first")
    return data
