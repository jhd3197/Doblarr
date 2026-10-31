"""Who speaks, kept honest across regrouping, corrections and episodes.

Three things depend on which lines a voice group holds, and all three must
follow when the membership changes:

- **Speaker baselines.** A line's ``relative_db`` is its level against its
  speaker's ordinary dialogue. Move lines between groups and the baselines move
  too. The raw measurement of each line does not: its source span is the same,
  so it is reused and only the baselines and relative levels are recomputed.
  Overlap is speaker-relative as well (two lines of one person are not
  cross-talk), so it is recomputed with the new labels.
- **Identities.** A group a person named keeps its character when a regroup
  hands most of its speaking time to one new group; lines a person moved by
  hand are moved again after every regroup. Both are recorded as associations
  (doblarr.identity), never inferred from a label string alone.
- **Series memory.** What a revision teaches about each character's voice is
  replaced, not added again, whenever its identities change.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace

from . import identity, levels, voice_tags
from .cues import SourceMeasurement, Span
from .fileio import write_json
from .studio import records

log = logging.getLogger("doblarr.speaker_memory")

OVERLAP_NOTE = "another speaker talks across this cue"


def evaluation_streams(db, input_file) -> set[int]:
    """Streams of this media that a studio reference holds out for evaluation."""
    wanted = identity.norm_path(input_file) if input_file else ""
    found: set[int] = set()
    for ref in records.list_latest(db, "reference"):
        track = ref.get("track") or {}
        if "evaluation" in (ref.get("roles") or []) and track.get("stream_index") is not None \
                and identity.norm_path(track.get("media_path") or "") == wanted:
            found.add(int(track["stream_index"]))
    return found


def held_out_revisions(db) -> set[str]:
    """Source revisions declared evaluation-only (doblarr.feedback.hold_out)."""
    from . import feedback

    return feedback.held_out(db)


def _segments_for_baseline(data: dict) -> list:
    """Segment stand-ins carrying exactly what levels.build_baseline reads."""
    out = []
    for seg in data.get("segments") or []:
        cue = seg.get("cue") or {}
        spans = [Span.from_dict(s) for s in (cue.get("source") or {}).get("spans") or []]
        try:
            measurement = SourceMeasurement.from_dict(cue.get("measurement") or {})
        except Exception:  # noqa: BLE001 - a corrupt row is skipped, not fatal
            measurement = SourceMeasurement()
        out.append(SimpleNamespace(speaker=str(seg.get("speaker") or ""),
                                   source=SimpleNamespace(spans=spans),
                                   measurement=measurement, raw=seg))
    return out


def refresh_baselines(script: Path) -> dict:
    """Recompute speaker baselines and relative levels for the current groups.

    Returns ``{changed, baseline, overlaps}``: how many lines' relative level
    moved, the new baseline record, and how many lines changed overlap status.
    """
    data = json.loads(Path(script).read_text(encoding="utf-8"))
    segments = _segments_for_baseline(data)
    flips = 0
    for seg in segments:
        m = seg.measurement
        if m.state not in ("measured", "contaminated") or m.speech_db is None:
            continue
        now = levels.overlapping(segments, seg)
        if now == m.overlapped:
            continue
        flips += 1
        m.overlapped = now
        others = [e for e in m.exclusions if e != OVERLAP_NOTE]
        if now:
            m.exclusions = [*others, OVERLAP_NOTE]
            if m.state == "measured":
                m.state, m.contaminated = "contaminated", True
        else:
            m.exclusions = others
            # Only overlap made it contaminated: the span itself is clean again.
            if m.state == "contaminated" and not any(
                    "background" in e or "separated" in e or "speech-active" in e
                    for e in others):
                m.state, m.contaminated = "measured", False
    before = {id(s): s.measurement.relative_db for s in segments}
    config = levels.settings({"measure_source": True})
    baseline = levels.build_baseline(segments, config)
    levels.apply_baseline(segments, baseline)
    changed = sum(1 for s in segments if s.measurement.relative_db != before[id(s)])
    for seg in segments:
        seg.raw.setdefault("cue", {})["measurement"] = seg.measurement.as_dict()
    data["dialogue_baseline"] = baseline
    write_json(Path(script), data, indent=1)
    return {"changed": changed, "baseline": baseline, "overlaps": flips}


def labels_to_characters(db, revision_id: str) -> dict[str, str]:
    """Voice group -> character id for one revision (accepted or manual links)."""
    return {label: character["id"]
            for label, character in identity.cluster_characters(db, revision_id).items()}


def teach(db, ident: dict, sidecar_path: Path | None, language: str) -> dict[str, int]:
    """Replace what this revision teaches the series about its voices."""
    if not ident.get("series_id") or not ident.get("revision_id"):
        return {}
    sidecar = voice_tags.read_sidecar(sidecar_path)
    if ident["revision_id"] in held_out_revisions(db):
        return {}     # an evaluation episode never feeds the memory it is scored against
    return voice_tags.remember_series(db, ident["series_id"], ident["revision_id"], sidecar,
                                      labels_to_characters(db, ident["revision_id"]),
                                      language)


def after_analysis(db, job, work: Path) -> dict:
    """At the end of an analysis: teach the series, summarise the grouping."""
    from .stages.common import work_stem

    ident = job.metrics.get("identity") or {}
    folder = Path(job.vocals).parent if job.vocals else Path(work)
    sidecar = folder / f"{work_stem(job)}.speakers.json"
    taught = teach(db, ident, sidecar if sidecar.is_file() else None, job.source_lang or "")
    job.metrics["speaker_memory"] = {"taught": taught}
    return taught


def carry_identities(db, ident: dict, before: list[str], after: list[str],
                     seconds: list[float], cues: list[str] | None = None) -> dict[str, str]:
    """After a regroup: each new group takes the character most of its time had.

    Lines a person assigned by hand are left out of the count: they are put
    back by their lock (`reapply_line_locks`), and counting them would let one
    moved line carry its character onto a group that was mostly someone else.
    Writes the new cluster associations (carried ones as ``accepted`` with the
    share that carried them) and clears links of labels that no longer exist.
    Returns new label -> character id.
    """
    revision_id = ident["revision_id"]
    locked = set(line_locks(db, revision_id))
    if cues is not None and locked:
        seconds = [0.0 if cue in locked else d for cue, d in zip(cues, seconds, strict=True)]
    old = labels_to_characters(db, revision_id)
    carried = voice_tags.carry_names(before, after, seconds, old)
    share: dict[str, float] = {}
    for b, a, d in zip(before, after, seconds, strict=True):
        if old.get(b) and carried.get(a) == old.get(b):
            share[a] = share.get(a, 0.0) + d
    totals: dict[str, float] = {}
    for a, d in zip(after, seconds, strict=True):
        totals[a] = totals.get(a, 0.0) + d
    for label in sorted(set(after) | set(old)):
        character = carried.get(label)
        current = next((r for r in identity.associations(db, revision_id, "cluster")
                        if r["ref"] == label), None)
        if character is None and (current is None or not current.get("character_id")):
            continue
        evidence = ([{"kind": "carried", "share": round(share.get(label, 0.0)
                                                         / (totals.get(label) or 1.0), 3)}]
                    if character else [{"kind": "regrouped", "note": "no group kept its time"}])
        identity.associate(db, revision_id, "cluster", label, character, state="accepted",
                           evidence=evidence, locked=False,
                           base_revision=current["revision"] if current else 0)
    return carried


def prior_segments(db, revision_id: str) -> list[dict]:
    """The lines (time, group label) of the analysis this revision has now."""

    for snap in records.list_latest(db, "snapshot", scope=revision_id):
        script = ((snap.get("stages") or {}).get("transcribe") or {}).get(
            "outputs", {}).get("script")
        if script and Path(script).is_file():
            data = json.loads(Path(script).read_text(encoding="utf-8"))
            return [{"start": float(s["start"]), "end": float(s["end"]),
                     "speaker": s.get("speaker") or ""} for s in data.get("segments") or []]
    return []


def carry_by_time(db, ident: dict, before: list[dict], segments) -> dict[str, str] | None:
    """Names onto a fresh grouping, by where in time each named group spoke.

    Each new line takes the earlier group it overlaps most (the lines may have
    been cut differently), then each new group takes the character most of
    its time had (`carry_identities`). Nothing changes when the grouping is
    the one already named. Returns new label -> character id, or None.
    """
    if not ident.get("revision_id") or not before or not segments:
        return None
    if not labels_to_characters(db, ident["revision_id"]):
        return None
    old, new, seconds, cues = [], [], [], []
    for seg in segments:
        best, best_overlap = "", 0.0
        for row in before:
            overlap = min(seg.end, row["end"]) - max(seg.start, row["start"])
            if overlap > best_overlap:
                best, best_overlap = row["speaker"], overlap
        old.append(best)
        new.append(seg.speaker or "")
        seconds.append(max(0.0, seg.end - seg.start))
        cues.append(str(getattr(seg, "cue_id", "") or ""))
    if old == new:
        return None
    return carry_identities(db, ident, old, new, seconds, cues)


def line_locks(db, revision_id: str) -> dict[str, str | None]:
    """Lines a person assigned by hand: cue -> character id (None: a new voice)."""
    return {r["ref"]: r.get("character_id")
            for r in identity.associations(db, revision_id, "line")
            if r.get("state") == "manual"}


def reapply_line_locks(db, ident: dict, script: Path, sidecar: Path | None) -> int:
    """Move every hand-assigned line back to its character's group."""
    from . import speakers

    locks = line_locks(db, ident["revision_id"])
    if not locks:
        return 0
    data = json.loads(Path(script).read_text(encoding="utf-8"))
    labels = {str(seg.get("speaker")) for seg in data.get("segments") or []}
    by_character = {v: k for k, v in labels_to_characters(db, ident["revision_id"]).items()}
    moves: dict[str, str] = {}
    for cue, character in locks.items():
        label = by_character.get(character) if character else None
        if label is None:
            taken = {int(x[8:]) for x in labels | set(moves.values()) if x[8:].isdigit()}
            label = f"SPEAKER_{max(taken, default=-1) + 1:02d}"
            if character:
                by_character[character] = label
                identity.associate(db, ident["revision_id"], "cluster", label, character,
                                   state="accepted", locked=False,
                                   evidence=[{"kind": "line-lock", "cue": cue}])
        moves[cue] = label
    moved = speakers.move_lines(Path(script), sidecar, moves)
    return moved


