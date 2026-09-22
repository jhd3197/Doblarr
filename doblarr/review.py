"""Immutable per-run review snapshots and explicit line overrides."""

import math
import os
import shutil
from dataclasses import asdict
from pathlib import Path

from .artifacts import digest
from .cues import (
    CUE_SCHEMA_VERSION,
    DISPOSITIONS,
    Selection,
    cue_payload,
    ensure_identity,
    now,
    register_unknown_clip,
    resolve_cue,
)
from .performance import from_edit
from .telemetry import write_json


def snapshot_revision(job) -> str:
    """Identity of the decisions a reviewer is looking at.

    An edit carries the revision it was made against, so a snapshot that has
    moved on (a rerun, another reviewer, a new take) rejects the stale edit
    instead of overwriting the newer decision.
    """
    return digest([
        {
            "cue": seg.cue_id,
            "start": seg.start,
            "end": seg.end,
            "text": seg.text_translated or seg.text_src,
            "delivery": seg.delivery,
            "voice": seg.voice,
            "revision": seg.revision,
            "issues": sorted(seg.issues),
            "take": seg.audio.selection.take_id if seg.audio.selection else None,
            "render": (seg.audio.current().fingerprint if seg.audio.current() else None),
            "intent": seg.intent.as_dict(),
            "level": seg.level.applied_db,
            "verification": seg.verification.inputs,
            # A timing edit changes what a reviewer heard, so a verdict made
            # before it must read as stale rather than certify the new fit.
            "timing": seg.phrasing.inputs or f"{seg.phrasing.mode}/{seg.phrasing.state}",
            "dispositions": sorted((f.finding_id, f.disposition) for f in seg.findings),
        }
        for seg in job.segments
    ] + [sorted((e.event_id, e.coverage, e.inputs) for e in job.nonverbal)])[:16]


def apply_edits(job, edits, lineage=None):
    """Apply reviewed line overrides, addressed by cue ID or by legacy index.

    An edit naming a cue that a split or merge retired raises an actionable
    conflict (see `cues.resolve_cue`) rather than being redirected onto whatever
    line now occupies that position.
    """
    lineage = job.cue_lineage if lineage is None else lineage
    ensure_identity(job)
    targets = {}
    for key, edit in edits.items():
        # New edits carry the cue ID they were made against; legacy overrides
        # only have the index they were saved under.
        seg = resolve_cue(job.segments, (edit or {}).get("cue") or key, lineage)
        targets.setdefault(id(seg), []).append(key)
    for keys in targets.values():
        if len(keys) > 1:
            raise ValueError(f"line edits {sorted(keys)} address the same cue")
    kept = []
    for seg in job.segments:
        keys = targets.get(id(seg)) or []
        edit = edits[keys[0]] if keys else {}
        if edit.get("exclude"):
            continue
        if "text" in edit:
            if not str(edit["text"]).strip():
                raise ValueError("edited dialogue must not be empty")
            seg.text_translated = edit["text"].strip()
            seg.translation_provenance = {"method": "manual", "reason": "review-edit"}
        start, end = seg.start, seg.end
        seg.start = float(edit.get("start", seg.start))
        seg.end = float(edit.get("end", seg.end))
        if (
            not math.isfinite(seg.start)
            or not math.isfinite(seg.end)
            or seg.start < 0
            or seg.end <= seg.start
        ):
            raise ValueError(f"line {seg.index} needs a positive time window")
        if (seg.start, seg.end) != (start, end):
            # Target placement only. The recorded source interval is evidence of
            # what was actually spoken and must survive any timing edit.
            seg.placement.offset += seg.start - start
        for key in ("voice", "delivery", "revision"):
            if key in edit:
                setattr(seg, key, edit[key])
        _apply_intent(seg, edit)
        _apply_gain(job, seg, edit)
        _apply_timing(job, seg, edit)
        _apply_selection(seg, edit)
        _apply_dispositions(seg, edit)
        kept.append(seg)
    if not kept:
        raise ValueError("review edits excluded every spoken line")
    job.segments = kept


# Edits that change what the engine would be asked to say or how. Any of them
# retires a reviewed take selection: the take a person chose was a take of the
# *old* line, and keeping it selected would quietly ignore the edit.
_REGENERATING = ("text", "delivery", "voice", "mode", "traits", "direction")


def _apply_intent(seg, edit) -> None:
    """Structured acting direction, composed again at synthesis time."""
    fields = {k: edit[k] for k in ("mode", "traits", "direction", "treatment", "origin")
              if k in edit}
    if fields:
        seg.intent = from_edit(fields, seg.intent)


