"""Immutable local dub outputs with independent script and rendered-version IDs."""

from __future__ import annotations

import hashlib
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from .artifacts import digest, read_json
from .telemetry import write_json


def file_hash(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def preserve_version(job, config, cast=None) -> dict:
    """Copy completed media; never hardlink a file that a later run may replace/edit."""
    if not job.output_file or not job.output_file.is_file():
        raise ValueError("A completed output is required to save a dub version")
    script = {
        "source_language": job.script_lang or job.source_lang,
        "target_language": job.target_lang,
        "segments": [{"index": s.index, "start": s.start, "end": s.end,
                      "speaker": s.speaker, "text": s.text_translated}
                     for s in job.segments],
    }
    translation_id = digest(script)
    settings = {
        "translate": {k: config["translate"].get(k) for k in
                      ("provider", "model", "batch_size", "chars_per_second", "glossary",
                       "locale", "adaptation", "direction", "character_notes")},
        "dub": {k: v for k, v in config["dub"].items()
                if k not in {"version_name", "preserve_versions", "dry_run"}},
        "voicebox": {k: config["voicebox"].get(k) for k in
                     ("default_engine", "model_size", "seed")},
        "quality": dict(config["quality"]),
    }
    voices = [{"index": s.index, "speaker": s.speaker,
               "profile": s.voice or (job.speakers[s.speaker].voicebox_profile_id
                                      if s.speaker in job.speakers else None),
               "delivery": s.delivery, "revision": s.revision}
              for s in job.segments]
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
                "script": script}
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
