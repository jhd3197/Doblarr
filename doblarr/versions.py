"""Immutable local dub outputs with independent script and rendered-version IDs."""

from __future__ import annotations

import hashlib
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from .artifacts import digest, read_json
from .cues import CUE_SCHEMA_VERSION, cue_payload, ensure_identity
from .telemetry import write_json


def file_hash(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def _cue_provenance(seg) -> dict:
    """One line's identity and audio provenance for a saved version manifest.

    Machine-local paths are left out on purpose: a manifest travels, and a
    recipe or an exported version must not carry this machine's work dir.
    """
    record = cue_payload(seg)
    for take in record["audio"]["takes"]:
        if take.get("raw"):
            take["raw"].pop("path", None)
    for render in record["audio"]["renders"]:
        render.pop("path", None)
    return {"index": seg.index, **record}


def _event_provenance(event) -> dict:
    """One nonverbal event for a saved manifest, without this machine's paths.

    A supplied replacement asset lives somewhere on one computer. The manifest
    records *that* an asset was used and its identity, never where to find it,
    because a version manifest travels and a local sound path is not a fact
    about the title.
    """
    record = event.as_dict()
    if record.get("artifact"):
        record["artifact"].pop("path", None)
    record["asset"] = Path(record["asset"]).name if record.get("asset") else ""
    return record


def preserve_version(job, config, cast=None) -> dict:
    """Copy completed media; never hardlink a file that a later run may replace/edit."""
    if not job.output_file or not job.output_file.is_file():
        raise ValueError("A completed output is required to save a dub version")
    ensure_identity(job)
    script = {
        "source_language": job.script_lang or job.source_lang,
        "target_language": job.target_lang,
        "target_locale": job.target_locale or job.target_lang,
        "segments": [{"index": s.index, "start": s.start, "end": s.end,
                      "speaker": s.speaker, "text": s.text_translated}
                     for s in job.segments],
    }
    translation_id = digest(script)
    settings = {
        "translate": {k: config["translate"].get(k) for k in
                      ("provider", "model", "batch_size", "chars_per_second", "glossary",
                       "locale", "adaptation", "direction", "character_notes", "slang")},
        "dub": {k: v for k, v in config["dub"].items()
                if k not in {"version_name", "preserve_versions", "dry_run"}},
        "voicebox": {k: config["voicebox"].get(k) for k in
                     ("default_engine", "model_size", "seed")},
        "quality": dict(config["quality"]),
        # Boundary preparation and edge fades change the rendered audio, so a
        # saved version has to record which settings produced it.
        "boundaries": dict(config.get("boundaries", {})),
        # So does the level owner, including the per-cue manual gains: two
        # renders that differ only by a reviewer's gain are different dubs.
        "levels": dict(config.get("levels", {})),
        # Which timing owner ran and what coverage was decided. The per-event
        # sound *paths* are dropped: a manifest travels, and where a wav file
        # sits on this machine is not part of what makes this version this
        # version. Which decision was made is, and it stays.
        "timing": dict(config.get("timing", {})),
        "coverage": {k: v for k, v in dict(config.get("coverage", {})).items()
                     if k != "assets"},
        # Which acoustic space each line was played through is part of what
        # makes this version this version: two renders differing only by a
        # preset are different dubs and must not share an identity.
        "treatments": dict(config.get("treatments", {})),
    }
    voices = [{"index": s.index, "speaker": s.speaker,
               "profile": s.voice or (job.speakers[s.speaker].voicebox_profile_id
                                      if s.speaker in job.speakers else None),
               "delivery": s.delivery, "revision": s.revision}
              for s in job.segments]
    # The identity digest is deliberately unchanged: adding cue provenance to it
    # would fork every version already on disk. Wording and placement still move
    # translation_id; any audible change moves output_sha256 and so version_id.
    identity = {"schema_version": 1, "translation_id": translation_id,
                "output_sha256": file_hash(job.output_file),
                "source_sha256": file_hash(job.input_file),
                "settings": settings, "voices": voices, "kind": job.kind,
                "cast": sorted(
                    [{k: entry.get(k) for k in
                      ("speaker_id", "voice", "engine", "delivery", "revision")}
                     for entry in (cast or [])], key=lambda entry: entry["speaker_id"])}
    version_id = digest(identity)
    root = (job.output_file.parent / "versions").resolve()
    root.mkdir(parents=True, exist_ok=True)
    destination = root / version_id
    output = destination / job.output_file.name
    manifest_path = destination / "version.json"
    name = config["dub"].get("version_name", "").strip() or "Dub"
    manifest = {**identity, "version_id": version_id, "name": name,
                "created_at": datetime.now(UTC).isoformat(), "output": str(output),
                "script": script,
                # Provenance, outside the identity digest: which cue each line
                # is, which source interval it came from, which take was
                # selected, and which processed artifact was actually rendered.
                "cue_schema": CUE_SCHEMA_VERSION,
                "source_reference": (job.source_reference.as_dict()
                                     if job.source_reference else None),
                "cue_lineage": {k: list(v) for k, v in job.cue_lineage.items()},
                "nonverbal": [_event_provenance(e) for e in job.nonverbal],
                "cues": [_cue_provenance(s) for s in job.segments],
                # What the run measured and what it concluded, outside the
                # identity digest: evidence about this version, not part of
                # what makes it this version.
                "dialogue_baseline": job.dialogue_baseline,
                "manual_gains": job.manual_gains,
                "treatment_edits": job.treatment_edits,
                # What the exported track was measured to be. Outside the
                # identity digest on purpose: it is evidence *about* this
                # version, produced after the bytes that define it, and
                # folding it in would fork a version on a re-check.
                "delivery": job.delivery,
                # Knowledge provenance for this version; outside the identity digest
                # so an unrelated rule edit never forks an identical render.
                "knowledge": job.knowledge_snapshot,
                "line_provenance": [{"index": s.index, "source": s.text_src,
                                     "translated": s.text_translated, "tts_text": s.tts_text,
                                     "rules": s.applied_rules,
                                     "translation": s.translation_provenance}
                                    for s in job.segments]}
    if destination.exists():
        saved = read_json(manifest_path)
        if (saved.get("version_id") != version_id or not output.is_file()
                or file_hash(output) != identity["output_sha256"]):
            raise ValueError(
                "Saved dub version is incomplete or modified; refusing to overwrite it")
        manifest = saved
    else:
        # This temporary directory is created strictly inside the resolved output
        # versions root; TemporaryDirectory removes only its own staging directory.
        with tempfile.TemporaryDirectory(prefix=".saving-", dir=root) as temp:
            staging = Path(temp).resolve()
            assert staging.parent == root
            shutil.copy2(job.output_file, staging / output.name)
            if file_hash(staging / output.name) != identity["output_sha256"]:
                raise ValueError("Dub changed while saving the version")
            write_json(staging / "version.json", manifest)
            try:
                staging.rename(destination)
            except FileExistsError:
                # Another identical run won the race. Verify its media before reuse.
                if not output.is_file() or file_hash(output) != identity["output_sha256"]:
                    raise
                manifest = read_json(manifest_path)
                if manifest.get("version_id") != version_id:
                    raise ValueError("Concurrent version has an invalid manifest") from None
    job.output_file = output
    job.version_id = version_id
    job.translation_id = translation_id
    job.version_name = manifest["name"]
    job.version_file = manifest_path
    return manifest
