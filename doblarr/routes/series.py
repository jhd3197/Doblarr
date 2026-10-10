"""Episode inventory and explicit per-file queueing for Sonarr series."""

import json
import threading
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import AfterValidator, BaseModel, Field

from .. import analysis, original_language, plex_library, speaking
from ..artifacts import read_json
from ..cache import TTLCache
from ..clients.plex import PlexError
from ..discovery import _audio_iso2, _name_to_iso2
from ..errors import ConfigError
from ..fileio import inside
from ..knowledge import snapshot as knowledge_snapshot
from ..languages import base_language, normalize
from ..languages import parse as parse_language_tag
from ..plex_library import library_path_key
from ..voices import cast_key
from .analysis import identify, load_names


def _language_tag(value: str) -> str:
    parsed = parse_language_tag(value)
    if parsed is None:
        raise ValueError("must be a language tag such as es, es-MX or es-419")
    return parsed


LanguageTag = Annotated[str, AfterValidator(_language_tag)]
LanguageQuery = Annotated[str, AfterValidator(_language_tag), Query()]


def job_locale(job) -> str:
    """Resolved target locale of a stored job row; legacy rows derive from target_lang."""
    return normalize(job.get("target_locale") or job.get("target_lang") or "") or ""


class EpisodeQueueIn(BaseModel):
    episode_ids: list[int] = Field(min_length=1, max_length=2000)
    target_lang: LanguageTag
    kind: Literal["full", "tease", "audition", "analyze"] = "full"
    missing_only: bool = True


class OriginalIn(BaseModel):
    lang: str = Field(default="", pattern=r"^([a-z]{2})?$")


