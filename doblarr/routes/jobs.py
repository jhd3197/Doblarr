"""Job queue controls and guarded output streaming."""

import os
import re
from dataclasses import asdict
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from ..config import Config
from ..errors import ForbiddenError, NotFoundError
from ..events import EventBus
from ..jobs import JobStore, Worker


class JobCreateIn(BaseModel):
    title: str = Field(min_length=1)
    source: str = "manual"
    source_lang: str = "auto"
    target_lang: str | None = None
    path: str | None = None
    kind: Literal["full", "tease"] = "full"
    force: bool = False  # re-run every stage, ignoring cached artifacts
    overrides: dict[str, Any] | None = None  # per-title config overrides


def build_router(config: Config, store: JobStore, worker: Worker,
                 bus: EventBus) -> APIRouter:
    api = APIRouter()

    def _allowed_path(path_str: str) -> Path | None:
        # Read live: saving an output directory must also update the download guard.
        try:
            allowed_roots = [Path(os.path.normcase(str(root.resolve())))
                             for root in (config.output_dir, config.work_dir)]
            p = Path(os.path.normcase(str(Path(path_str).resolve())))
        except OSError:
            return None
        for root in allowed_roots:
            try:
                p.relative_to(root)
                return Path(path_str).resolve()
            except ValueError:
                continue
        return None

    def _ranged_response(path: Path, range_header: str | None):
        """Serve `path`, honoring `Range: bytes=...` for browser video seeking
        (starlette 0.38's FileResponse does not do ranges)."""
        media_type = {".mkv": "video/x-matroska", ".mp4": "video/mp4",
                      ".m4v": "video/mp4"}.get(path.suffix.lower(),
                                               "application/octet-stream")
        size = path.stat().st_size
        base_headers = {"Accept-Ranges": "bytes"}
        m = re.fullmatch(r"bytes=(\d*)-(\d*)", (range_header or "").strip())
        if not range_header:
            return FileResponse(path, media_type=media_type, headers=base_headers)
        if not m or (not m.group(1) and not m.group(2)):
            return JSONResponse(status_code=416, content={"error": "bad range"},
                                headers={"Content-Range": f"bytes */{size}"})
        start_s, end_s = m.groups()
        if not start_s:      # suffix range: last N bytes
            start = max(0, size - int(end_s))
            end = size - 1
        else:
            start = int(start_s)
            end = int(end_s) if end_s else size - 1
        end = min(end, size - 1)
        if start >= size or start > end:
            return JSONResponse(status_code=416, content={"error": "range unsatisfiable"},
                                headers={"Content-Range": f"bytes */{size}"})
        length = end - start + 1

        def iterfile():
            with open(path, "rb") as fh:
                fh.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = fh.read(min(64 * 1024, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk

        return StreamingResponse(
            iterfile(), status_code=206, media_type=media_type,
            headers={**base_headers,
                     "Content-Range": f"bytes {start}-{end}/{size}",
                     "Content-Length": str(length)})

    @api.get("/api/jobs")
    def list_jobs():
        jobs = store.list()
        for j in jobs:
            # computed server-side so the UI never guesses about the filesystem
            out = _allowed_path(j["output_file"]) if j.get("output_file") else None
            j["has_file"] = bool(out and out.exists())
        return {"jobs": jobs, "counts": store.counts(), "paused": worker.paused}

    @api.post("/api/queue/pause")
    def pause_queue():
        worker.pause()
        return {"paused": True}

    @api.post("/api/queue/resume")
    def resume_queue():
        worker.resume()
        return {"paused": False}

    @api.post("/api/jobs")
    def create_job(body: JobCreateIn):
        default_target = config["general"]["target_languages"][0]
        job = store.add(
            title=body.title,
            source=body.source,
            source_lang=body.source_lang,
            target_lang=body.target_lang or default_target,
            input_file=body.path,
            kind=body.kind,
            force=body.force,
            overrides=body.overrides,
        )
        bus.publish("job", {"type": "queued", "job_id": job.id, "title": job.title})
        return {"ok": True, "job": asdict(job)}

    @api.post("/api/jobs/clear-finished")
    def clear_finished():
        return {"ok": True, "removed": store.clear_finished()}

    @api.delete("/api/jobs/{job_id}")
    def delete_job(job_id: str):
        """Remove a job; a RUNNING job is cancelled instead of removed."""
        if worker.cancel(job_id):
            bus.publish("job", {"type": "cancelling", "job_id": job_id})
            return {"ok": True, "cancelled": True}
        if not store.remove(job_id):
            raise NotFoundError(f"no job with id {job_id}")
        return {"ok": True}

    @api.get("/api/jobs/{job_id}/file")
    def job_file(job_id: str, request: Request):
        """Stream a job's output file (HTTP Range support for video seeking)."""
        job = store.get(job_id)
        if job is None or not job.output_file:
            raise NotFoundError(f"no output file for job {job_id}")
        path = _allowed_path(job.output_file)
        if path is None:
            raise ForbiddenError("output path is outside the configured directories")
        if not path.exists():
            raise NotFoundError("output file does not exist on disk")
        return _ranged_response(path, request.headers.get("range"))

    return api