def _apply_gain(job, seg, edit) -> None:
    """A reviewer's per-line gain, in dB, kept with the job so a resume honors it."""
    if "gain_db" not in edit:
        return
    value = edit["gain_db"]
    if value in (None, ""):
        job.manual_gains.pop(seg.cue_id, None)
        return
    gain = float(value)
    if not math.isfinite(gain):
        raise ValueError(f"line {seg.index} needs a finite gain in dB")
    if abs(gain) > 24:
        raise ValueError(f"line {seg.index}: a manual gain beyond +/-24 dB is a mistake")
    job.manual_gains[seg.cue_id] = round(gain, 3)


# Timing decisions a reviewer makes about one line. None of them regenerates
# speech: they change how the take that already exists is placed and cut.
_TIMING_KEYS = ("anchors", "pauses", "bypass_timing", "overlap")


def _apply_timing(job, seg, edit) -> None:
    """Reviewer anchors, protected pauses, a bypass and an accepted overlap.

    Kept with the job rather than written into the config, for the same reason
    a manual gain is: it is a decision about this run, it must survive a
    resume, and it must not quietly become the default for the next episode.
    """
    if not any(key in edit for key in _TIMING_KEYS):
        return
    entry = dict(job.timing_edits.get(seg.cue_id) or {})
    if "anchors" in edit:
        anchors = []
        for raw in (edit["anchors"] or []):
            if not isinstance(raw, dict) or raw.get("at") is None:
                raise ValueError(f"line {seg.index}: an anchor needs a phrase and a time")
            try:
                at = float(raw["at"])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"line {seg.index}: an anchor needs a time") from exc
            if not math.isfinite(at) or at < 0 or at > seg.duration + 1e-6:
                raise ValueError(
                    f"line {seg.index}: an anchor must sit inside the line's window")
            anchors.append({"phrase": str(raw.get("phrase") or ""),
                            "order": raw.get("order"),
                            "edge": "end" if str(raw.get("edge")) == "end" else "start",
                            "at": round(at, 4),
                            "note": str(raw.get("note") or "")[:200]})
        if anchors:
            entry["anchors"] = anchors
        else:
            entry.pop("anchors", None)
    if "pauses" in edit:
        supplied = edit["pauses"] or {}
        if not isinstance(supplied, dict):
            raise ValueError(f"line {seg.index}: pauses must be an object")
        pauses = {str(k): {"protected": bool((v or {}).get("protected", True)),
                           "kind": str((v or {}).get("kind") or "pause")}
                  for k, v in supplied.items()}
        if pauses:
            entry["pauses"] = pauses
        else:
            entry.pop("pauses", None)
    if "bypass_timing" in edit:
        if edit["bypass_timing"]:
            entry["bypass"] = True
        else:
            entry.pop("bypass", None)
    if "overlap" in edit:
        if edit["overlap"]:
            current = seg.audio.current()
            # Bound to the exact render they heard. When the audio changes the
            # acceptance reads as stale instead of certifying a new collision.
            entry["overlap"] = {"accepted": True, "at": now(),
                                "inputs": current.fingerprint if current else ""}
        else:
            entry.pop("overlap", None)
    if entry:
        job.timing_edits[seg.cue_id] = entry
    else:
        job.timing_edits.pop(seg.cue_id, None)


def _apply_selection(seg, edit) -> None:
    """Point the cue at the take a reviewer chose, or back at a previous one.

    Selecting a take is not a generation: the raw audio already exists. Only
    the derivatives made from the *other* take are dropped, so the render
    reruns and the TTS does not.
    """
    chosen = seg.audio.selection
    if (chosen and chosen.reason in ("review", "restored", "candidate")
            and any(key in edit for key in _REGENERATING)):
        # The line changed; the chosen take was a take of the old line.
        seg.audio.selection = Selection(take_id=chosen.take_id, reason="auto",
                                        actor=chosen.actor, previous=chosen.previous,
                                        at=now())
    take_id = edit.get("take")
    if not take_id:
        return
    take = seg.audio.take(str(take_id))
    if take is None:
        raise ValueError(f"line {seg.index} has no take {take_id}")
    if take.raw is None or not take.raw.exists():
        raise ValueError(f"take {take_id} has no audio on disk any more")
    previous = seg.audio.selection.take_id if seg.audio.selection else None
    if previous == take.take_id:
        return
    seg.audio.selection = Selection(
        take_id=take.take_id,
        reason="restored" if take.origin == "auto" else "review",
        actor=str(edit.get("actor") or ""), previous=previous, at=now())
    seg.audio.invalidate_after("raw")
    seg.audio_clip = Path(take.raw.path)


