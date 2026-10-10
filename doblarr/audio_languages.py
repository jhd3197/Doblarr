"""Audio track languages across the library, and making them consistent.

Players pick audio by its language tag and show its title. Libraries collect
tracks from many sources, so the same language turns up as "Latino",
"Esp LAT" and "Spanish 2.0", untagged ("und"), or tagged one language while
the title names another. This module reads every track's tag and title from
Plex (sharing the library scan's cache, so a rescan only asks about changed
files), groups them by the language they are, and rewrites the ones the user
picks.

What a track *is* comes from its title when the title names a language (a
release group writes "Korean" more carefully than a muxer fills the tag),
else from its tag. Nothing here listens to the audio: an untagged track with
an uninformative title stays unknown until the user says what it is.

Rewriting changes metadata only. With mkvpropedit on the PATH a Matroska file
is edited in place; anything else is remuxed by ffmpeg into a copy beside it,
checked to hold the same streams, and only then swapped in.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import logging
import re
import shutil
import subprocess
import threading
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import plex_library
from .ffmpeg import run_ffmpeg, run_ffprobe
from .languages import catalog, get, iso3_for, normalize

log = logging.getLogger("doblarr.audio_languages")

UNKNOWN = "und"
CHANNELS = {1: "1.0", 2: "2.0", 3: "2.1", 6: "5.1", 7: "6.1", 8: "7.1"}
EXAMPLES = 4   # titles listed under each variant

# Words in track titles that name a language. Catalog names, native names and
# ISO 639-2 codes are added below; these are what release groups also write.
_WORDS = {
    "latino": "es-419", "lat": "es-419", "latam": "es-419", "latin": "es-419",
    "latinoamericano": "es-419", "castellano": "es-ES", "castilian": "es-ES",
    "españa": "es-ES", "spain": "es-ES", "argentino": "es-AR", "rioplatense": "es-AR",
    "colombiano": "es-CO", "chileno": "es-CL", "español": "es", "espanol": "es",
    "spanish": "es", "esp": "es", "eng": "en", "english": "en", "deutsch": "de",
    "francais": "fr", "français": "fr", "italiano": "it", "portugues": "pt",
    "português": "pt", "brazilian": "pt", "jap": "ja", "jpn": "ja", "japanese": "ja",
}
for _e in catalog():
    if _e.id == _e.base:
        _WORDS.setdefault(_e.name.lower(), _e.id)
        _WORDS.setdefault(_e.native_name.lower(), _e.id)
        if _e.iso3:
            _WORDS.setdefault(_e.iso3, _e.id)
for _a in ("zho", "deu", "fra", "nld", "ell", "ces", "ron", "fas", "mandarin", "cantonese"):
    _WORDS.setdefault(_a, normalize(_a) or _a)


def track_name(lang: str) -> str:
    """How a language reads in a track title: 'Japanese', 'Spanish (Latin America)'."""
    entry = get(lang)
    if not entry:
        return lang.upper()
    if entry.region == "419":
        return entry.name.split(" — ")[0] + " (Latin America)"
    if " — " in entry.name:
        language, region = entry.name.split(" — ", 1)
        return f"{language} ({region})"
    return entry.name


def channels_label(count: int) -> str:
    return CHANNELS.get(count, f"{count}ch" if count else "")


def title_language(title: str) -> str | None:
    """The language a track title names, regional when it says so ('Esp LAT' -> es-419)."""
    found = [_WORDS[w] for w in re.findall(r"\w+", title.casefold()) if w in _WORDS]
    regional = [f for f in found if "-" in f]
    if regional:
        return regional[0]
    return found[0] if found else None


def classify(track: dict, original: str | None) -> dict:
    """The language a track is, whether its tag disagrees, and what it is to the title."""
    title = track.get("title") or ""
    words = set(re.findall(r"\w+", title.casefold()))
    tagged = None if track["lang"] in ("", UNKNOWN) else normalize(track["lang"])
    named = title_language(title)
    lang = named or tagged
    # A title that only says "Spanish" keeps a regional tag's region.
    if named and tagged and named == tagged.split("-")[0]:
        lang = tagged
    mismatch = bool(named and tagged and named.split("-")[0] != tagged.split("-")[0])
    if "commentary" in words:
        kind = "Commentary"
    elif "ai" in words or "doblarr" in words:
        kind = "AI"
    elif original and lang:
        kind = "Original" if lang.split("-")[0] == original.split("-")[0] else "Dub"
    else:
        kind = ""
    return {"lang": lang or UNKNOWN, "mismatch": mismatch, "kind": kind}


def _variant_id(*parts) -> str:
    return hashlib.sha1("\x1f".join(map(str, parts)).encode("utf-8")).hexdigest()[:12]


def _label(item: dict, movie: bool) -> str:
    title = item.get("title", "?")
    return f"{title} ({item['year']})" if movie and item.get("year") else title


def read_tracks(client, cache: plex_library.AudioCache,
                originals=lambda item, movie: None) -> list[dict]:
    """Every audio track in Plex's movie and show libraries, one row per track.

    `originals(item, movie)` names a title's original language when known; it
    decides whether a track is the original or a dub.
    """
    leaves: list[tuple[dict, dict, bool, str | None]] = []
    for section in client.sections():
        if section["type"] not in ("movie", "show"):
            continue
        movie = section["type"] == "movie"
        for item in client.section_items(section["key"]):
            original = originals(item, movie)
            for leaf in [item] if movie else client.leaves(item["ratingKey"]):
                leaves.append((item, leaf, movie, original))

    missing = [leaf for _i, leaf, _m, _o in leaves
               if cache.tracks(leaf["ratingKey"], leaf.get("updatedAt")) is None]
    if missing:
        log.info("plex: reading audio tracks of %d file(s)", len(missing))

        def probe(leaf):
            try:
                meta = client.metadata(leaf["ratingKey"])
                return leaf, client.audio_languages(meta), plex_library.audio_tracks(meta)
            except Exception as exc:  # noqa: BLE001 - one unreadable file must not end the scan
                log.warning("plex: %s: %s", leaf.get("title"), exc)
                return leaf, None, None

        with ThreadPoolExecutor(max_workers=plex_library.WORKERS) as pool:
            for leaf, langs, tracks in pool.map(probe, missing):
                if tracks is not None:
                    cache.put(leaf["ratingKey"], leaf.get("updatedAt"), langs, tracks)
        cache.save()

    rows = []
    for item, leaf, movie, original in leaves:
        for track in cache.tracks(leaf["ratingKey"], leaf.get("updatedAt")) or []:
            # `tag` is the tag as found; `lang` (from classify) the language the track is.
            rows.append({**track, "tag": track["lang"], **classify(track, original),
                         "key": str(leaf["ratingKey"]), "label": _label(item, movie)})
    return rows


def summarize(rows: list[dict]) -> dict:
    """Tracks grouped by language, then by how they are tagged and titled now."""
    groups: dict[str, dict] = defaultdict(dict)
    for row in rows:
        ch = channels_label(row["channels"])
        vid = _variant_id(row["lang"], row["tag"], row["title"], ch, row["kind"])
        variant = groups[row["lang"]].setdefault(vid, {
            "id": vid, "tag": row["tag"], "title": row["title"], "channels": ch,
            "kind": row["kind"], "mismatch": row["mismatch"], "tracks": 0, "where": Counter()})
        variant["tracks"] += 1
        variant["where"][row["label"]] += 1

    # The usual title for a language at a channel count is what most of its tracks use.
    usual: dict[tuple[str, str], str] = {}
    for lang, variants in groups.items():
        counts: Counter = Counter()
        for v in variants.values():
            counts[(v["channels"], v["title"])] += v["tracks"]
        for (ch, title), _n in sorted(counts.items(), key=lambda kv: -kv[1]):
            usual.setdefault((lang, ch), title)

    buckets: list[dict] = []
    totals: Counter[str] = Counter()
    for lang, variants in groups.items():
        listed = []
        for v in sorted(variants.values(), key=lambda v: -v["tracks"]):
            where = v.pop("where")
            examples = [{"label": k, "tracks": n} for k, n in where.most_common(EXAMPLES)]
            listed.append({**v, "where": examples, "titles": len(where)})
            totals["tracks"] += v["tracks"]
            if v["tag"] in ("", UNKNOWN):
                totals["und"] += v["tracks"]
            if v["mismatch"]:
                totals["mismatched_tag"] += v["tracks"]
            if lang != UNKNOWN and v["title"] != usual[(lang, v["channels"])]:
                totals["inconsistent_titles"] += v["tracks"]
            if v["tag"] in ("", UNKNOWN) or v["mismatch"] or (
                    lang != UNKNOWN and v["title"] != usual[(lang, v["channels"])]):
                totals["to_fix"] += v["tracks"]
        buckets.append({
            "lang": lang, "name": "Unknown language" if lang == UNKNOWN else track_name(lang),
            "code": UNKNOWN if lang == UNKNOWN else iso3_for(lang),
            "tracks": sum(v["tracks"] for v in listed), "variants": listed})
    buckets.sort(key=lambda b: (b["lang"] != UNKNOWN, -b["tracks"]))

    files = defaultdict(list)
    for row in rows:
        files[row["file"]].append(row)
    totals["wrong_default"] = sum(1 for tracks in files.values()
                                  if len(tracks) > 1 and not any(t["default"] for t in tracks))
    for key in ("tracks", "to_fix", "und", "mismatched_tag", "inconsistent_titles"):
        totals.setdefault(key, 0)
    return {"buckets": buckets, "totals": dict(totals)}


def languages() -> list[dict]:
    """What a track can be set to: every base language and regional locale."""
    return [{"id": e.id, "name": track_name(e.id), "code": iso3_for(e.id)} for e in catalog()]


class AudioLanguages:
    """The last scan, and at most one rewrite running in the background."""

    def __init__(self, client_of, cache_of, originals, dry_run_of, refresh=None):
        self.client_of, self.cache_of, self.originals = client_of, cache_of, originals
        self.dry_run_of, self.refresh = dry_run_of, refresh
        self.lock = threading.Lock()
        self.rows: list[dict] = []
        self.scanned_at = ""
        self.job: dict = {"state": "idle"}

    def scan(self, force: bool = False) -> dict:
        with self.lock:
            if force or not self.scanned_at:
                self.rows = read_tracks(self.client_of(), self.cache_of(), self.originals)
                self.scanned_at = _dt.datetime.now().isoformat(timespec="seconds")
            return {"scanned_at": self.scanned_at, **summarize(self.rows),
                    "languages": languages(), "dry_run": bool(self.dry_run_of()),
                    "writer": "mkvpropedit" if shutil.which("mkvpropedit") else "ffmpeg",
                    "apply": self.job}

    def plan(self, scanned_at: str, changes: list[dict]) -> dict[str, list[dict]]:
        """Edits per file from the variants the user picked, as {file: [edit]}."""
        if scanned_at != self.scanned_at:
            raise ValueError("The library changed since this page loaded; reload and try again.")
        wanted = {c["id"]: c for c in changes}
        by_file: dict[str, list[dict]] = defaultdict(list)
        for row in self.rows:
            vid = _variant_id(row["lang"], row["tag"], row["title"],
                              channels_label(row["channels"]), row["kind"])
            change = wanted.get(vid)
            if not change:
                continue
            lang = str(change.get("language") or "")
            code = iso3_for(lang) if lang and lang != UNKNOWN else UNKNOWN
            if lang and lang != UNKNOWN and not get(lang):
                raise ValueError(f"Unknown language {lang!r}")
            title = str(change.get("title") or "").strip()[:200]
            if code == row["tag"] and title == row["title"]:
                continue
            by_file[row["file"]].append({"key": row["key"], "pos": row["pos"],
                                         "was_tag": row["tag"], "was_title": row["title"],
                                         "tag": code, "title": title})
        return dict(by_file)

    def start(self, scanned_at: str, changes: list[dict]) -> dict:
        if self.job.get("state") == "running":
            raise RuntimeError("Tracks are already being rewritten.")
        edits = self.plan(scanned_at, changes)
        tracks = sum(len(e) for e in edits.values())
        if self.dry_run_of():
            self.job = {"state": "planned", "files": len(edits), "tracks": tracks, "done": 0,
                        "failed": [], "message": "Dry run is on: nothing was written."}
            return self.job
        self.job = {"state": "running", "files": len(edits), "tracks": tracks, "done": 0,
                    "failed": []}
        threading.Thread(target=self._run, args=(edits,), daemon=True,
                         name="audio-languages").start()
        return self.job

    def _run(self, edits: dict[str, list[dict]]) -> None:
        cache = self.cache_of()
        for file, file_edits in edits.items():
            try:
                rewrite(Path(file), file_edits)
                for key in {e["key"] for e in file_edits}:
                    cache.forget(key)
                    if self.refresh:
                        try:
                            self.refresh(key)
                        except Exception as exc:  # noqa: BLE001 - Plex catches up on its next scan
                            log.warning("plex refresh %s: %s", key, exc)
            except Exception as exc:  # noqa: BLE001 - one file's failure must not stop the rest
                log.warning("audio languages: %s: %s", file, exc)
                self.job["failed"].append({"file": Path(file).name, "error": str(exc)})
            self.job["done"] += 1
        cache.save()
        failed = len(self.job["failed"])
        self.job.update(state="done", message=(f"{failed} file(s) could not be rewritten." if failed
                                               else "Every picked track was rewritten."))
        # The next page load reads the rewritten files again.
        self.scanned_at = ""


def _probe_audio(path: Path) -> tuple[int, list[dict]]:
    import json

    data = json.loads(run_ffprobe(["-v", "error", "-show_entries",
                                   "stream=index,codec_type:stream_tags=language,title",
                                   "-of", "json", str(path)]))
    streams = data.get("streams") or []
    audio = [{"lang": str((s.get("tags") or {}).get("language") or UNKNOWN).lower(),
              "title": str((s.get("tags") or {}).get("title") or "")}
             for s in streams if s.get("codec_type") == "audio"]
    return len(streams), audio


def rewrite(path: Path, edits: list[dict]) -> str:
    """Set the language tag and title of some of a file's audio tracks.

    Refuses when the file's tracks no longer read as they did in the scan, so
    a file replaced since then is never edited by position. Returns the tool used.
    """
    if not path.is_file():
        raise FileNotFoundError(
            f"{path} is not reachable from Doblarr (is Plex's path the same here?)")
    count, audio = _probe_audio(path)
    for e in edits:
        now = audio[e["pos"]] if e["pos"] < len(audio) else None
        # Plex and ffprobe may spell one tag differently (ger/deu), so compare languages.
        if now is None or now["title"] != e["was_title"] or (
                normalize(now["lang"]) if now["lang"] != UNKNOWN else None) != (
                normalize(e["was_tag"]) if e["was_tag"] not in ("", UNKNOWN) else None):
            raise ValueError("the file changed since the scan; rescan and try again")

    if path.suffix.lower() == ".mkv" and shutil.which("mkvpropedit"):
        args = ["mkvpropedit", str(path)]
        for e in edits:
            args += ["--edit", f"track:a{e['pos'] + 1}", "--set", f"language={e['tag']}"]
            args += ["--set", f"name={e['title']}"] if e["title"] else ["--delete", "name"]
        done = subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", check=False, timeout=300)
        if done.returncode not in (0, 1):   # 1 is mkvpropedit's "finished with warnings"
            tail = (done.stdout or done.stderr).strip().splitlines()
            raise RuntimeError(tail[-1] if tail else "mkvpropedit failed")
        return "mkvpropedit"

    temp = path.with_name(path.stem + ".partial" + path.suffix)
    args = ["-y", "-i", str(path), "-map", "0", "-c", "copy"]
    for e in edits:
        args += [f"-metadata:s:a:{e['pos']}", f"language={e['tag']}",
                 f"-metadata:s:a:{e['pos']}", f"title={e['title']}"]
    try:
        run_ffmpeg([*args, str(temp)])
        written, _ = _probe_audio(temp)
        if written != count:
            raise RuntimeError(f"the rewritten copy has {written} streams, the file {count}; "
                               "left untouched")
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)
    return "ffmpeg"
