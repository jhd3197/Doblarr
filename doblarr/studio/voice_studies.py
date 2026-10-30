"""Voice studies as studio records (doblarr.voice_study does the measuring).

A study names two films by the path of their analysed media (or sidecar) and
the voice-group names a person gave each. Profiles keep only prototype sums;
probe results keep cue ids and answers, never a vector. Every record is in
the study's scope, so deleting the study removes all of them.
"""

from __future__ import annotations

from pathlib import Path

from .. import voice_study
from . import records

KINDS = ("voice_study", "voice_profile", "voice_probe_result")
_lookup = voice_study.read


def create(db, work_dir: Path, name: str, source: dict, probes: list[dict], *,
           floor: float, margin: float) -> dict:
    """Build the study and film 1's profiles. `source` and each probe are
    `{"path": ..., "names": {group: character}}`; probes are not read here."""
    sidecar = _lookup(source["path"], work_dir)
    known = voice_study.named_lines(sidecar, source["names"])
    if not known:
        raise voice_study.StudyError("film 1 has no named lines; name its voice groups first")
    study_id = records.digest_id(name, records.now_marker())
    study = records.put(db, "voice_study", study_id, {
        "name": name, "source": source, "probes": probes, "model": sidecar.get("model"),
        "thresholds": {"floor": floor, "margin": margin}, "status": "profiles built",
        "created_at": records.now_marker()}, scope=study_id, create_only=True)
    used = len(known)
    for character, profile in voice_study.profiles(known).items():
        records.put(db, "voice_profile", records.digest_id(study_id, character), {
            "study_id": study_id, "character": character, **profile,
            "excluded_lines": len(sidecar.get("lines") or []) - used}, scope=study_id)
    return study


def probe(db, work_dir: Path, study_id: str, path: str) -> dict:
    """Run one of the study's probes held out and store its result."""
    study = records.get(db, "voice_study", study_id)
    if study is None:
        raise KeyError(study_id)
    film = next((p for p in study["probes"] if p["path"] == path), None)
    if film is None:
        raise KeyError(path)
    thresholds = study["thresholds"]
    out = voice_study.run(_lookup(study["source"]["path"], work_dir), study["source"]["names"],
                          _lookup(path, work_dir), film["names"], **thresholds)
    out.pop("profiles")
    result = records.put(db, "voice_probe_result", records.digest_id(study_id, path), {
        "study_id": study_id, "probe": path, **out}, scope=study_id)
    records.update(db, "voice_study", study_id, {"status": "probed"}, scope=study_id)
    return result


def detail(db, study_id: str) -> dict | None:
    study = records.get(db, "voice_study", study_id)
    if study is None:
        return None
    mine = [r for k in ("voice_profile", "voice_probe_result")
            for r in records.list_latest(db, k, study_id)]
    return {**study,
            "profiles": [r for r in mine if "protos" in r],
            "results": [r for r in mine if "matrix" in r]}


def delete(db, study_id: str) -> int:
    return records.purge(db, KINDS, study_id)