def analysis_script(db, revision_id: str) -> Path | None:
    """The analysis script of a source revision (any language), if one exists."""
    for snap in records.list_latest(db, "snapshot", scope=revision_id):
        script = ((snap.get("stages") or {}).get("transcribe") or {}).get(
            "outputs", {}).get("script")
        if script and Path(script).is_file():
            return Path(script)
    return None


def characters_for_labels(db, revision_id: str, segments) -> dict[str, dict]:
    """Another run's voice labels -> characters, through the cues they share.

    A dub run groups voices again and its SPEAKER_03 need not be the analysis'
    SPEAKER_03. Cue ids are stable across runs of the same source, so each
    label takes the character most of its cues were identified as in the
    analysis (by speaking time), and only when that is at least half of it.
    """
    script = analysis_script(db, revision_id)
    if script is None:
        return {}
    analysed = json.loads(script.read_text(encoding="utf-8"))
    cue_label = {str((seg.get("cue") or {}).get("cue_id")): str(seg.get("speaker"))
                 for seg in analysed.get("segments") or []}
    linked = identity.cluster_characters(db, revision_id)
    locks = {cue: identity.resolve_character(db, cid) for cue, cid in
             line_locks(db, revision_id).items() if cid}
    votes: dict[str, dict[str, float]] = {}
    found: dict[str, dict] = {}
    for seg in segments:
        cue = str(seg.cue_id)
        character = locks.get(cue) or linked.get(cue_label.get(cue, ""))
        if character is None:
            continue
        found[character["id"]] = character
        bucket = votes.setdefault(seg.speaker, {})
        bucket[character["id"]] = bucket.get(character["id"], 0.0) + max(0.01, seg.duration)
    totals: dict[str, float] = {}
    for seg in segments:
        totals[seg.speaker] = totals.get(seg.speaker, 0.0) + max(0.01, seg.duration)
    out = {}
    for label, bucket in votes.items():
        best, seconds = max(bucket.items(), key=lambda kv: kv[1])
        if seconds / (totals.get(label) or 1.0) >= 0.5:
            out[label] = found[best]
    return out


