"""Episode inventory and explicit per-file queueing for Sonarr series."""

import threading
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import AfterValidator, BaseModel, Field

from ..artifacts import read_json
from ..cache import TTLCache
from ..discovery import _audio_iso2, _name_to_iso2
from ..knowledge import snapshot as knowledge_snapshot
from ..languages import base_language, normalize
from ..languages import parse as parse_language_tag
from ..voices import cast_key


def normalized(path):
    return str(path or "").replace("\\", "/").rstrip("/").casefold()


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
    kind: Literal["full", "tease", "audition"] = "full"
    missing_only: bool = True


def build_router(config, services, store, bus):
    api = APIRouter()
    cache = TTLCache(max_size=32)
    lock = threading.Lock()

    def inventory(tvdb_id, refresh=False):
        cached = cache.get(tvdb_id, ttl=60) if not refresh else None
        if cached is not None:
            return cached
        client = services.sonarr
        show = next((s for s in client.list_series() if s.get("tvdbId") == tvdb_id), None)
        if not show:
            raise HTTPException(404, "Show not found in Sonarr")
        result = (show, client.episodes(show["id"]), client.episode_files(show["id"]))
        cache.set(tvdb_id, result)
        return result

    def output_exists(job):
        if job.get("status") != "done" or job.get("kind", "full") != "full":
            return False
        if not job.get("output_file"):
            return False
        path = Path(job["output_file"]).resolve()
        if not any(
            path.is_relative_to(root.resolve()) for root in (config.output_dir, config.work_dir)
        ):
            return False
        report = read_json(Path(job["report_file"])) if job.get("report_file") else {}
        return path.is_file() and not report.get("dry_run", False)

    def detail(tvdb_id, target, refresh=False):
        show, episodes, files = inventory(tvdb_id, refresh)
        indexed = {f["id"]: f for f in files}
        original = _name_to_iso2((show.get("originalLanguage") or {}).get("name"))
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
                and normalized(j.get("input_file")) == normalized(path)
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
            "path": show.get("path"),
            "original": original,
            "media_type": "show",
            "target_lang": target,
            "episodes": rows,
            "downloaded": sum(r["downloaded"] for r in rows),
            "dubbed": sum(r["dubbed"] for r in rows),
            "total": len(rows),
        }

    @api.get("/api/series/{tvdb_id}/episodes")
    def get_episodes(
        tvdb_id: int,
        target_lang: LanguageQuery,
        refresh: bool = False,
    ):
        return detail(tvdb_id, target_lang, refresh)

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
                    if body.missing_only and row["dubbed"]
                    else "Shares a queued episode file"
                    if normalized(path) in paths
                    else "File is not accessible to Doblarr"
                    if not Path(path).is_file()
                    else None
                )
                if reason:
                    skipped.append({"id": eid, "reason": reason})
                    continue
                paths.add(normalized(path))
                episode_plan = (store.db.load_plan(cast_key(path=path)) or {}).get("plan", {})
                overrides = {**plan, **episode_plan}
                overrides.pop("target_lang", None)
                job = store.add(
                    title=(
                        f"{data['title']} S{row['season']:02}E{row['episode']:02} — {row['title']}"
                    ),
                    source="Sonarr · Shows",
                    source_lang=data["original"] or "auto",
                    target_lang=base,
                    target_locale=locale,
                    input_file=path,
                    kind=body.kind,
                    overrides=overrides,
                    knowledge_snapshot=knowledge_snapshot(store.db),
                    show_ref=f"series:{tvdb_id}",
                )
                queued.append({"episode_id": eid, "job_id": job.id})
                bus.publish("job", {"type": "queued", "job_id": job.id, "title": job.title})
            return {"queued": queued, "skipped": skipped}

    return api
