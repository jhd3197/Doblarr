"""Job queue controls and guarded output streaming."""

import copy
import os
import re
from dataclasses import asdict
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .. import preview as scene_preview
from ..artifacts import read_json
from ..config import Config
from ..cues import (
    DISPOSITIONS,
    SPEECH_MODES,
    adopt_legacy,
    apply_cue_payload,
    check_schema,
)
from ..errors import ForbiddenError, NotFoundError
from ..events import EventBus
from ..jobs import JobStore, Worker
from ..knowledge import snapshot as knowledge_snapshot
from ..languages import base_language, normalize
from ..models import DubJob, Segment, Speaker
from ..review import load_decisions, merge_decisions, record_decision
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
    cue: str | None = Field(default=None, max_length=64)  # stable cue id, when known
    text: str | None = Field(default=None, min_length=1, max_length=10000)
    start: float | None = Field(default=None, ge=0)
    end: float | None = Field(default=None, gt=0)
    voice: str | None = Field(default=None, max_length=200)
    delivery: str | None = Field(default=None, max_length=500)
    exclude: bool | None = None
    regenerate: bool = False
    # Plan 03: structured acting direction, a chosen take, a per-line gain and
    # a request for alternatives. All optional; omitting one leaves whatever
    # the run already decided untouched.
    mode: str | None = Field(default=None, max_length=32)
    traits: list[str] | None = Field(default=None, max_length=8)
    direction: str | None = Field(default=None, max_length=500)
    take: str | None = Field(default=None, max_length=64)
    gain_db: float | None = Field(default=None, ge=-24, le=24)
    candidates: int | None = Field(default=None, ge=0, le=4)

    @field_validator("mode")
    @classmethod
    def known_mode(cls, value):
        if value is not None and value not in SPEECH_MODES:
            raise ValueError(f"speech mode must be one of {', '.join(SPEECH_MODES)}")
        return value

    @field_validator("traits")
    @classmethod
    def clean_traits(cls, value):
        if value is None:
            return value
        cleaned = [str(t).strip().lower() for t in value if str(t).strip()]
        if any(len(t) > 40 for t in cleaned):
            raise ValueError("a delivery trait must be short")
        return cleaned


class DispositionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    finding: str = Field(min_length=1, max_length=64)
    disposition: str = Field(min_length=1, max_length=32)
    note: str = Field(default="", max_length=2000)

    @field_validator("disposition")
    @classmethod
    def known(cls, value):
        if value not in DISPOSITIONS:
            raise ValueError(f"disposition must be one of {', '.join(DISPOSITIONS)}")
        return value