def name_from_dialogue(db, ident: dict, lines: list[dict]) -> list[dict]:
    """Name voice groups the dialogue clearly identifies (doblarr.dialogue_clues).

    A group takes the name it answers to when that is strong: at least three
    answers, or two for a group nobody named, and never a name it calls
    itself. It replaces a name that disagrees; the old one is kept in the
    evidence so the page can offer to undo, and a name a person kept after an
    undo (evidence ``kept``) is never replaced. Writes ``accepted`` links.
    Returns what changed: [{label, name, replaced, answers}].
    """
    from . import dialogue_clues

    revision_id, series_id = ident.get("revision_id"), ident.get("series_id")
    if db is None or not revision_id or not series_id or not lines:
        return []
    characters = identity.characters(db, series_id)
    cast = [n for c in characters for n in [c["name"], *(c.get("aliases") or [])]]
    clues = dialogue_clues.read(lines, cast)["groups"]
    links = {r["ref"]: r for r in identity.associations(db, revision_id, "cluster")}
    names = {c["id"]: c for c in identity.characters(db, series_id, include_retired=True)}
    changes = []
    for label, said in sorted(clues.items()):
        wanted = said.get("suggests")
        if not wanted or not said["answers"]:
            continue
        current = links.get(label) or {}
        if any(e.get("kind") == "kept" for e in current.get("evidence") or []):
            continue
        named = names.get(current.get("character_id") or "")
        count = said["answers"][0]["count"]
        if count < (2 if named is None else 3):
            continue
        if named is not None and wanted.casefold() in {
                n.casefold() for n in [named["name"], *(named.get("aliases") or [])]}:
            continue
        character = identity.ensure_character(db, series_id, wanted, origin="dialogue")
        identity.associate(db, revision_id, "cluster", label, character["id"], state="accepted",
                           locked=False, base_revision=current.get("revision", 0),
                           evidence=[{"kind": "dialogue", "answers": count,
                                      "replaced": named["name"] if named else "",
                                      "reader": dialogue_clues.READER}])
        changes.append({"label": label, "name": character["name"],
                        "replaced": named["name"] if named else "", "answers": count})
    if changes:
        log.info("dialogue named %s", ", ".join(f"{c['label']}={c['name']}" for c in changes))
    return changes


