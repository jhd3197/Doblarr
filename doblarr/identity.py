"""What a file is, which show it belongs to, and who speaks in it.

A path is where a file is today, not what it is. Doblarr's older memory keyed
everything by a name: a show by its folder, an episode by its file name, a
script by its stem. Two shows filed under the same folder name, an episode
copied elsewhere, a re-encode or a director's cut all collide under such keys.
This module gives each of them an identity that does not depend on the name.

- A **series** is a show or a movie collection. Its id comes from a library
  provider (``show:tvdb:<id>``, ``movie:tmdb:<id>``) when one is known, and is
  a local id bound to the full folder path otherwise. A folder *name* never
  decides it on its own.
- A **media** record is one episode or one movie. It owns its **source
  revisions**: one per distinct content (sampled hash, size, duration and
  stream layout). The same content at a new path is the same revision, which is
  what makes a move harmless. New content at a known path, or another cut, is a
  new revision of the same media, so nothing measured on the old file is
  silently reused for the new one.
- A **character** belongs to one series and keeps its id through renames.
  Variants (an age, a transformation, an edition) hang off the character.
  A diarization label, a performer, a cloned voice and a visible track are
  separate things; they are linked to a character through **associations** that
  carry evidence and a review state.

Everything is stored as revisioned studio records (doblarr.studio.records), so
two concurrent writers conflict instead of overwriting each other.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import threading
import uuid
from pathlib import Path
from typing import Any

from .fileio import write_json
from .studio import records

log = logging.getLogger("doblarr.identity")

SAMPLE_BYTES = 1 << 20     # hashed from the head, the middle and the tail of a file
IDENTITY_METHOD = "sampled-sha256/1"
SEASON_FOLDER = re.compile(r"^(season|series|temporada|saison|staffel|specials?)\b|^s?\d+$",
                           re.IGNORECASE)
EPISODE_TAG = re.compile(r"\bS(\d{1,3})\s*E(\d{1,4})\b", re.IGNORECASE)
# Words that name an edition rather than the title. Read from the file name as a
# hint only; a person can set the edition explicitly.
EDITION_WORDS = re.compile(
    r"\b(director'?s cut|extended(?: edition| cut)?|uncut|unrated|theatrical(?: cut)?|"
    r"remaster(?:ed)?|special edition|final cut|tv cut|bd ?rip|web-?dl|dvd ?rip)\b",
    re.IGNORECASE)

_cache_lock = threading.Lock()


def norm_path(path: str | Path) -> str:
    """A location as a comparable string: separators unified, case folded on Windows."""
    text = str(path or "").replace("\\", "/").rstrip("/")
    return os.path.normcase(text).replace("\\", "/")


def _cache_file(cache_dir: Path | None) -> Path | None:
    return Path(cache_dir) / "identity-cache.json" if cache_dir else None


def _read_cache(path: Path | None) -> dict:
    if path is None:
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def probe_streams(path: Path) -> tuple[float | None, list[dict]]:
    """Duration and stream layout from ffprobe; (None, []) when it cannot read it."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "format=duration:stream=index,codec_type,codec_name,channels,sample_rate,"
             "avg_frame_rate,width,height:stream_tags=language,title",
             "-of", "json", str(path)],
            capture_output=True, check=False, text=True, timeout=60).stdout
        data = json.loads(out or "{}")
    except (OSError, ValueError, subprocess.SubprocessError):
        return None, []
    streams = []
    for row in data.get("streams") or []:
        tags = row.get("tags") or {}
        streams.append({k: v for k, v in {
            "index": row.get("index"), "type": row.get("codec_type"),
            "codec": row.get("codec_name"), "channels": row.get("channels"),
            "rate": row.get("sample_rate"), "fps": row.get("avg_frame_rate"),
            "width": row.get("width"), "height": row.get("height"),
            "language": str(tags.get("language") or ""), "title": str(tags.get("title") or ""),
        }.items() if v not in (None, "")})
    try:
        duration = float((data.get("format") or {}).get("duration") or "")
    except (TypeError, ValueError):
        duration = None
    return duration, streams