class DecisionIn(BaseModel):
    """A reviewer's verdict on one cue, recorded without re-rendering anything."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    cue: str = Field(min_length=1, max_length=64)
    base_revision: str = Field(min_length=1, max_length=64)
    dispositions: list[DispositionIn] = Field(default_factory=list, max_length=50)
    note: str | None = Field(default=None, max_length=2000)
    actor: str = Field(default="", max_length=100)
    reviewed: bool | None = None


class ReviewEditsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    edits: list[LineEditIn] = Field(min_length=1, max_length=2000)
    use_updated_knowledge: bool = False  # default: keep the run's frozen rule snapshot
    # The review snapshot these edits were made against. Omitted by older
    # clients, whose edits are resolved against their own snapshot as before.
    base_revision: str | None = Field(default=None, max_length=64)


# Which work a set of edits actually costs, per line. A reviewer deciding
# whether to queue a re-render should not have to know which field triggers
# speech generation and which one only re-renders the audio that already
# exists.
_REGENERATES = ("text", "voice", "delivery", "mode", "traits", "direction")
_REPROCESSES = ("start", "end", "gain_db", "take")


def _versions_root(output: Path) -> Path:
    """Where this job's saved versions live, whichever render produced `output`.

    `preserve_version` moves a completed output into `versions/<id>/`, so a job
    that has saved one already points inside that tree; a job that has not
    points at the plain output directory next to it.
    """
    if output.parent.parent.name == "versions":
        return output.parent.parent
    return output.parent / "versions"


def _rerun_plan(edits, candidate_requests: dict) -> dict:
    """Say plainly what each edited line will cost before it is queued."""
    lines = []
    for patch in edits:
        fields = patch.model_dump(exclude_none=True,
                                  exclude={"index", "cue", "regenerate", "candidates"})
        regenerates = bool(patch.regenerate) or any(k in fields for k in _REGENERATES)
        reprocesses = regenerates or any(k in fields for k in _REPROCESSES)
        lines.append({
            "index": patch.index,
            "cue": patch.cue,
            "work": ("generates new speech" if regenerates
                     else "re-renders existing audio" if reprocesses
                     else "records a decision only"),
            "rechecks": regenerates or reprocesses,
        })
    return {
        "lines": lines,
        "generating": sum(1 for line in lines if line["work"] == "generates new speech"),
        "processing": sum(1 for line in lines
                          if line["work"] == "re-renders existing audio"),
        "candidates": sum(int(n) for n in candidate_requests.values()),
        "note": "Lines not listed here reuse their existing audio.",
    }


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

    def _redact_cue(row):
        """Keep provenance, drop machine-local paths, exactly as for audio_clip."""
        cue = row.get("cue")
        if not isinstance(cue, dict):
            # A pre-schema review snapshot. It is shown as what it is — a legacy
            # row with one clip of unproven role — and is never given a
            # fabricated cue id, so an edit from it still resolves by index
            # against its own snapshot.
            row["cue"] = {
                "cue_id": "",
                "lineage": {"origin": "legacy", "legacy_index": row.get("index")},
                "source": {"spans": []},
                "placement": {},
                "audio": {"takes": [], "selection": None,
                          "renders": [{"role": "unknown", "proven": False,
                                       "available": bool(row.get("has_audio"))}]},
                "preparation": {"decision": "unknown",
                                "reason": "snapshot predates boundary analysis"},
                "findings": [],
            }
            return
        for take in cue.get("audio", {}).get("takes", []):
            raw = take.get("raw")
            if isinstance(raw, dict):
                raw["available"] = bool(_allowed_path(raw.get("path") or "")
                                        and Path(raw["path"]).is_file())
                raw.pop("path", None)
        for render in cue.get("audio", {}).get("renders", []):
            render["available"] = bool(_allowed_path(render.get("path") or "")
                                       and Path(render["path"]).is_file())
            render.pop("path", None)

    def _media(data: dict) -> dict:
        """Which scene previews this snapshot can actually produce, and why not.

        Availability is resolved server-side against the real filesystem and
        the same path guard the download routes use, so the browser never
        offers a control that cannot work.
        """
        media = data.get("media") or {}
        found = {}
        for key in ("source_track", "source_audio", "dubbed_track", "output"):
            path = _allowed_path(media[key]) if media.get(key) else None
            found[key] = bool(path and path.is_file())
        return {
            "source": found["source_track"] or found["source_audio"],
            "dub": found["dubbed_track"] or found["output"],
            "note": ("" if found["source_track"] or found["source_audio"]
                     else "the extracted original audio is no longer on disk"),
        }

    @api.get("/api/jobs/{job_id}/review")
    def job_review(job_id: str):
        job, data = artifact(job_id, "review_file")
        check_schema(data.get("cue_schema"), "review snapshot")
        for row in data["segments"]:
            clip = _allowed_path(row["audio_clip"]) if row.get("audio_clip") else None
            row["has_audio"] = bool(clip and clip.is_file())
            row.pop("audio_clip", None)
            row.pop("words", None)
            _redact_cue(row)
        effective = config.with_overrides(job.overrides or {})
        # The run's own settings win; live config is only a fallback for a
        # snapshot written before they were frozen.
        frozen = data.get("settings") or {}
        previews = _media(data)
        # Machine-local paths never leave the server; the browser gets the
        # availability flags computed from them instead.
        data.pop("media", None)
        reference_clips = data.pop("references", None) or {}
        payload = {
            **data,
            "title": job.title,
            "editable": job.status not in {"running", "queued"},
            "title_ref": cast_key(path=str(job.input_file)) if job.input_file else "",
            "show_ref": effective["dub"].get("cast_group", ""),
            "previews": previews,
            "levels": frozen.get("levels") or {
                k: effective["levels"].get(k) for k in
                ("mode", "target_db", "strength", "max_boost_db", "max_cut_db")},
            "verification_policy": frozen.get(
                "verification_policy", effective["quality"].get("asr", "off")),
            "candidate_limit": int(frozen.get(
                "candidate_limit", effective["dub"].get("candidate_limit", 4))),
            "references": sorted(reference_clips),
            "frozen_settings": bool(frozen),
        }
        return merge_decisions(payload, load_decisions(config.work_dir, job_id))

    def _scene_job(job, data: dict):
        """Rebuild just enough of a run from its snapshot to cut previews from.

        The snapshot is the authority here: it names the media this run
        produced and carries each cue's records, so a preview is always of the
        run being reviewed rather than of whatever the live config would make
        now.
        """
        media = data.get("media") or {}

        def resolved(*keys):
            for key in keys:
                value = media.get(key)
                if not value:
                    continue
                path = _allowed_path(value)
                if path and path.is_file():
                    return path
            return None

        rebuilt = DubJob(
            input_file=Path(job.input_file or data.get("title") or "unknown"),
            source_lang=data.get("source_language") or "und",
            target_lang=data.get("language") or "und",
            target_locale=data.get("locale") or "",
        )
        rebuilt.source_track = resolved("source_track", "source_audio")
        rebuilt.source_audio = rebuilt.source_track
        rebuilt.dubbed_track = resolved("dubbed_track", "output")
        work = _allowed_path(media["work"]) if media.get("work") else None
        rebuilt.artifacts_dir = work or config.work_dir / "previews"
        segments = []
        for row in data["segments"]:
            seg = Segment(index=int(row["index"]), start=float(row["start"]),
                          end=float(row["end"]), text_src=row.get("text_src", ""),
                          speaker=row.get("speaker", "SPEAKER_00"),
                          text_translated=row.get("text_translated"))
            record = row.get("cue")
            clip = _allowed_path(row["audio_clip"]) if row.get("audio_clip") else None
            if clip and clip.is_file():
                seg.audio_clip = clip
            if record:
                apply_cue_payload(seg, record)
            else:
                # A snapshot from before the cue schema. Its one clip is
                # registered with an unproven role, which is what it is.
                adopt_legacy(seg, data.get("revision", ""))
            current = seg.audio.current()
            if current and current.path:
                seg.audio_clip = Path(current.path)
            segments.append(seg)
        rebuilt.segments = segments
        # Speakers exist here only to carry their clone reference, which is
        # what the "reference" preview plays.
        for label, clip in (data.get("references") or {}).items():
            resolved_clip = _allowed_path(str(clip))
            rebuilt.speakers[label] = Speaker(
                label=label,
                reference_clip=resolved_clip if resolved_clip and resolved_clip.is_file()
                else None)
        return rebuilt, segments

    @api.get("/api/jobs/{job_id}/scene/{index}")
    def job_scene(job_id: str, index: int, context: int = 2):
        """The exchange around one line: which cues, which spans, what can play."""
        job, data = artifact(job_id, "review_file")
        check_schema(data.get("cue_schema"), "review snapshot")
        rebuilt, segments = _scene_job(job, data)
        try:
            frame = scene_preview.window(segments, index, context)
        except scene_preview.PreviewError as exc:
            raise HTTPException(404, str(exc)) from exc
        seg = next(s for s in segments if s.index == index)
        return {
            **frame,
            "revision": data.get("revision"),
            "available": {
                **_media(data),
                "reference": bool((data.get("references") or {}).get(seg.speaker)),
                "line": bool(seg.audio.current() and seg.audio.current().exists()),
                "take": bool(seg.audio.selected() and seg.audio.selected().raw
                             and seg.audio.selected().raw.exists()),
            },
            # Ordered by what the technical checks can actually establish —
            # a take with a defect goes last, with the defect named. This is
            # not a ranking of the acting, and the UI says so.
            "takes": sorted(
                ({"take_id": t.take_id, "origin": t.origin, "state": t.state,
                  "attempt": t.attempt, "direction": t.direction,
                  "checks": t.checks, "error": t.error,
                  "selected": bool(seg.audio.selection
                                   and seg.audio.selection.take_id == t.take_id),
                  "available": bool(t.raw and t.raw.exists())}
                 for t in seg.audio.takes),
                key=lambda t: (not t["selected"],
                               t["state"] == "failed",
                               (t["checks"] or {}).get("state") == "defective",
                               t["attempt"])),
            "selection": (seg.audio.selection.as_dict() if seg.audio.selection else None),
        }

    @api.get("/api/jobs/{job_id}/preview/{kind}/{index}")
    def job_preview(job_id: str, kind: str, index: int, request: Request,
                    context: int = 2, take: str = "", start: float | None = None,
                    end: float | None = None):
        """Stream one bounded preview: the original scene, the dub, a line or a take."""
        job, data = artifact(job_id, "review_file")
        check_schema(data.get("cue_schema"), "review snapshot")
        rebuilt, segments = _scene_job(job, data)
        bounds = (start, end) if start is not None and end is not None else None
        try:
            resolved = scene_preview.resolve(rebuilt, segments, kind, index,
                                             context=context, bounds=bounds, take=take)
        except scene_preview.PreviewError as exc:
            raise HTTPException(409, str(exc)) from exc
        path = _allowed_path(str(resolved["path"]))
        if path is None:
            raise ForbiddenError("preview media is outside the configured directories")
        if not path.is_file():
            raise NotFoundError("the media for this preview is no longer on disk")
        return _ranged_response(path, request.headers.get("range"))

    @api.get("/api/jobs/{job_id}/versions")
    def job_versions(job_id: str):
        """Saved dub versions for this job's output, newest first.

        A version is an immutable render someone already produced. Listing
        them is what makes "compare with the previous version" possible
        without re-rendering anything.
        """
        job = store.get(job_id)
        if job is None:
            raise NotFoundError(f"no job with id {job_id}")
        output = _allowed_path(job.output_file) if job.output_file else None
        if output is None:
            return {"versions": [], "current": job.version_id}
        root = _versions_root(output)
        rows = []
        for manifest_path in sorted(root.glob("*/version.json")) if root.is_dir() else []:
            manifest = read_json(manifest_path)
            if not manifest.get("version_id"):
                continue
            media = _allowed_path(manifest.get("output") or "")
            rows.append({
                "version_id": manifest["version_id"],
                "name": manifest.get("name") or "Dub",
                "created_at": manifest.get("created_at"),
                "translation_id": manifest.get("translation_id"),
                "kind": manifest.get("kind"),
                "current": manifest["version_id"] == job.version_id,
                "available": bool(media and media.is_file()),
            })
        rows.sort(key=lambda row: row.get("created_at") or "", reverse=True)
        return {"versions": rows, "current": job.version_id}

    @api.get("/api/jobs/{job_id}/versions/{version_id}/file")
    def job_version_file(job_id: str, version_id: str, request: Request):
        """Stream one saved version's media, so two renders can be compared."""
        job = store.get(job_id)
        if job is None or not job.output_file:
            raise NotFoundError(f"no job with id {job_id}")
        if not re.fullmatch(r"[0-9a-f]{8,128}", version_id):
            raise ForbiddenError("that is not a version id")
        output = _allowed_path(job.output_file)
        if output is None:
            raise ForbiddenError("output path is outside the configured directories")
        manifest = read_json(_versions_root(output) / version_id / "version.json")
        media = _allowed_path(manifest.get("output") or "")
        if media is None or not media.is_file():
            raise NotFoundError("that version's media is not on disk")
        return _ranged_response(media, request.headers.get("range"))

    @api.post("/api/jobs/{job_id}/decisions")
    def job_decision(job_id: str, body: DecisionIn):
        """Record a reviewer's verdict on a cue without re-rendering anything.

        A generated export never marks a scene reviewed; this is the only way
        a disposition is set, and it always carries the snapshot revision the
        reviewer was looking at.
        """
        job, data = artifact(job_id, "review_file")
        check_schema(data.get("cue_schema"), "review snapshot")
        if body.base_revision != data.get("revision"):
            raise HTTPException(
                409,
                "This review has changed since you opened it. Reload it so your "
                "notes apply to the current lines.")
        known = {(row.get("cue") or {}).get("cue_id") for row in data["segments"]}
        if body.cue not in known:
            raise HTTPException(404, "That line is not part of this review")
        patch: dict[str, Any] = {
            "dispositions": {d.finding: {"disposition": d.disposition, "note": d.note}
                             for d in body.dispositions}}
        if body.note is not None:
            patch["note"] = body.note
        if body.reviewed is not None:
            patch["reviewed"] = body.reviewed
        try:
            entry = record_decision(config.work_dir, job_id, data["revision"], body.cue,
                                    patch, actor=body.actor)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"ok": True, "decision": entry}

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
        check_schema(data.get("cue_schema"), "review snapshot")
        if original.status in {"queued", "running"}:
            raise HTTPException(409, "Wait for the job to stop before editing")
        if body.base_revision and body.base_revision != data.get("revision"):
            raise HTTPException(
                409,
                "This review has changed since you opened it. Reload it so your "
                "edits apply to the current lines.",
            )
        rows = {s["index"]: s for s in data["segments"]}
        effective = config.with_overrides(original.overrides or {})
        edits = copy.deepcopy(effective["dub"].get("line_edits", {}))
        candidate_requests = copy.deepcopy(effective["dub"].get("candidates", {}))
        decisions = load_decisions(config.work_dir, job_id)
        seen = set()
        for patch in body.edits:
            if patch.index not in rows or patch.index in seen:
                raise HTTPException(422, "Unknown or duplicate line index")
            seen.add(patch.index)
            row = rows[patch.index]
            cue_id = (row.get("cue") or {}).get("cue_id") or ""
            if patch.cue and cue_id and patch.cue != cue_id:
                raise HTTPException(409, "This line has changed identity; reload the review")
            changes = patch.model_dump(
                exclude_none=True, exclude={"index", "regenerate", "cue", "candidates"})
            if patch.take:
                takes = {t.get("take_id") for t
                         in (row.get("cue") or {}).get("audio", {}).get("takes", [])}
                if patch.take not in takes:
                    raise HTTPException(409, "That take is not on this line any more")
            if patch.candidates:
                if not cue_id:
                    raise HTTPException(
                        409, "This line has no stable identity yet; re-render it first")
                candidate_requests[cue_id] = patch.candidates
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
            if cue_id:
                # Pin the edit to the cue, not the position: a later split or
                # merge must raise a conflict instead of moving the edit.
                edit["cue"] = cue_id
            edit.update(changes)
            if patch.regenerate:
                edit["revision"] = max(edit.get("revision", 0), row.get("revision", 0)) + 1
        # Verdicts recorded against *this* snapshot travel with the re-render,
        # so a decision survives the run it was made about. A verdict recorded
        # against an older snapshot is left behind rather than carried onto
        # audio it was never about.
        for cue_id, entry in decisions.get("cues", {}).items():
            if entry.get("revision") != data.get("revision"):
                continue
            verdicts = {f: {"disposition": v.get("disposition"),
                            "note": v.get("note", ""), "actor": v.get("actor", "")}
                        for f, v in (entry.get("dispositions") or {}).items()}
            if not verdicts:
                continue
            target = next((key for key, edit in edits.items()
                           if edit.get("cue") == cue_id), None)
            if target is None:
                row = next((r for r in data["segments"]
                            if (r.get("cue") or {}).get("cue_id") == cue_id), None)
                if row is None:
                    continue
                target = str(row["index"])
                edits.setdefault(target, {}).update({
                    "cue": cue_id,
                    "text": row.get("text_translated") or row["text_src"],
                    "start": row["start"], "end": row["end"],
                    "delivery": row.get("delivery", ""),
                    "revision": row.get("revision", 0)})
            edits[target]["dispositions"] = verdicts
        overrides = copy.deepcopy(original.overrides or {})
        overrides["dub.line_edits"] = edits
        overrides["dub.candidates"] = candidate_requests
        overrides["dub.dry_run"] = False
        rerun = _rerun_plan(body.edits, candidate_requests)
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
        return {"ok": True, "job": asdict(updated), "rerun": rerun,
                "previous_version": original.version_id}

    return api