MEMORY_SIMILARITY = 0.70   # a voice this close to a character the show knows...
MEMORY_MARGIN = 0.15       # ...and this far ahead of the next one is that character


def name_from_memory(db, ident: dict, sidecar_path: Path | None, language: str) -> list[dict]:
    """Name unnamed voice groups the show's voice memory clearly recognises.

    The memory is what earlier episodes taught (a duration-weighted print per
    character). A group is named only when it is very close to one character
    and well ahead of the next: on a hand-labelled episode, learning from one
    half and matching the other, every match past this bar was right (8 of 8),
    and every wrong guess was a character the memory had never heard, far
    below it. Never replaces a name; writes ``accepted`` links with the
    evidence. Returns [{label, name, similarity, margin, episodes}].
    """
    revision_id, series_id = ident.get("revision_id"), ident.get("series_id")
    if db is None or not revision_id or not series_id:
        return []
    sidecar = voice_tags.read_sidecar(sidecar_path)
    links = {r["ref"]: r for r in identity.associations(db, revision_id, "cluster")}
    named = {label for label, row in links.items() if row.get("character_id")
             or any(e.get("kind") == "kept" for e in row.get("evidence") or [])}
    picks = voice_tags.suggest_series(db, series_id, revision_id, sidecar, named, language,
                                      exclude=held_out_revisions(db))
    characters = {c["id"]: c for c in identity.characters(db, series_id)}
    taken: set[str] = set()
    changes = []
    for label, ranked in sorted(picks.items(), key=lambda kv: -kv[1][0]["similarity"]):
        best = ranked[0]
        margin = best["margin"] if best["margin"] is not None else 1.0
        character = characters.get(best["character_id"])
        if character is None or best["similarity"] < MEMORY_SIMILARITY \
                or margin < MEMORY_MARGIN or character["id"] in taken:
            continue
        taken.add(character["id"])
        current = links.get(label) or {}
        identity.associate(db, revision_id, "cluster", label, character["id"], state="accepted",
                           locked=False, base_revision=current.get("revision", 0),
                           evidence=[{"kind": "voice-memory", "similarity": best["similarity"],
                                      "margin": round(margin, 3), "episodes": best["episodes"],
                                      "lines": best["lines"]}])
        changes.append({"label": label, "name": character["name"],
                        "similarity": best["similarity"], "margin": round(margin, 3),
                        "episodes": best["episodes"]})
    if changes:
        log.info("voice memory named %s", ", ".join(f"{c['label']}={c['name']}" for c in changes))
    return changes
