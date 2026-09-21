"""Knowledge pack format, install/update/rollback, downloads, contributions.

A pack is a single bounded JSON document: a manifest plus the same portable
record form used by recipe overlays, pinned by id/revision. Everything is
validated — schema, hashes, size caps, duplicates, dependencies, conflicts —
BEFORE anything is activated; activation is one transaction. Old releases are
never deleted: frozen job snapshots and recipe pins keep resolving exactly.

Review states travel with the content: installing a pack never marks anything
reviewed, and proposed entries stay inactive until a reviewed release arrives.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .. import __version__
from ..recipes import RecipeEntry
from ..store import Database
from . import store as knowledge_store
from .models import Entry, Realization
from .portable import overlay_to_entry, overlay_to_realizations

log = logging.getLogger("doblarr.knowledge.packs")

PACK_FORMAT = "doblarr-knowledge-pack"
PACK_SCHEMA_VERSION = 1
MAX_PACK_BYTES = 512 * 1024
MAX_PACK_ENTRIES = 500
MAX_CONTRIBUTION_ENTRIES = 200

OFFICIAL = "official"
THIRD_PARTY = "third-party"


class PackManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    format: str
    schema_version: int
    id: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9][a-z0-9.-]*$")
    name: str = Field(min_length=1, max_length=200)
    release: str = Field(min_length=1, max_length=40)
    app_versions: str = Field(default=">=0.1", max_length=40)
    coverage: dict = Field(default_factory=dict)
    dependencies: dict[str, str] = Field(default_factory=dict, max_length=20)
    attribution: dict = Field(default_factory=dict)
    content_sha256: str = Field(min_length=64, max_length=64)
    entries: list[RecipeEntry] = Field(default_factory=list, max_length=MAX_PACK_ENTRIES)

    @field_validator("format")
    @classmethod
    def _format(cls, value: str) -> str:
        if value != PACK_FORMAT:
            raise ValueError(f"not a knowledge pack (format must be {PACK_FORMAT!r})")
        return value

    @field_validator("schema_version")
    @classmethod
    def _schema(cls, value: int) -> int:
        if value != PACK_SCHEMA_VERSION:
            raise ValueError(f"unsupported pack schema version {value}")
        return value

    @field_validator("app_versions")
    @classmethod
    def _app_compat(cls, value: str) -> str:
        if not value.startswith(">="):
            raise ValueError("app_versions must look like '>=0.1'")
        try:
            required = tuple(int(p) for p in value[2:].split("."))
            current = tuple(int(p) for p in __version__.split("."))
        except ValueError:
            raise ValueError("app_versions must look like '>=0.1'") from None
        padded = current + (0,) * (len(required) - len(current))
        if padded < required:
            raise ValueError(f"pack requires app {value}; this is {__version__}")
        return value


def content_hash(entries: list[dict]) -> str:
    canonical = json.dumps(entries, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class PackError(ValueError):
    """Any pack validation/activation failure; nothing was activated."""


@dataclass
class PreparedPack:
    manifest: PackManifest
    entries: list[Entry]
    realizations: list[Realization]
    document: dict


def load_pack_file(path: str | Path) -> dict:
    path = Path(path)
    if not path.is_file():
        raise PackError(f"pack file not found: {path}")
    if path.stat().st_size > MAX_PACK_BYTES:
        raise PackError(f"pack exceeds {MAX_PACK_BYTES // 1024} KB; audio/media are not packs")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PackError(f"pack is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise PackError("a pack is a JSON object, not code or a list")
    return data


def validate_pack(db: Database, data: dict) -> PreparedPack:
    """Full pre-activation validation; raises PackError, touches nothing."""
    if len(json.dumps(data, ensure_ascii=False).encode()) > MAX_PACK_BYTES:
        raise PackError("pack exceeds the size limit")
    try:
        manifest = PackManifest.model_validate(data)
    except ValidationError as exc:
        raise PackError("; ".join(e["msg"] for e in exc.errors())) from exc
    if manifest.content_sha256 != content_hash(data.get("entries", [])):
        raise PackError("content hash mismatch; the pack file is corrupted or modified")
    ids = [e.id for e in manifest.entries]
    if len(ids) != len(set(ids)):
        raise PackError("duplicate entry ids inside the pack")
    realization_ids = [r.id for e in manifest.entries for r in e.realizations]
    if len(realization_ids) != len(set(realization_ids)):
        raise PackError("duplicate realization ids inside the pack")
    installed = {r["pack_id"]: r["release"] for r in knowledge_store.pack_releases(db)
                 if r["active"]}
    missing = {
        dep: release
        for dep, release in manifest.dependencies.items()
        if installed.get(dep) != release
    }
    if missing:
        raise PackError(f"missing pack dependencies: {missing}")
    entries = [
        overlay_to_entry(overlay, scope_ref=manifest.id) for overlay in manifest.entries
    ]
    entries = [
        Entry(**{**e.__dict__, "scope": "pack", "origin": "installed", "pack_id": manifest.id})
        for e in entries
    ]
    realizations = []
    for overlay in manifest.entries:
        for realization in overlay_to_realizations(overlay):
            realizations.append(
                Realization(**{
                    **realization.__dict__, "origin": "installed", "pack_id": manifest.id})
            )
    conflicts = []
    for entry in entries:
        row = db.query_one(
            "SELECT * FROM knowledge_entries WHERE id = ? AND revision = ?",
            (entry.id, entry.revision),
        )
        if row is not None and knowledge_store._entry_from_row(row) != entry:
            conflicts.append(entry.id)
    if conflicts:
        raise PackError(f"entry pins conflict with existing records: {sorted(conflicts)}")
    for realization in realizations:
        row = db.query_one(
            "SELECT * FROM knowledge_realizations WHERE id = ? AND revision = ?",
            (realization.id, realization.revision),
        )
        if row is not None and knowledge_store._realization_from_row(row) != realization:
            raise PackError(f"realization pin conflicts: {realization.id}")
    for entry in entries:
        entry.validate()
    for realization in realizations:
        realization.validate()
    # A release cannot silently invalidate another active pack's dependency.
    for release in knowledge_store.pack_releases(db):
        if release["active"] and release["pack_id"] != manifest.id:
            required = json.loads(release["manifest"]).get("dependencies", {}).get(manifest.id)
            if required and required != manifest.release:
                raise PackError(f"{release['pack_id']} requires {manifest.id}@{required}")
    return PreparedPack(
        manifest=manifest, entries=entries, realizations=realizations, document=data,
    )


def activate(db: Database, prepared: PreparedPack, *, source: str, distribution: str) -> dict:
    """Swap the prepared release in atomically; the previous release stays installed."""
    manifest = prepared.manifest
    existing = [
        r for r in knowledge_store.pack_releases(db, manifest.id)
        if r["release"] == manifest.release
    ]
    if existing:
        raise PackError(f"release {manifest.release} of {manifest.id} is already installed")
    with db._lock, db._conn:
        validate_pack(db, prepared.document)
        # Validate the original hashed representation (defaults may have been omitted).
        for entry in prepared.entries:
            row = db.query_one(
                "SELECT * FROM knowledge_entries WHERE id = ? AND revision = ?",
                (entry.id, entry.revision),
            )
            if row is not None and knowledge_store._entry_from_row(row) != entry:
                raise PackError(f"entry pin conflicts: {entry.id}")
        db._conn.execute(
            "UPDATE knowledge_packs SET active = 0 WHERE pack_id = ?", (manifest.id,)
        )
        for entry in prepared.entries:
            if not knowledge_store.insert_entry_version(db._conn, entry):
                raise PackError(f"entry pin conflicts: {entry.id}")
        for realization in prepared.realizations:
            if not knowledge_store.insert_realization_version(db._conn, realization):
                raise PackError(f"realization pin conflicts: {realization.id}")
        db._conn.execute(
            "INSERT INTO knowledge_packs (pack_id, release, name, manifest, content_hash,"
            " source, distribution, active, installed_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                manifest.id,
                manifest.release,
                manifest.name,
                json.dumps(prepared.document, ensure_ascii=False, sort_keys=True),
                manifest.content_sha256,
                source,
                distribution,
                1,
                knowledge_store._now(),
            ),
        )
    log.info(
        "knowledge pack %s@%s installed (%d entries, source=%s)",
        manifest.id, manifest.release, len(prepared.entries), source,
    )
    return {
        "pack_id": manifest.id,
        "release": manifest.release,
        "entries": len(prepared.entries),
        "source": source,
    }


def install_pack(
    db: Database, path: str | Path, *, source: str = THIRD_PARTY, distribution: str = ""
) -> dict:
    """Install or update a pack from a local JSON file (validate, then activate)."""
    prepared = validate_pack(db, load_pack_file(path))
    return activate(db, prepared, source=source, distribution=distribution or str(path))


def rollback_pack(db: Database, pack_id: str) -> dict:
    """Re-activate the previously installed release. Explicit; nothing is deleted."""
    releases = knowledge_store.pack_releases(db, pack_id)
    if not releases:
        raise PackError(f"no installed pack with id {pack_id!r}")
    active = next((r for r in releases if r["active"]), None)
    candidates = [r for r in releases if r is not active and not r["active"]]
    if active is None or not candidates:
        raise PackError(f"pack {pack_id!r} has no earlier release to roll back to")
    previous = candidates[-1]
    previous_manifest = json.loads(previous["manifest"])
    previous_manifest["content_sha256"] = content_hash(previous_manifest["entries"])
    validate_pack(db, previous_manifest)
    knowledge_store.set_active_release(db, pack_id, previous["release"])
    log.info("knowledge pack %s rolled back %s -> %s", pack_id, active["release"],
             previous["release"])
    return {"pack_id": pack_id, "release": previous["release"],
            "rolled_back_from": active["release"]}


def download_pack(db: Database, config: dict, pack_id: str, cache_dir: Path) -> Path:
    """Download-once fetch of a pack file into the local cache; never per line."""
    base = str(config.get("pack_distribution_url") or "").strip()
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,99}", pack_id):
        raise PackError("invalid pack id")
    if not base:
        raise PackError("knowledge.pack_distribution_url is not configured")
    import requests

    url = f"{base.rstrip('/')}/{pack_id}.json"
    cache_dir.mkdir(parents=True, exist_ok=True)
    dest = cache_dir / f"{pack_id}.json"
    try:
        response = requests.get(url, timeout=30, stream=True)
    except requests.RequestException as exc:
        raise PackError(f"could not download pack: {exc}") from exc
    try:
        if response.status_code != 200:
            raise PackError(f"pack download failed (HTTP {response.status_code})")
        content = bytearray()
        for chunk in response.iter_content(65536):
            content.extend(chunk)
            if len(content) > MAX_PACK_BYTES:
                raise PackError(f"pack exceeds {MAX_PACK_BYTES // 1024} KB")
        try:
            validate_pack(db, json.loads(content))
        except (ValueError, TypeError) as exc:
            raise PackError(f"invalid downloaded pack: {exc}") from exc
        temporary = dest.with_suffix(".partial")
        temporary.write_bytes(content)
        temporary.replace(dest)
    finally:
        response.close()
    return dest


# -- bundled starter pack -----------------------------------------------------


def starter_pack_path() -> Path:
    from importlib.resources import files

    return Path(str(files("doblarr.knowledge.data").joinpath("starter-pack.json")))


def ensure_starter_pack(db: Database, config: dict) -> dict | None:
    """Install the bundled starter pack on first run so offline start works."""
    if not config.get("auto_install_starter", True):
        return None
    if knowledge_store.pack_releases(db, "doblarr-starter"):
        return None
    prepared = validate_pack(db, load_pack_file(starter_pack_path()))
    return activate(db, prepared, source=OFFICIAL, distribution="bundled")


# -- contribution bundles -----------------------------------------------------


def contribution_bundle(db: Database, entry_ids: list[str], *, author: str,
                        license: str, notes: str) -> tuple[dict, list[str]]:
    """Export selected LOCAL entries for repository review; never dialogue/media."""
    warnings: list[str] = []
    if len(entry_ids) > MAX_CONTRIBUTION_ENTRIES:
        raise PackError(f"a contribution is at most {MAX_CONTRIBUTION_ENTRIES} entries")
    entries = []
    realizations = []
    for entry_id in dict.fromkeys(entry_ids):
        entry = knowledge_store.get_entry(db, entry_id)
        if entry is None:
            raise PackError(f"unknown entry: {entry_id}")
        if entry.origin != "local":
            raise PackError("only your own local entries can be contributed")
        for example in entry.examples:
            if len(example) > 200 or "/" in example or "\\" in example:
                raise PackError(
                    "examples must be short authored text, never dialogue or file paths"
                )
        scope_note = ""
        if entry.scope != "personal":
            scope_note = f" (exported from {entry.scope} scope for review)"
            warnings.append(f"{entry.phrase!r} was rescoped to personal for review{scope_note}")
        entries.append(
            Entry(**{**entry.__dict__, "scope": "personal", "scope_ref": ""})
        )
        realizations += knowledge_store.realizations_for(db, entry_id)
    bundle_entries = [
        {**_portable_entry(e), "realizations": [
            _portable_realization(r) for r in realizations if r.entry_id == e.id]}
        for e in entries
    ]
    bundle = {
        "format": "doblarr-knowledge-contribution",
        "schema_version": 1,
        "author": author,
        "license": license,
        "notes": notes,
        "entries": bundle_entries,
    }
    bundle["content_sha256"] = content_hash(bundle_entries)
    return bundle, warnings


def _portable_entry(entry: Entry) -> dict:
    return {
        "id": entry.id,
        "revision": entry.revision,
        "kind": entry.kind,
        "locale": entry.locale,
        "coverage": list(entry.coverage),
        "source_lang": entry.source_lang,
        "source_form": entry.source_form,
        "phrase": entry.phrase,
        "sense": entry.sense,
        "usage": entry.usage,
        "examples": list(entry.examples),
        "pronunciation": entry.pronunciation,
        "ipa": entry.ipa,
        "scope": entry.scope,
        "status": entry.status,
        "license": entry.license,
        "contributor": entry.contributor,
        "review_history": list(entry.review_history),
    }


def _portable_realization(realization: Realization) -> dict:
    return {
        "id": realization.id,
        "revision": realization.revision,
        "engine": realization.engine,
        "model": realization.model,
        "voice": realization.voice,
        "replacement": realization.replacement,
        "evidence": realization.evidence,
        "status": realization.status,
    }