def _apply_dispositions(seg, edit) -> None:
    """Record a human verdict on a finding, with its history and its note."""
    decisions = edit.get("dispositions") or {}
    if not isinstance(decisions, dict):
        raise ValueError(f"line {seg.index}: dispositions must be an object")
    for finding_id, decision in decisions.items():
        found = next((f for f in seg.findings if f.finding_id == str(finding_id)), None)
        if found is None:
            # A finding that no longer exists is not an error: the audio it was
            # about may have been replaced. The verdict is simply dropped.
            continue
        value = str((decision or {}).get("disposition") or "").strip()
        if value not in DISPOSITIONS:
            raise ValueError(f"unknown disposition {value!r} for finding {finding_id}")
        note = str((decision or {}).get("note") or "")
        actor = str((decision or {}).get("actor") or "")
        if found.disposition == value and not note:
            continue
        found.history.append({"at": now(), "from": found.disposition, "to": value,
                              "reason": note or "reviewer decision", "actor": actor,
                              "inputs": found.inputs})
        found.disposition = value


# --------------------------------------------------------------------------
# Reviewer decisions
# --------------------------------------------------------------------------
#
# A review snapshot is an immutable projection of one run. A reviewer's verdict
# on a finding is new information about that run, so it lives in a sidecar next
# to the snapshot rather than being written back into it. The snapshot stays
# exactly what the pipeline produced; the sidecar says what a person concluded
# about it, and records which snapshot revision they were looking at.

def decisions_path(root, job_id: str) -> Path:
    """Where one job's reviewer decisions live.

    Keyed by the job, not by the snapshot file: every run writes a fresh
    report name, so a sidecar next to the snapshot would silently lose every
    verdict the moment the job was re-rendered.
    """
    if not job_id:
        raise ValueError("review decisions belong to a job")
    return Path(root) / "reviews" / "decisions" / f"{job_id}.json"


def load_decisions(root, job_id: str) -> dict:
    from .artifacts import read_json

    data = read_json(decisions_path(root, job_id))
    if not isinstance(data, dict) or not isinstance(data.get("cues"), dict):
        return {"version": 1, "cues": {}, "events": {}}
    # A sidecar written before coverage existed has no events map. Adding an
    # empty one is not a migration, it is the same file read forward.
    if not isinstance(data.get("events"), dict):
        data["events"] = {}
    return data


def record_decision(root, job_id: str, revision: str, cue_id: str, patch: dict,
                    actor: str = "", scope: str = "cue") -> dict:
    """Store one cue's or one event's reviewer decision, keeping its history.

    Every disposition carries the snapshot revision it was made against, so a
    verdict recorded before a re-render is visibly about the older audio
    instead of silently certifying the new one.

    Events get their own map rather than being filed under a cue: a reaction's
    cue is very often the one that was removed from synthesis, and half of them
    have no surviving cue at all.
    """
    if not cue_id:
        raise ValueError("a review decision needs the cue or event it is about")
    if scope not in ("cue", "event"):
        raise ValueError("a review decision is about a cue or an event")
    data = load_decisions(root, job_id)
    where = data["cues"] if scope == "cue" else data["events"]
    entry = dict(where.get(cue_id) or {})
    history = list(entry.get("history") or [])
    dispositions = dict(entry.get("dispositions") or {})
    for finding_id, decision in (patch.get("dispositions") or {}).items():
        value = str((decision or {}).get("disposition") or "").strip()
        if value not in DISPOSITIONS:
            raise ValueError(f"unknown disposition {value!r}")
        record = {"disposition": value, "note": str((decision or {}).get("note") or ""),
                  "actor": actor, "at": now(), "revision": revision}
        dispositions[str(finding_id)] = record
        history.append({"finding": str(finding_id), **record})
    entry["dispositions"] = dispositions
    entry["history"] = history[-50:]
    for key in ("note", "mode", "traits", "direction", "gain_db", "take", "candidates",
                "reviewed", "timing_note", "coverage_note"):
        if key in patch:
            entry[key] = patch[key]
    entry["revision"] = revision
    entry["at"] = now()
    where[cue_id] = entry
    data["version"] = 1
    write_json(decisions_path(root, job_id), data)
    return entry


