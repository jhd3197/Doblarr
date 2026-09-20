"""Episode inventory and explicit per-file queueing for Sonarr series."""

import threading
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from ..artifacts import read_json
from ..cache import TTLCache
from ..discovery import _audio_iso2, _name_to_iso2
from ..voices import cast_key


def normalized(path):
    return str(path or "").replace("\\", "/").rstrip("/").casefold()


class EpisodeQueueIn(BaseModel):
    episode_ids: list[int] = Field(min_length=1, max_length=2000)
    target_lang: str = Field(pattern=r"^[a-zA-Z]{2,3}(?:-[a-zA-Z]{2,4})?$")
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
                    {target},
                    "unknown",
                )
            )
            matched = [
                j
                for j in jobs
                if path
                and normalized(j.get("input_file")) == normalized(path)
                and j.get("target_lang", "").lower() == target
            ]
            active = next((j for j in matched if j["status"] in {"queued", "running"}), None)
            completed = next((j for j in matched if output_exists(j)), None)
            status = (
                "audio-present"
                if target in audio
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
                    "dubbed": target in audio or bool(completed),
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
        target_lang: str = Query(pattern=r"^[a-zA-Z]{2,3}(?:-[a-zA-Z]{2,4})?$"),
        refresh: bool = False,
    ):
        return detail(tvdb_id, target_lang.lower(), refresh)

    @api.post("/api/series/{tvdb_id}/queue")
    def queue_episodes(tvdb_id: int, body: EpisodeQueueIn):
        with lock:
            data = detail(tvdb_id, body.target_lang.lower(), refresh=True)
            rows = {r["id"]: r for r in data["episodes"]}
            if set(body.episode_ids) - set(rows):
                raise HTTPException(422, "An episode is no longer in this show; refresh the list")
            plan = (store.db.load_plan(cast_key(path=data["path"])) or {}).get("plan", {})
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
                    target_lang=body.target_lang.lower(),
                    input_file=path,
                    kind=body.kind,
                    overrides=overrides,
                )
                queued.append({"episode_id": eid, "job_id": job.id})
                bus.publish("job", {"type": "queued", "job_id": job.id, "title": job.title})
            return {"queued": queued, "skipped": skipped}

    return api