def content_identity(path: str | Path, cache_dir: Path | None = None) -> dict:
    """The identity of a file's content, independent of where it lives.

    Hashes at most three MiB (head, middle, tail) plus the size, so a two-hour
    movie costs the same as a clip. The stream layout and duration join the key:
    two encodes of one episode differ even if their sampled bytes collided.
    Cached by location, size and modification time.
    """
    path = Path(path)
    stat = path.stat()
    location = norm_path(path.resolve())
    marker = f"{location}|{stat.st_size}|{stat.st_mtime_ns}"
    cache_path = _cache_file(cache_dir)
    with _cache_lock:
        cached = _read_cache(cache_path).get(marker)
    if cached:
        return cached
    digest = hashlib.sha256(str(stat.st_size).encode())
    with path.open("rb") as stream:
        offsets = sorted({0, max(0, stat.st_size // 2 - SAMPLE_BYTES // 2),
                          max(0, stat.st_size - SAMPLE_BYTES)})
        for offset in offsets:
            stream.seek(offset)
            digest.update(stream.read(SAMPLE_BYTES))
    duration, streams = probe_streams(path)
    layout = [[s.get("index"), s.get("type"), s.get("codec"), s.get("channels"),
               s.get("language")] for s in streams]
    digest.update(json.dumps([round(duration or 0.0, 1), layout]).encode())
    found = {"content_key": "c" + digest.hexdigest()[:24], "size": stat.st_size,
             "duration": duration, "streams": streams, "method": IDENTITY_METHOD}
    if cache_path is not None:
        with _cache_lock:
            table = _read_cache(cache_path)
            table[marker] = found
            if len(table) > 5000:   # bounded: oldest insertions go first
                for old in list(table)[:len(table) - 5000]:
                    table.pop(old, None)
            write_json(cache_path, table, indent=None)
    return found


def show_folder(path: str | Path) -> Path:
    """The folder a show lives in: the file's folder, above any season folder."""
    folder = Path(str(path).replace("\\", "/")).parent
    if SEASON_FOLDER.match(folder.name) and folder.parent.name:
        folder = folder.parent
    return folder


def parse_episode(text: str) -> tuple[int | None, int | None]:
    match = EPISODE_TAG.search(text or "")
    return (int(match.group(1)), int(match.group(2))) if match else (None, None)


def edition_hint(path: str | Path) -> str:
    match = EDITION_WORDS.search(Path(str(path).replace("\\", "/")).stem)
    return match.group(1).lower() if match else ""


# --------------------------------------------------------------------------
# Hints: what the library and the queue already know about a path
# --------------------------------------------------------------------------

def library_hints(db, path: str | Path) -> dict:
    """Provider ids, kind, season and episode for a path, from the library scan
    and the jobs that ran on it. Every value is optional; none is guessed."""
    wanted = norm_path(path)
    hints: dict[str, Any] = {}
    row = db.query_one("SELECT items FROM scan_state WHERE id = 1")
    if row is not None:
        try:
            items = json.loads(row["items"] or "[]")
        except ValueError:
            items = []
        for item in items:
            if not isinstance(item, dict) or not item.get("path"):
                continue
            if norm_path(item["path"]) == wanted:
                hints.update({k: item[k] for k in ("tmdb_id", "tvdb_id", "title", "year")
                              if item.get(k)})
                hints["kind"] = "movie" if item.get("media_type") == "movie" else "episode"
                break
        if not hints:
            # An episode inside a show folder the library knows: the show's
            # provider id is location evidence (the folder *path*, never its name).
            for item in items:
                if (isinstance(item, dict) and item.get("media_type") == "show"
                        and item.get("tvdb_id") and item.get("path")
                        and wanted.startswith(norm_path(item["path"]) + "/")):
                    hints.update(tvdb_id=item["tvdb_id"], kind="episode")
                    break
    rows = db.query("SELECT title, input_file, payload FROM jobs WHERE input_file IS NOT NULL "
                    "ORDER BY created_at DESC")
    for job in rows:
        if norm_path(job["input_file"] or "") != wanted:
            continue
        try:
            payload = json.loads(job["payload"] or "{}")
        except ValueError:
            payload = {}
        show_ref = str(payload.get("show_ref") or "")
        if show_ref.startswith("series:") and show_ref[7:].isdigit():
            hints.setdefault("tvdb_id", int(show_ref[7:]))
            hints.setdefault("kind", "episode")
        season, episode = parse_episode(job["title"] or "")
        if season is not None:
            hints.setdefault("season", season)
            hints.setdefault("episode", episode)
        break
    if "season" not in hints:
        season, episode = parse_episode(Path(str(path).replace("\\", "/")).name)
        if season is not None:
            hints["season"], hints["episode"] = season, episode
            hints.setdefault("kind", "episode")
    return hints


# --------------------------------------------------------------------------
# Series and media records
# --------------------------------------------------------------------------

def series_id_for(path: str | Path, hints: dict) -> tuple[str, dict]:
    """The series a file belongs to and the facts its id was derived from."""
    if hints.get("kind") == "movie" and hints.get("tmdb_id"):
        return f"movie:tmdb:{int(hints['tmdb_id'])}", {"provider": "tmdb"}
    if hints.get("tvdb_id"):
        return f"show:tvdb:{int(hints['tvdb_id'])}", {"provider": "tvdb"}
    if hints.get("kind") == "movie":
        # A movie without a provider id is its own collection.
        folder = Path(str(path).replace("\\", "/")).parent
        return "movie:local:" + hashlib.sha256(norm_path(folder).encode()).hexdigest()[:16], {
            "provider": "local", "folder": str(folder)}
    folder = show_folder(path)
    return "show:local:" + hashlib.sha256(norm_path(folder).encode()).hexdigest()[:16], {
        "provider": "local", "folder": str(folder)}


def _put_merged(db, kind: str, record_id: str, mutate, *, scope: str = "",
                attempts: int = 4) -> dict:
    """Read, change and write one record, retrying on a concurrent write.

    `mutate(document) -> document` must be a pure function of what it was
    given, so a retry after a conflict applies the same change to the newer
    revision instead of overwriting it.
    """
    for _ in range(attempts):
        current = records.get(db, kind, record_id)
        document = mutate(dict(current) if current else {})
        if current is not None and _same(current, document):
            return current
        try:
            return records.put(db, kind, record_id, document, scope=scope,
                               base_revision=current["revision"] if current else 0)
        except records.StudioConflict:
            continue
    raise records.StudioConflict(f"{kind} {record_id} kept changing; try again")


def _same(current: dict, document: dict) -> bool:
    skip = ("id", "revision", "scope", "updated_at")
    return ({k: v for k, v in current.items() if k not in skip}
            == {k: v for k, v in document.items() if k not in skip})


def show_ref(series_id: str | None) -> str:
    """The key show-scoped rules are kept under: "series:<tvdb id>" for a
    TVDB show (what queued jobs carry), else "series:<series id>"."""
    series = str(series_id or "")
    if series.startswith("show:tvdb:"):
        return f"series:{series.rsplit(':', 1)[-1]}"
    return f"series:{series}" if series.startswith("show:") else ""


def canonical_series(db, series_id: str) -> str:
    """Follow a confirmed link from a local series to the one it really is."""
    seen = set()
    current = series_id
    while current and current not in seen:
        seen.add(current)
        found = records.get(db, "series", current) or {}
        if not found.get("linked_to"):
            return current
        current = found["linked_to"]
    return current


def link_series(db, local_id: str, target_id: str) -> dict:
    """A person confirmed that a local series is a provider series.

    Its characters join the target (merged by name), its media move to the
    target, and the local record keeps a pointer so older references resolve.
    Returns what moved.
    """
    if local_id == target_id:
        raise ValueError("a series cannot be linked to itself")
    moved_characters, moved_media = 0, 0
    for character in characters(db, local_id):
        existing = find_character(db, target_id, character["name"])
        if existing is None:
            records.put(db, "character", character["id"],
                        {**{k: v for k, v in character.items()
                            if k not in ("id", "revision", "scope", "updated_at")},
                         "series_id": target_id}, scope=target_id,
                        base_revision=character["revision"])
        else:
            records.update(db, "character", character["id"], {"merged_into": existing["id"]})
        moved_characters += 1
    for media in records.list_latest(db, "media", scope=local_id):
        records.put(db, "media", media["id"],
                    {**{k: v for k, v in media.items()
                        if k not in ("id", "revision", "scope", "updated_at")},
                     "series_id": target_id}, scope=target_id, base_revision=media["revision"])
        moved_media += 1
    current = records.get(db, "series", local_id) or {}
    records.put(db, "series", local_id, {**{k: v for k, v in current.items()
                                            if k not in ("id", "revision", "scope",
                                                         "updated_at")},
                                         "linked_to": target_id, "linked_at":
                                             records.now_marker()},
                scope=local_id, base_revision=current.get("revision", 0))
    return {"characters": moved_characters, "media": moved_media, "into": target_id}


def ensure_series(db, series_id: str, facts: dict, title: str = "") -> dict:
    def mutate(doc):
        doc.setdefault("created_from", facts)
        doc["provider"] = facts.get("provider", doc.get("provider", "local"))
        if facts.get("folder"):
            folders = list(doc.get("folders") or [])
            if facts["folder"] not in folders:
                folders.append(facts["folder"])
            doc["folders"] = folders
        if title and not doc.get("title"):
            doc["title"] = title
        doc.setdefault("legacy_keys", [])
        return doc
    return _put_merged(db, "series", series_id, mutate, scope=series_id)


def _media_id(series_id: str, hints: dict) -> str | None:
    if hints.get("kind") == "movie" and hints.get("tmdb_id"):
        return f"mv:tmdb:{int(hints['tmdb_id'])}"
    if series_id.startswith("show:tvdb:") and hints.get("season") is not None:
        return f"ep:{series_id[5:]}:s{int(hints['season'])}e{int(hints['episode'])}"
    return None


def find_media_by_content(db, content_key: str) -> dict | None:
    for media in records.list_latest(db, "media"):
        if f"rev-{content_key}" in (media.get("revisions") or {}):
            return media
    return None


def find_media_by_path(db, path: str | Path) -> dict | None:
    """The media a location last belonged to (a location hint, not an identity)."""
    wanted = norm_path(path)
    for media in records.list_latest(db, "media"):
        for revision in (media.get("revisions") or {}).values():
            if wanted in [norm_path(p) for p in revision.get("locations") or []]:
                return media
    return None


def resolve(db, path: str | Path, *, hints: dict | None = None, cache_dir: Path | None = None,
            edition: str = "") -> dict:
    """Identify a file: its series, media and source revision, recording them.

    Returns ``{series_id, media_id, revision_id, content_key, kind, season,
    episode, edition, duration, how}``. ``how`` says which evidence decided the
    media (provider ids, matching content, a known location, or a new local id).
    """
    path = Path(path)
    hints = {**library_hints(db, path), **(hints or {})}
    content = content_identity(path, cache_dir)
    revision_id = f"rev-{content['content_key']}"
    series_id, facts = series_id_for(path, hints)
    series_id = canonical_series(db, series_id)
    media_id = _media_id(series_id, hints)
    how = "provider"
    if media_id is None:
        known = find_media_by_content(db, content["content_key"])
        if known is not None:
            media_id, how = known["id"], "content"
            series_id = canonical_series(db, known.get("series_id") or series_id)
        else:
            previous = find_media_by_path(db, path)
            if previous is not None:
                # New content at a known location: a replaced file or another
                # cut of the same title, so a new revision of the same media.
                media_id, how = previous["id"], "location"
                series_id = canonical_series(db, previous.get("series_id") or series_id)
            else:
                media_id, how = "md-" + uuid.uuid4().hex[:12], "new"
    ensure_series(db, series_id, facts, hints.get("title", "") if hints.get("kind") == "movie"
                  else "")
    edition = edition or edition_hint(path)
    location = str(path)

    def mutate(doc):
        doc["series_id"] = canonical_series(db, doc.get("series_id") or series_id)
        doc["kind"] = hints.get("kind") or doc.get("kind") or "episode"
        for key in ("season", "episode", "tmdb_id", "tvdb_id", "title", "year"):
            if hints.get(key) is not None and doc.get(key) is None:
                doc[key] = hints[key]
        revisions = dict(doc.get("revisions") or {})
        revision = dict(revisions.get(revision_id) or {
            "content_key": content["content_key"], "size": content["size"],
            "duration": content["duration"], "streams": content["streams"],
            "method": content["method"], "edition": edition, "locations": [],
            "first_seen": records.now_marker()})
        if location not in revision["locations"]:
            revision["locations"] = [*revision["locations"], location]
        if edition and not revision.get("edition"):
            revision["edition"] = edition
        revisions[revision_id] = revision
        doc["revisions"] = revisions
        doc.setdefault("legacy_keys", [])
        return doc

    media = _put_merged(db, "media", media_id, mutate, scope=series_id)
    revision = media["revisions"][revision_id]
    return {"series_id": media["series_id"], "media_id": media_id, "revision_id": revision_id,
            "content_key": content["content_key"], "kind": media.get("kind"),
            "season": media.get("season"), "episode": media.get("episode"),
            "edition": revision.get("edition", ""), "duration": revision.get("duration"),
            "how": how}


def episode_order(db, media_id: str) -> tuple[int, int] | None:
    """(season, episode) of a media, for revelation boundaries; None for a movie."""
    media = records.get(db, "media", media_id) or {}
    if media.get("season") is None:
        return None
    return int(media["season"]), int(media.get("episode") or 0)


# --------------------------------------------------------------------------
# Characters
# --------------------------------------------------------------------------

VARIANT_KINDS = ("age", "transformation", "edition", "disguise", "other")


def _fold(text: str) -> str:
    return " ".join(str(text or "").split()).casefold()


def characters(db, series_id: str, *, include_retired: bool = False) -> list[dict]:
    found = records.list_latest(db, "character", scope=series_id)
    return [c for c in found if include_retired or not (c.get("retired") or c.get("merged_into"))]


def find_character(db, series_id: str, name: str) -> dict | None:
    """The live character of a series called `name` (or with it as an alias)."""
    wanted = _fold(name)
    if not wanted:
        return None
    for character in characters(db, series_id):
        if wanted in {_fold(str(character.get("name") or ""))} | {_fold(a) for a in
                                                      character.get("aliases") or []}:
            return character
    return None


def ensure_character(db, series_id: str, name: str, *, origin: str = "manual",
                     role: str = "") -> dict:
    """The character called `name` in a series, created once if new."""
    name = " ".join(str(name or "").split())[:80]
    if not name:
        raise ValueError("a character needs a name")
    found = find_character(db, series_id, name)
    if found is not None:
        return found
    character_id = "chr-" + uuid.uuid4().hex[:12]
    return records.put(db, "character", character_id, {
        "series_id": series_id, "name": name, "aliases": [], "role": role,
        "variants": [], "origin": origin, "retired": False, "merged_into": None,
        "created_at": records.now_marker()}, scope=series_id, create_only=True)


def rename_character(db, character_id: str, name: str, *, base_revision: int | None,
                     keep_alias: bool = True) -> dict:
    current = records.get(db, "character", character_id)
    if current is None:
        raise KeyError(character_id)
    name = " ".join(str(name or "").split())[:80]
    clash = find_character(db, current["series_id"], name)
    if clash is not None and clash["id"] != character_id:
        raise ValueError(f"{name} is already a character in this series; merge them instead")
    aliases = list(current.get("aliases") or [])
    if keep_alias and current.get("name") and _fold(current["name"]) != _fold(name) \
            and current["name"] not in aliases:
        aliases.append(current["name"])
    return records.update(db, "character", character_id, {"name": name, "aliases": aliases},
                          base_revision=base_revision)


def merge_characters(db, keep_id: str, gone_id: str, *, base_revision: int | None) -> dict:
    """Fold one character into another. The gone record stays as history and
    points at the survivor, so old associations still resolve."""
    keep = records.get(db, "character", keep_id)
    gone = records.get(db, "character", gone_id)
    if keep is None or gone is None or keep["series_id"] != gone["series_id"]:
        raise ValueError("both characters must exist in the same series")
    aliases = list(dict.fromkeys([*(keep.get("aliases") or []), gone["name"],
                                  *(gone.get("aliases") or [])]))
    variants = list(keep.get("variants") or [])
    known = {v["id"] for v in variants}
    variants += [v for v in gone.get("variants") or [] if v["id"] not in known]
    merged = records.update(db, "character", keep_id, {"aliases": aliases,
                                                        "variants": variants},
                            base_revision=base_revision)
    records.update(db, "character", gone_id, {"merged_into": keep_id})
    return merged


def add_variant(db, character_id: str, label: str, kind: str = "other", *,
                base_revision: int | None, notes: str = "") -> dict:
    if kind not in VARIANT_KINDS:
        raise ValueError(f"variant kind must be one of {', '.join(VARIANT_KINDS)}")
    current = records.get(db, "character", character_id)
    if current is None:
        raise KeyError(character_id)
    variant = {"id": "var-" + uuid.uuid4().hex[:8], "label": str(label)[:80], "kind": kind,
               "notes": str(notes)[:500]}
    return records.update(db, "character", character_id,
                          {"variants": [*(current.get("variants") or []), variant]},
                          base_revision=base_revision)


def resolve_character(db, character_id: str) -> dict | None:
    """Follow merges to the surviving character."""
    seen = set()
    current = records.get(db, "character", character_id)
    while current is not None and current.get("merged_into") and current["id"] not in seen:
        seen.add(current["id"])
        current = records.get(db, "character", current["merged_into"])
    return current


# --------------------------------------------------------------------------
# Associations: evidence-backed links from a cluster, a track or a line
# --------------------------------------------------------------------------

SUBJECTS = ("cluster", "visual_track", "line", "voice_asset", "performer")
ASSOCIATION_STATES = ("proposed", "accepted", "rejected", "manual")


def association_id(revision_id: str, subject: str, ref: str) -> str:
    return "as-" + hashlib.sha256(f"{revision_id}|{subject}|{ref}".encode()).hexdigest()[:16]


def associate(db, revision_id: str, subject: str, ref: str, character_id: str | None, *,
              state: str = "manual", evidence: list[dict] | None = None,
              variant_id: str = "", locked: bool | None = None,
              base_revision: int | None = None) -> dict:
    """Link one subject in one source revision to a character (or to nobody).

    A ``manual`` association is a person's decision and is locked by default,
    so an automatic proposal never replaces it.
    """
    if subject not in SUBJECTS:
        raise ValueError(f"unknown association subject {subject!r}")
    if state not in ASSOCIATION_STATES:
        raise ValueError(f"unknown association state {state!r}")
    record_id = association_id(revision_id, subject, ref)
    current = records.get(db, "association", record_id)
    if current and current.get("locked") and state == "proposed":
        return current
    document = {"revision_id": revision_id, "subject": subject, "ref": ref,
                "character_id": character_id, "variant_id": variant_id, "state": state,
                "evidence": list(evidence or [])[:20],
                "locked": (state == "manual") if locked is None else bool(locked),
                "at": records.now_marker()}
    return records.put(db, "association", record_id, document, scope=revision_id,
                       base_revision=base_revision if base_revision is not None
                       else (current["revision"] if current else 0))


def associations(db, revision_id: str, subject: str | None = None) -> list[dict]:
    rows = records.list_latest(db, "association", scope=revision_id)
    return [r for r in rows if subject is None or r.get("subject") == subject]


def cluster_characters(db, revision_id: str) -> dict[str, dict]:
    """Voice group label -> character record, for one source revision."""
    out = {}
    for row in associations(db, revision_id, "cluster"):
        if row.get("state") in ("manual", "accepted") and row.get("character_id"):
            character = resolve_character(db, row["character_id"])
            if character is not None:
                out[row["ref"]] = character
    return out