def build_router(config, services, store, bus):
    api = APIRouter()
    cache = TTLCache(max_size=32)
    lock = threading.Lock()

    def inventory(tvdb_id, refresh=False):
        cached = cache.get(tvdb_id, ttl=60) if not refresh else None
        if cached is not None:
            return cached
        result = None
        try:
            client = services.sonarr
            show = next((s for s in client.list_series() if s.get("tvdbId") == tvdb_id), None)
            if show:
                result = (show, client.episodes(show["id"]),
                          client.episode_files(show["id"]))
        except ConfigError:
            pass  # no Sonarr; Plex may still have the show
        if result is None:
            # A show only Plex has: the same shape, with Plex rating keys as ids.
            try:
                result = plex_library.show_inventory(
                    services.plex, tvdb_id,
                    plex_library.AudioCache(config.work_dir / plex_library.CACHE_FILE))
            except (ConfigError, PlexError):
                result = None
        if result is None:
            raise HTTPException(404, "Show not found in Sonarr or Plex")
        cache.set(tvdb_id, result)
        return result

    def output_exists(job):
        if job.get("status") != "done" or job.get("kind", "full") != "full":
            return False
        if not job.get("output_file"):
            return False
        if not inside(job["output_file"], (config.output_dir, config.work_dir)):
            return False
        report = read_json(Path(job["report_file"])) if job.get("report_file") else {}
        return Path(job["output_file"]).is_file() and not report.get("dry_run", False)

    def detail(tvdb_id, target, refresh=False, only=None):
        """The show's episodes with their dub status; `only` keeps one episode id."""
        show, episodes, files = inventory(tvdb_id, refresh)
        if only is not None:
            episodes = [ep for ep in episodes if ep.get("id") == only]
        indexed = {f["id"]: f for f in files}
        metadata = _name_to_iso2((show.get("originalLanguage") or {}).get("name"))
        # A show only Plex has carries no original language: the episode files
        # tell (doblarr.original_language), probed once and kept for the show.
        spoken = original_language.for_title(
            store.db, original_language.cache_key("show", tvdb_id),
            [f.get("path") for f in files], metadata)
        original = spoken.get("lang") or metadata
        base = base_language(target)  # media audio tags are base-language only
        jobs = store.list()
        rows = []
        for ep in sorted(
            episodes, key=lambda e: (e.get("seasonNumber", 0), e.get("episodeNumber", 0))
        ):
            media = indexed.get(ep.get("episodeFileId"), {})
            path = media.get("path")
            audio = sorted(
                _audio_iso2(
                    (media.get("mediaInfo") or {}).get("audioLanguages"),
                    original,
                    {base},
                    "unknown",
                )
            )
            matched = [
                j
                for j in jobs
                if path
                and library_path_key(j.get("input_file")) == library_path_key(path)
                and job_locale(j) == target
            ]
            active = next((j for j in matched if j["status"] in {"queued", "running"}), None)
            completed = next((j for j in matched if output_exists(j)), None)
            status = (
                "audio-present"
                if base in audio
                else "dub-ready"
                if completed
                else active["status"]
                if active
                else "not-downloaded"
                if not path
                else "failed"
                if matched and matched[0]["status"] == "failed"
                else "unknown-audio"
                if not audio
                else "needs-dub"
            )
            rows.append(
                {
                    "id": ep["id"],
                    "season": ep.get("seasonNumber", 0),
                    "episode": ep.get("episodeNumber", 0),
                    "title": ep.get("title", "Untitled"),
                    "path": path,
                    "audio_langs": audio,
                    "status": status,
                    "downloaded": bool(path),
                    "dubbed": base in audio or bool(completed),
                    "job_id": active["id"] if active else None,
                    "output_job_id": completed["id"] if completed else None,
                }
            )
        return {
            "title": show["title"],
            "source": "Plex · Shows" if show.get("source") == "Plex" else "Sonarr · Shows",
            "path": show.get("path"),
            "original": original,
            "original_from": {k: spoken.get(k) for k in ("source", "confidence", "evidence",
                                                         "candidates", "probed")},
            "media_type": "show",
            "target_lang": target,
            "episodes": rows,
            "downloaded": sum(r["downloaded"] for r in rows),
            "dubbed": sum(r["dubbed"] for r in rows),
            "total": len(rows),
        }

    def named_voices(title_key: str) -> dict[str, dict]:
        """Catalogue voices a person tied to this show, by character."""
        found = {}
        for row in store.db.query("SELECT title_key, plan FROM title_plans "
                                  "WHERE title_key LIKE 'voice-traits:%'"):
            traits = json.loads(row["plan"] or "{}")
            if traits.get("show") == title_key and traits.get("character"):
                found[traits["character"].upper()] = {
                    **traits, "key": row["title_key"].removeprefix("voice-traits:")}
        return found

    @api.get("/api/series/{tvdb_id}/voices")
    def get_voices(tvdb_id: int, refresh: bool = False):
        """Every character in the show's run scripts: share of talk, range, voice."""
        show, episodes, files = inventory(tvdb_id, refresh)
        indexed = {f["id"]: f for f in files}
        seen: list[dict] = []
        casts: dict[str, dict] = {}
        counted: set[str] = set()
        for ep in sorted(episodes, key=lambda e: (e.get("seasonNumber", 0),
                                                  e.get("episodeNumber", 0))):
            path = (indexed.get(ep.get("episodeFileId")) or {}).get("path")
            # A double episode is one file listed twice; its talk counts once.
            if not path or library_path_key(path) in counted:
                continue
            script = speaking.find_script(config.work_dir, path)
            if script is None:
                continue
            counted.add(library_path_key(path))
            label = f"S{ep.get('seasonNumber', 0):02d}E{ep.get('episodeNumber', 0):02d}"
            # Voice groups are per episode; a name a person gave one is what
            # makes SPEAKER_03 here and SPEAKER_01 there the same character.
            names = load_names(store.db, path)
            segments = [{**s, "speaker": names.get(s.get("speaker") or "") or s.get("speaker")}
                        for s in speaking.load_segments(script)]
            seen.append({"id": ep["id"], "label": label, "title": ep.get("title", ""),
                         "segments": segments})
            # A cast is keyed by the path the run used, which may be a copy of the
            # episode elsewhere; the file name is what they share.
            name = Path(str(path).replace("\\", "/")).name.casefold()
            for saved in store.db.list_casts():
                if Path(saved["title_key"].replace("\\", "/")).name.casefold() != name:
                    continue
                for entry in saved["cast"]:
                    if entry.get("voice"):
                        casts.setdefault(entry.get("speaker_id"), entry)
        result = speaking.talk_share(seen)
        named = named_voices(f"tvdb-{tvdb_id}")
        for row in result["speakers"]:
            entry = casts.get(row["speaker"]) or {}
            identity = named.get(row["speaker"].upper())
            row["label"] = entry.get("label") or row["speaker"]
            row["voice"] = ({"key": identity["key"], "name": identity.get("display_name")
                             or row["speaker"], "gender": identity.get("gender"),
                             "age": identity.get("age"), "color": identity.get("color") or ""}
                            if identity else
                            {"key": f"profile:{entry['voice']}", "name": None}
                            if entry.get("voice") else None)
        return {**result, "title": show["title"],
                "analysed": [{k: e[k] for k in ("id", "label", "title")} for e in seen],
                "episode_count": len(episodes)}

    @api.get("/api/series/{tvdb_id}/episodes")
    def get_episodes(
        tvdb_id: int,
        target_lang: LanguageQuery,
        refresh: bool = False,
    ):
        return detail(tvdb_id, target_lang, refresh)

    @api.get("/api/series/{tvdb_id}/episodes/{episode_id}")
    def get_episode(tvdb_id: int, episode_id: int, target_lang: LanguageQuery):
        """One episode and its show, so an episode page doesn't list the whole series."""
        result = detail(tvdb_id, target_lang, only=episode_id)
        if not result["episodes"]:
            raise HTTPException(404, "Episode not found in Sonarr or Plex")
        return {"episode": result["episodes"][0], "target_lang": target_lang,
                "show": {k: result[k] for k in ("title", "source", "path", "original")}
                | {"tvdb_id": tvdb_id}}

    @api.put("/api/series/{tvdb_id}/original-language")
    def set_original(tvdb_id: int, body: OriginalIn):
        """A person says what the show was made in; it outranks any detection."""
        key = original_language.cache_key("show", tvdb_id)
        if not body.lang:
            store.db.save_plan(key, "Original language", {})
            return {"lang": "", "source": "cleared"}
        return original_language.remember(store.db, key, {
            "lang": body.lang, "confidence": "manual", "evidence": ["set by hand"]},
            source="manual")

    @api.post("/api/series/{tvdb_id}/queue")
    def queue_episodes(tvdb_id: int, body: EpisodeQueueIn):
        with lock:
            data = detail(tvdb_id, body.target_lang, refresh=True)
            rows = {r["id"]: r for r in data["episodes"]}
            if set(body.episode_ids) - set(rows):
                raise HTTPException(422, "An episode is no longer in this show; refresh the list")
            plan = (store.db.load_plan(cast_key(path=data["path"])) or {}).get("plan", {})
            base = base_language(body.target_lang)
            locale = body.target_lang if body.target_lang != base else ""
            queued, skipped, paths = [], [], set()
            for eid in dict.fromkeys(body.episode_ids):
                row = rows[eid]
                path = row["path"]
                reason = (
                    "Not downloaded"
                    if not path
                    else "Already queued or running"
                    if row["job_id"]
                    else "Target audio already available"
                    if body.missing_only and row["dubbed"] and body.kind != "analyze"
                    else "Shares a queued episode file"
                    if library_path_key(path) in paths
                    else "File is not accessible to Doblarr"
                    if not Path(path).is_file()
                    else None
                )
                if reason:
                    skipped.append({"id": eid, "reason": reason})
                    continue
                paths.add(library_path_key(path))
                episode_plan = (store.db.load_plan(cast_key(path=path)) or {}).get("plan", {})
                overrides = {**plan, **episode_plan}
                overrides.pop("target_lang", None)
                run = {"source_lang": data["original"] or "auto", "target_lang": base,
                       "target_locale": locale, "input_file": path}
                if body.kind == "analyze":
                    # Analysing again runs the way the earlier analysis did:
                    # another target would read other subtitles and cut and
                    # group the lines anew.
                    run = analysis.earlier_run(
                        store.db, config.work_dir, path,
                        identify(store.db, path, Path(config.work_dir) / "cache")) or run
                job = store.add(
                    title=(
                        f"{data['title']} S{row['season']:02}E{row['episode']:02} — {row['title']}"
                    ),
                    source=data.get("source") or "Sonarr · Shows",
                    **run,
                    kind=body.kind,
                    overrides=overrides,
                    knowledge_snapshot=knowledge_snapshot(store.db),
                    show_ref=f"series:{tvdb_id}",
                )
                queued.append({"episode_id": eid, "job_id": job.id})
                bus.publish("job", {"type": "queued", "job_id": job.id, "title": job.title})
            return {"queued": queued, "skipped": skipped}

    return api
