"""Job queue controls and guarded output streaming."""

import copy
import os
import re
from dataclasses import asdict
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from ..artifacts import read_json
from ..config import Config
from ..errors import ForbiddenError, NotFoundError
from ..events import EventBus
from ..jobs import JobStore, Worker
from ..knowledge import snapshot as knowledge_snapshot
from ..languages import base_language, normalize
from ..voices import cast_key


class JobCreateIn(BaseModel):
    title: str = Field(min_length=1)
    source: str = "manual"
    source_lang: str = "auto"
    target_lang: str | None = None
    path: str | None = None
    kind: Literal["full", "tease", "audition"] = "full"
    force: bool = False  # re-run every stage, ignoring cached artifacts
    overrides: dict[str, Any] | None = None  # per-title config overrides


class LineEditIn(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    index: int = Field(ge=0)
    text: str | None = Field(default=None, min_length=1, max_length=10000)
    start: float | None = Field(default=None, ge=0)
    end: float | None = Field(default=None, gt=0)
    voice: str | None = Field(default=None, max_length=200)
    delivery: str | None = Field(default=None, max_length=500)
    exclude: bool | None = None
    regenerate: bool = False


class ReviewEditsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    edits: list[LineEditIn] = Field(min_length=1, max_length=2000)
    use_updated_knowledge: bool = False  # default: keep the run's frozen rule snapshot


def build_router(config: Config, store: JobStore, worker: Worker, bus: EventBus) -> APIRouter:
    api = APIRouter()

    def _allowed_path(path_str: str) -> Path | None:
        # Read live: saving an output directory must also update the download guard.
        try:
            allowed_roots = [
                Path(os.path.normcase(str(root.resolve())))
                for root in (config.output_dir, config.work_dir)
            ]
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
        media_type = {
            ".mkv": "video/x-matroska",
            ".mp4": "video/mp4",
            ".m4v": "video/mp4",
            ".wav": "audio/wav",
        }.get(path.suffix.lower(), "application/octet-stream")
        size = path.stat().st_size
        base_headers = {"Accept-Ranges": "bytes"}
        m = re.fullmatch(r"bytes=(\d*)-(\d*)", (range_header or "").strip())
        if not range_header:
            return FileResponse(path, media_type=media_type, headers=base_headers)
        if not m or (not m.group(1) and not m.group(2)):
            return JSONResponse(
                status_code=416,
                content={"error": "bad range"},
                headers={"Content-Range": f"bytes */{size}"},
            )
        start_s, end_s = m.groups()
        if not start_s:  # suffix range: last N bytes
            start = max(0, size - int(end_s))
            end = size - 1
        else:
            start = int(start_s)
            end = int(end_s) if end_s else size - 1
        end = min(end, size - 1)
        if start >= size or start > end:
            return JSONResponse(
                status_code=416,
                content={"error": "range unsatisfiable"},
                headers={"Content-Range": f"bytes */{size}"},
            )
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
            iterfile(),
            status_code=206,
            media_type=media_type,
            headers={
                **base_headers,
                "Content-Range": f"bytes {start}-{end}/{size}",
                "Content-Length": str(length),
            },
        )

    @api.get("/api/jobs")
    def list_jobs():
        jobs = store.list()
        for j in jobs:
            # computed server-side so the UI never guesses about the filesystem
            out = _allowed_path(j["output_file"]) if j.get("output_file") else None
            j["has_file"] = bool(out and out.exists())
            review = _allowed_path(j["review_file"]) if j.get("review_file") else None
            j["has_review"] = bool(review and review.is_file())
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
        if body.path and Path(body.path).is_dir():
            raise HTTPException(
                422,
                "Choose episode files from the show's Episodes tab; a folder is not a dub input",
            )
        default_target = config["general"]["target_languages"][0]
        target = normalize(body.target_lang or default_target) or body.target_lang or default_target
        base = base_language(target)
        job = store.add(
            title=body.title,
            source=body.source,
            source_lang=body.source_lang,
            target_lang=base,
            target_locale=target if target != base else "",
            input_file=body.path,
            kind=body.kind,
            force=body.force,
            overrides=body.overrides,
            knowledge_snapshot=knowledge_snapshot(store.db),
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

    def artifact(job_id, field):
        job = store.get(job_id)
        name = getattr(job, field, None)
        if not job or not name:
            raise NotFoundError("No report is available for this job")
        path = _allowed_path(name)
        if path is None:
            raise ForbiddenError("report path is outside the configured directories")
        data = read_json(path)
        if not data:
            raise NotFoundError("Report is missing or incomplete")
        return job, data

    @api.get("/api/jobs/{job_id}/report")
    def job_report(job_id: str):
        return artifact(job_id, "report_file")[1]

    @api.get("/api/jobs/{job_id}/review")
    def job_review(job_id: str):
        job, data = artifact(job_id, "review_file")
        for row in data["segments"]:
            clip = _allowed_path(row["audio_clip"]) if row.get("audio_clip") else None
            row["has_audio"] = bool(clip and clip.is_file())
            row.pop("audio_clip", None)
            row.pop("words", None)
        effective = config.with_overrides(job.overrides or {})
        return {
            **data,
            "title": job.title,
            "editable": job.status not in {"running", "queued"},
            "title_ref": cast_key(path=str(job.input_file)) if job.input_file else "",
            "show_ref": effective["dub"].get("cast_group", ""),
        }

    @api.get("/api/jobs/{job_id}/clips/{index}")
    def line_audio(job_id: str, index: int, request: Request):
        _, data = artifact(job_id, "review_file")
        row = next((s for s in data["segments"] if s["index"] == index), None)
        if not row or not row.get("audio_clip"):
            raise NotFoundError("No generated audio for this line")
        path = _allowed_path(row["audio_clip"])
        if path is None:
            raise ForbiddenError("clip is outside the configured directories")
        if not path.is_file():
            raise NotFoundError("Generated clip no longer exists")
        return _ranged_response(path, request.headers.get("range"))

    @api.post("/api/jobs/{job_id}/review")
    def queue_review(job_id: str, body: ReviewEditsIn):
        original, data = artifact(job_id, "review_file")
        if original.status in {"queued", "running"}:
            raise HTTPException(409, "Wait for the job to stop before editing")
        rows = {s["index"]: s for s in data["segments"]}
        effective = config.with_overrides(original.overrides or {})
        edits = copy.deepcopy(effective["dub"].get("line_edits", {}))
        seen = set()
        for patch in body.edits:
            if patch.index not in rows or patch.index in seen:
                raise HTTPException(422, "Unknown or duplicate line index")
            seen.add(patch.index)
            row = rows[patch.index]
            changes = patch.model_dump(exclude_none=True, exclude={"index", "regenerate"})
            if "text" in changes and not changes["text"].strip():
                raise HTTPException(422, "Dialogue cannot be blank; use Exclude instead")
            if changes.get("end", row["end"]) <= changes.get("start", row["start"]):
                raise HTTPException(422, "End time must be after start time")
            edit = edits.setdefault(str(patch.index), {})
            # Preserve the reviewed text and timing instead of reviving an earlier script.
            for key, value in {
                "text": row.get("text_translated") or row["text_src"],
                "start": row["start"],
                "end": row["end"],
                "delivery": row.get("delivery", ""),
                "revision": row.get("revision", 0),
            }.items():
                edit.setdefault(key, value)
            edit.update(changes)
            if patch.regenerate:
                edit["revision"] = max(edit.get("revision", 0), row.get("revision", 0)) + 1
        overrides = copy.deepcopy(original.overrides or {})
        overrides["dub.line_edits"] = edits
        overrides["dub.dry_run"] = False
        updated = store.add(
            title=original.title,
            source=original.source,
            source_lang=original.source_lang,
            target_lang=original.target_lang,
            target_locale=original.target_locale,
            input_file=original.input_file,
            # Frozen rules are inherited unless the author explicitly asks for
            # the updated knowledge (a saved correction only applies on request).
            knowledge_snapshot=(
                knowledge_snapshot(store.db)
                if body.use_updated_knowledge
                else original.knowledge_snapshot
            ),
            kind=original.kind,
            overrides=overrides,
        )
        bus.publish("job", {"type": "queued", "job_id": updated.id, "title": updated.title})
        return {"ok": True, "job": asdict(updated)}

    return api