def merge_decisions(payload: dict, decisions: dict) -> dict:
    """Fold stored decisions into a review payload for display.

    A disposition recorded against an older snapshot revision is shown as
    stale rather than applied: the audio it was about is not the audio on
    screen, and quietly carrying it forward is how an old approval ends up
    certifying a new render.
    """
    current = payload.get("revision")
    for row in payload.get("segments", []):
        cue_id = (row.get("cue") or {}).get("cue_id") or ""
        entry = decisions.get("cues", {}).get(cue_id)
        if not entry:
            continue
        _fold(row, (row.get("cue") or {}).get("findings", []), entry, current)
    for event in payload.get("nonverbal", []):
        entry = decisions.get("events", {}).get(event.get("event_id") or "")
        if not entry:
            continue
        _fold(event, event.get("findings", []), entry, current)
    return payload


def _fold(row: dict, findings: list, entry: dict, current) -> None:
    """Attach one stored decision to a row and to the findings it judged."""
    stale = entry.get("revision") != current
    row["decision"] = {**entry, "stale": stale}
    for finding in findings:
        verdict = entry.get("dispositions", {}).get(finding.get("finding_id"))
        if not verdict:
            continue
        finding["review"] = {**verdict, "stale": stale}
        if not stale:
            finding["disposition"] = verdict["disposition"]


def write_review(job, root, settings=None):
    """Write the immutable snapshot of this run for review.

    `settings` are the effective settings the run used. They are frozen here
    rather than read back from live config, so opening an old review shows the
    policy that produced it instead of whatever is configured today.
    """
    if not job.report_file:
        return
    ensure_identity(job)
    job.review_file = root / "reviews" / job.report_file.name
    rows = []
    for seg in job.segments:
        register_unknown_clip(seg)
        row = asdict(seg)
        clip = seg.audio_clip
        if clip and clip.is_file():
            snapshot = root / "reviews" / job.report_file.stem / f"line_{seg.index}.wav"
            snapshot.parent.mkdir(parents=True, exist_ok=True)
            if not snapshot.exists():
                try:
                    os.link(clip, snapshot)
                except OSError:
                    shutil.copy2(clip, snapshot)
            clip = snapshot
        row["audio_clip"] = str(clip.resolve()) if clip else None
        speaker = job.speakers.get(seg.speaker)
        row["profile"] = seg.voice or (speaker.voicebox_profile_id if speaker else None)
        # One versioned codec: the review snapshot carries the same cue records
        # the script cache and the saved version manifest do. They live under
        # "cue" only, so the row never holds two copies that can disagree.
        for owned in ("lineage", "source", "placement", "audio", "findings"):
            row.pop(owned, None)
        row["cue"] = cue_payload(seg)
        rows.append(row)
    write_json(
        job.review_file,
        {
            "version": 1,
            "cue_schema": CUE_SCHEMA_VERSION,
            "revision": snapshot_revision(job),
            # Where this run's playable evidence lives. Machine-local, so the
            # API resolves previews from it and never sends it to a browser.
            "media": {
                "source_track": str(job.source_track) if job.source_track else None,
                "source_audio": str(job.source_audio) if job.source_audio else None,
                "dubbed_track": str(job.dubbed_track) if job.dubbed_track else None,
                "output": str(job.output_file) if job.output_file else None,
                "work": str(job.artifacts_dir) if job.artifacts_dir else None,
                # The two separated stems, so a coverage review can hear the bed
                # under the dub and the voices that were taken out of it.
                "vocals": str(job.vocals) if job.vocals else None,
                "background": (str(job.background)
                               if job.background and job.background != job.source_audio
                               else None),
            },
            "settings": dict(settings or {}),
            "dialogue_baseline": job.dialogue_baseline,
            "manual_gains": job.manual_gains,
            # Which sample each cloned voice was built from, so the same
            # character can be compared across scenes. Paths stay server-side.
            "references": {label: str(speaker.reference_clip)
                           for label, speaker in job.speakers.items()
                           if speaker.reference_clip},
            "language": job.target_lang,
            "source_language": job.script_lang or job.source_lang,
            "locale": job.target_locale or job.target_lang,
            "source_reference": (job.source_reference.as_dict()
                                 if job.source_reference else None),
            "cue_lineage": {k: list(v) for k, v in job.cue_lineage.items()},
            "nonverbal": [e.as_dict() for e in job.nonverbal],
            "timing_edits": job.timing_edits,
            "segments": rows,
            "metrics": job.metrics,
            "flagged": sum(bool(s.issues) for s in job.segments),
        },
    )
