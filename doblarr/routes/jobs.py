"""Job queue controls and guarded output streaming."""

import copy
import os
import re
from dataclasses import asdict
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .. import delivery as export_delivery
from .. import preview as scene_preview
from .. import treatments
from ..artifacts import read_json
from ..config import Config
from ..cues import (
    DISPOSITIONS,
    EVENT_DECISIONS,
    SPEECH_MODES,
    TREATED,
    TREATMENT_PRESETS,
    NonverbalEvent,
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


class AnchorIn(BaseModel):
    """One phrase edge a reviewer wants to land at a particular moment."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    phrase: str | None = Field(default=None, max_length=80)
    order: int | None = Field(default=None, ge=0, le=64)
    edge: Literal["start", "end"] = "start"
    at: float = Field(ge=0, le=3600)
    note: str = Field(default="", max_length=200)

    @field_validator("order")
    @classmethod
    def addressable(cls, value, info):
        if value is None and not (info.data.get("phrase") or "").strip():
            raise ValueError("an anchor must name a phrase or its position")
        return value


class PauseIn(BaseModel):
    """Whether one gap inside a line is performance or removable padding."""

    model_config = ConfigDict(extra="forbid")
    pause: str = Field(min_length=1, max_length=80)
    protected: bool = True
    kind: Literal["padding", "pause", "hesitation", "breath", "response"] = "pause"


class TreatmentIn(BaseModel):
    """The acoustic space or device a reviewer chose for one line.

    `bypass` is a first-class answer and is not the same as `preset="dry"`
    from a scene rule's point of view: it says *this line specifically* is to
    be left alone, and it survives a change to the scene rule above it.
    """

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    preset: str | None = Field(default=None, max_length=32)
    intensity: float | None = Field(default=None, ge=0, le=1)
    bypass: bool = False

    @field_validator("preset")
    @classmethod
    def known(cls, value):
        if value is not None and value not in TREATMENT_PRESETS:
            raise ValueError(f"preset must be one of {', '.join(TREATMENT_PRESETS)}")
        return value

    @model_validator(mode="after")
    def says_something(self):
        if self.preset is None and self.intensity is None and not self.bypass:
            raise ValueError("a treatment edit must name a preset, an intensity or a bypass")
        return self


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
    # Plan 04: phrase timing decisions. None of them generates speech — they
    # change how the take that already exists is cut and placed.
    anchors: list[AnchorIn] | None = Field(default=None, max_length=32)
    pauses: list[PauseIn] | None = Field(default=None, max_length=32)
    bypass_timing: bool | None = None
    overlap: bool | None = None
    # Plan 05: which space or device this line is played through. Like the
    # timing edits it generates no speech — it re-renders the dry line that
    # already exists.
    treatment: TreatmentIn | None = None

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


class EventEditIn(BaseModel):
    """A coverage decision for one nonverbal event.

    `asset` is a path on this machine. It is accepted here because a local
    sound file is exactly what a local replacement is, and it is deliberately
    never carried into a recipe or a version manifest.
    """

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    event: str = Field(min_length=1, max_length=64)
    decision: str = Field(min_length=1, max_length=32)
    asset: str | None = Field(default=None, max_length=1000)
    gain_db: float | None = Field(default=None, ge=-24, le=24)
    note: str = Field(default="", max_length=2000)

    @field_validator("decision")
    @classmethod
    def known(cls, value):
        if value not in EVENT_DECISIONS:
            raise ValueError(f"decision must be one of {', '.join(EVENT_DECISIONS)}")
        return value


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
    """A reviewer's verdict on one cue or one event, recorded without re-rendering.

    `event` addresses a reaction or background event. It is a separate field
    from `cue` because a reaction's cue is usually the one that was removed
    from synthesis, and filing the verdict under it would point at a line that
    is not in the review.
    """

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    cue: str | None = Field(default=None, min_length=1, max_length=64)
    event: str | None = Field(default=None, min_length=1, max_length=64)
    base_revision: str = Field(min_length=1, max_length=64)
    dispositions: list[DispositionIn] = Field(default_factory=list, max_length=50)
    note: str | None = Field(default=None, max_length=2000)
    actor: str = Field(default="", max_length=100)
    reviewed: bool | None = None

    @model_validator(mode="after")
    def one_subject(self):
        if bool(self.cue) == bool(self.event):
            raise ValueError("a decision is about exactly one cue or one event")
        return self


class ReviewEditsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    edits: list[LineEditIn] = Field(default_factory=list, max_length=2000)
    # Coverage decisions belong to the run, not to one line: an event often has
    # no surviving cue at all.
    events: list[EventEditIn] = Field(default_factory=list, max_length=500)
    use_updated_knowledge: bool = False  # default: keep the run's frozen rule snapshot
    # The review snapshot these edits were made against. Omitted by older
    # clients, whose edits are resolved against their own snapshot as before.
    base_revision: str | None = Field(default=None, max_length=64)


# Which work a set of edits actually costs, per line. A reviewer deciding
# whether to queue a re-render should not have to know which field triggers
# speech generation and which one only re-renders the audio that already
# exists.
_REGENERATES = ("text", "voice", "delivery", "mode", "traits", "direction")
_REPROCESSES = ("start", "end", "gain_db", "take", "anchors", "pauses",
                "bypass_timing", "treatment")


def _versions_root(output: Path) -> Path:
    """Where this job's saved versions live, whichever render produced `output`.

    `preserve_version` moves a completed output into `versions/<id>/`, so a job
    that has saved one already points inside that tree; a job that has not
    points at the plain output directory next to it.
    """
    if output.parent.parent.name == "versions":
        return output.parent.parent
    return output.parent / "versions"


def _rerun_plan(edits, candidate_requests: dict, events=()) -> dict:
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
        # Coverage never generates speech: it retains, replaces or omits a
        # sound and re-mixes. It is counted separately so nobody reads a
        # reaction decision as a re-render of the dialogue.
        "coverage": len(list(events)),
        "note": "Lines not listed here reuse their existing audio. Coverage "
                "decisions re-mix without generating speech.",
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

    def _redact_events(data: dict) -> list:
        """Coverage events for the browser: provenance yes, local paths no."""
        rows = []
        for raw in (data.get("nonverbal") or []):
            row = dict(raw)
            artifact = row.get("artifact")
            if isinstance(artifact, dict):
                path = _allowed_path(artifact.get("path") or "")
                artifact["available"] = bool(path and Path(artifact["path"]).is_file())
                artifact.pop("path", None)
            # A replacement sound lives somewhere on this machine. The reviewer
            # needs to know *which* file was used, not where it is.
            if row.get("asset"):
                row["asset"] = Path(row["asset"]).name
            rows.append(row)
        return rows

    def _redact_delivery(report: dict) -> dict:
        """The export report for the browser: findings yes, local paths no."""
        if not report:
            return {}
        row = copy.deepcopy(report)
        output = row.pop("output", None)
        row["output_name"] = Path(output).name if output else ""
        row["available"] = bool(output and _allowed_path(output)
                                and Path(output).is_file())
        return row

    def _media(data: dict) -> dict:
        """Which scene previews this snapshot can actually produce, and why not.

        Availability is resolved server-side against the real filesystem and
        the same path guard the download routes use, so the browser never
        offers a control that cannot work.
        """
        media = data.get("media") or {}
        found = {}
        for key in ("source_track", "source_audio", "dubbed_track", "output",
                    "vocals", "background"):
            path = _allowed_path(media[key]) if media.get(key) else None
            found[key] = bool(path and path.is_file())
        return {
            "source": found["source_track"] or found["source_audio"],
            "dub": found["dubbed_track"] or found["output"],
            "vocals": found["vocals"],
            "bed": found["background"],
            "bed_note": ((data.get("settings") or {}).get("background") or {}).get(
                "label", "" if found["background"] else
                "separation did not run, so the original mix is the bed"),
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
            # Plan 04. The timing owner and the coverage policy this run used,
            # the event ledger, and what the bed under the dub actually is.
            "timing": frozen.get("timing") or {
                k: effective["timing"].get(k) for k in
                ("mode", "max_stretch", "min_stretch", "protect_pause",
                 "anchor_tolerance", "repair")},
            "coverage": frozen.get("coverage") or {
                k: effective["coverage"].get(k) for k in
                ("mode", "gain_db", "handle_ms", "fade_ms", "max_seconds",
                 "leakage_check", "generate")},
            "background": frozen.get("background") or {},
            "nonverbal": _redact_events(data),
            "coverage_summary": (data.get("metrics") or {}).get("coverage") or {},
            "conversation": (data.get("metrics") or {}).get("conversation") or {},
            # Plan 05. Which acoustic space this run used and what it could
            # render here, plus what the exported file was measured to be.
            "treatments": frozen.get("treatments") or {
                **{k: effective["treatments"].get(k)
                   for k in ("mode", "default", "intensity")},
                "scenes": treatments.settings(dict(effective["treatments"]))["scenes"],
                "catalogue": treatments.catalogue(),
            },
            "treatment_summary": (data.get("metrics") or {}).get("treatments") or {},
            "treatment_edits": dict(data.get("treatment_edits") or {}),
            "delivery": _redact_delivery(data.get("delivery") or {}),
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
        rebuilt.vocals = resolved("vocals")
        rebuilt.background = resolved("background")
        rebuilt.nonverbal = [NonverbalEvent.from_dict(row)
                             for row in (data.get("nonverbal") or [])]
        rebuilt.timing_edits = {str(k): dict(v) for k, v
                                in (data.get("timing_edits") or {}).items()
                                if isinstance(v, dict)}
        rebuilt.treatment_edits = {str(k): dict(v) for k, v
                                   in (data.get("treatment_edits") or {}).items()
                                   if isinstance(v, dict)}
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

    def _phrasing(seg) -> dict:
        """One line's timing plan, without repeating the whole render recipe."""
        plan = seg.phrasing
        return {
            "mode": plan.mode, "state": plan.state, "reason": plan.reason,
            "planner": plan.planner, "slot": plan.slot, "onset": plan.onset,
            "planned_duration": plan.planned_duration,
            "actual_duration": plan.actual_duration,
            "max_stretch": plan.max_stretch, "min_stretch": plan.min_stretch,
            "moved": plan.moved, "protected_kept": plan.protected_kept,
            "bypassed": plan.bypassed, "attempts": plan.attempts,
            "conflicts": [dict(c) for c in plan.conflicts],
            "phrases": [{**p.as_dict(),
                         "at": next((piece["at"] for piece in plan.pieces
                                     if piece.get("phrase") == p.phrase_id), None),
                         "out": next((piece["out"] for piece in plan.pieces
                                      if piece.get("phrase") == p.phrase_id), None)}
                        for p in plan.phrases],
            "pauses": [p.as_dict() for p in plan.pauses],
            "anchors": [{**a.as_dict(), "error": a.error} for a in plan.anchors],
        }

    def _event_row(event, rebuilt) -> dict:
        row = event.as_dict()
        artifact = row.get("artifact")
        if isinstance(artifact, dict):
            artifact["available"] = bool(event.artifact and event.artifact.exists())
            artifact.pop("path", None)
        row["asset"] = Path(row["asset"]).name if row.get("asset") else ""
        row["playable"] = bool(event.rendered)
        return row

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
                "event": any(e.rendered for e in scene_preview.events_in(rebuilt, frame)),
                "line": bool(seg.audio.current() and seg.audio.current().exists()),
                "take": bool(seg.audio.selected() and seg.audio.selected().raw
                             and seg.audio.selected().raw.exists()),
                # The two halves of a treatment decision. `dry` is available
                # whenever a line is, because the finished line before any
                # effect always exists; `treated` only when one was applied.
                "dry": bool(seg.audio.upstream_of(TREATED)
                            and seg.audio.upstream_of(TREATED).exists()),
                "treated": bool(seg.audio.render(TREATED)
                                and seg.audio.render(TREATED).exists()),
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
            # Plan 04: how this line's internal timing was decided, what it
            # collides with, and which reactions sit inside the window.
            "phrasing": _phrasing(seg),
            "collisions": [
                {"code": f.code, "severity": f.severity, **dict(f.evidence)}
                for f in seg.findings
                if f.code in ("timing_collision", "timing_self_overlap",
                              "timing_overlap_intended", "timing_overlap_accepted")
                and f.disposition != "obsolete"],
            "events": [_event_row(event, rebuilt)
                       for event in scene_preview.events_in(rebuilt, frame)],
            "timing_edit": rebuilt.timing_edits.get(seg.cue_id) or {},
            # Plan 05: the space this line was played through, what it could
            # not do here, and the findings a reviewer should listen for.
            "treatment": seg.treatment.as_dict(),
            "treatment_edit": rebuilt.treatment_edits.get(seg.cue_id) or {},
            "treatment_findings": [
                {"code": f.code, "severity": f.severity, **dict(f.evidence)}
                for f in seg.findings
                if f.code.startswith("treatment_") and f.disposition != "obsolete"],
        }

    @api.get("/api/jobs/{job_id}/preview/{kind}/{index}")
    def job_preview(job_id: str, kind: str, index: int, request: Request,
                    context: int = 2, take: str = "", event: str = "",
                    start: float | None = None, end: float | None = None):
        """Stream one bounded preview: the original scene, the dub, a line or a take."""
        job, data = artifact(job_id, "review_file")
        check_schema(data.get("cue_schema"), "review snapshot")
        rebuilt, segments = _scene_job(job, data)
        bounds = (start, end) if start is not None and end is not None else None
        try:
            resolved = scene_preview.resolve(rebuilt, segments, kind, index,
                                             context=context, bounds=bounds, take=take,
                                             event=event)
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

    @api.get("/api/jobs/{job_id}/delivery")
    def job_delivery(job_id: str):
        """What the exported track was measured to be, and what that means.

        Three states are kept apart on purpose: the render finished, the
        export passed its configured structural checks, and a person has
        listened. Only the middle one is decided here.
        """
        job, data = artifact(job_id, "review_file")
        check_schema(data.get("cue_schema"), "review snapshot")
        report = _redact_delivery(data.get("delivery") or {})
        return {
            "job_id": job_id,
            "revision": data.get("revision"),
            "render": {"status": job.status, "message": job.message},
            "delivery": report,
            "version_id": job.version_id,
            "version_saved": bool(job.version_id),
            "human_review": _review_state(config.work_dir, job_id, data),
            "note": ("A passing export check says the delivered container is "
                     "structurally what was asked for. It says nothing about "
                     "whether the dub sounds right; that needs a listener."),
        }

    def _review_state(work_dir, job_id: str, data: dict) -> dict:
        """How much of this snapshot a person has actually signed off.

        Deliberately counted from the decisions sidecar and not from playback:
        reaching the end of a file is not a verdict, and a render completing is
        not an approval.
        """
        decisions = load_decisions(work_dir, job_id)
        cues = decisions.get("cues", {})
        current = data.get("revision")
        reviewed = [cue for cue, entry in cues.items()
                    if entry.get("reviewed") and entry.get("revision") == current]
        stale = [cue for cue, entry in cues.items()
                 if entry.get("reviewed") and entry.get("revision") != current]
        total = len(data.get("segments") or [])
        return {
            "reviewed": len(reviewed), "lines": total,
            "stale": len(stale), "complete": bool(total) and len(reviewed) >= total,
            "note": ("A verdict recorded against an earlier snapshot is counted "
                     "as stale, not as approval of this audio."),
        }

    @api.get("/api/jobs/{job_id}/versions/{version_id}/compare/{other_id}")
    def job_version_compare(job_id: str, version_id: str, other_id: str):
        """What changed between two saved versions of this job's output.

        Separates a line whose *speech* was regenerated from one that was only
        reprocessed, and names the windows a reviewer should expect to differ —
        ducking and effect tails reach past the cue that was edited, so a
        neighbouring region moving is not evidence of a second change.
        """
        job = store.get(job_id)
        if job is None or not job.output_file:
            raise NotFoundError(f"no job with id {job_id}")
        for candidate in (version_id, other_id):
            if not re.fullmatch(r"[0-9a-f]{8,128}", candidate):
                raise ForbiddenError("that is not a version id")
        output = _allowed_path(job.output_file)
        if output is None:
            raise ForbiddenError("output path is outside the configured directories")
        root = _versions_root(output)
        previous = read_json(root / version_id / "version.json")
        current = read_json(root / other_id / "version.json")
        for manifest, wanted in ((previous, version_id), (current, other_id)):
            if manifest.get("version_id") != wanted:
                raise NotFoundError(f"no saved version {wanted} for this job")
        result = export_delivery.compare(previous, current)
        # Delivery evidence travels with the version it was taken on; a check
        # that ran against the older file closes nothing about the newer one.
        result["delivery"] = {
            "previous": _redact_delivery(previous.get("delivery") or {}),
            "current": _redact_delivery(current.get("delivery") or {}),
            "note": ("Each report describes the file it was run on. Retesting a "
                     "fix closes a finding only for the output it was checked "
                     "against."),
        }
        return result

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
        scope = "cue" if body.cue else "event"
        subject = body.cue or body.event or ""
        known = ({(row.get("cue") or {}).get("cue_id") for row in data["segments"]}
                 if scope == "cue"
                 else {row.get("event_id") for row in (data.get("nonverbal") or [])})
        if subject not in known:
            raise HTTPException(
                404, "That line is not part of this review" if scope == "cue"
                else "That event is not part of this review")
        patch: dict[str, Any] = {
            "dispositions": {d.finding: {"disposition": d.disposition, "note": d.note}
                             for d in body.dispositions}}
        if body.note is not None:
            patch["note"] = body.note
        if body.reviewed is not None:
            patch["reviewed"] = body.reviewed
        try:
            entry = record_decision(config.work_dir, job_id, data["revision"], subject,
                                    patch, actor=body.actor, scope=scope)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"ok": True, "scope": scope, "decision": entry}

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
        if not body.edits and not body.events:
            raise HTTPException(422, "Nothing to change: no line edits and no coverage "
                                     "decisions were sent")
        rows = {s["index"]: s for s in data["segments"]}
        effective = config.with_overrides(original.overrides or {})
        edits = copy.deepcopy(effective["dub"].get("line_edits", {}))
        candidate_requests = copy.deepcopy(effective["dub"].get("candidates", {}))
        coverage_events = copy.deepcopy(effective["coverage"].get("events", {}))
        coverage_assets = copy.deepcopy(effective["coverage"].get("assets", {}))
        treatment_lines = copy.deepcopy(effective["treatments"].get("lines", {}))
        known_events = {row.get("event_id") for row in (data.get("nonverbal") or [])}
        for choice in body.events:
            if choice.event not in known_events:
                raise HTTPException(404, "That event is not part of this review")
            entry: dict[str, Any] = {"decision": choice.decision}
            if choice.gain_db is not None:
                entry["gain_db"] = choice.gain_db
            if choice.note:
                entry["note"] = choice.note
            if choice.asset:
                # A local sound file. Checked against the same guard the
                # download routes use: a coverage decision must not become a
                # way to read an arbitrary path off this machine.
                asset = _allowed_path(choice.asset)
                if asset is None or not asset.is_file():
                    raise HTTPException(
                        422, "A replacement sound must be a file inside the configured "
                             "work or output directories")
                coverage_assets[choice.event] = str(asset)
            elif choice.decision != "replace":
                coverage_assets.pop(choice.event, None)
            coverage_events[choice.event] = entry
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
            # `treatment` is excluded here and collected separately: it is a
            # per-cue override on the run, not a field of the line edit, for
            # the same reason `candidates` is.
            changes = patch.model_dump(
                exclude_none=True,
                exclude={"index", "regenerate", "cue", "candidates", "treatment"})
            if "pauses" in changes:
                # The wire form is a list so the order is stable; the edit
                # record is keyed by pause id so a second decision about the
                # same gap replaces the first instead of stacking.
                changes["pauses"] = {row["pause"]: {"protected": row["protected"],
                                                    "kind": row["kind"]}
                                     for row in changes["pauses"]}
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
            if patch.treatment is not None:
                # Keyed by cue, never by position: a treatment somebody chose
                # after hearing a line has to follow that line through a later
                # edit rather than land on whatever ends up at that index.
                if not cue_id:
                    raise HTTPException(
                        409, "This line has no stable identity yet; re-render it first")
                chosen = patch.treatment.model_dump(exclude_none=True)
                # `bypass: false` is the absence of a bypass, not a decision to
                # have one. Storing it would make an override that says nothing.
                if not chosen.get("bypass"):
                    chosen.pop("bypass", None)
                treatment_lines[cue_id] = chosen
            if "text" in changes and not changes["text"].strip():
                raise HTTPException(422, "Dialogue cannot be blank; use Exclude instead")
            if changes.get("end", row["end"]) <= changes.get("start", row["start"]):
                raise HTTPException(422, "End time must be after start time")
            window = (changes.get("end", row["end"])
                      - changes.get("start", row["start"]))
            for anchor in (patch.anchors or []):
                if anchor.at > window + 1e-6:
                    raise HTTPException(
                        422, f"An anchor at {anchor.at:g}s sits past the end of this "
                             f"line's {window:.2f}s window")
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
        if coverage_events:
            overrides["coverage.events"] = coverage_events
        if coverage_assets:
            overrides["coverage.assets"] = coverage_assets
        if treatment_lines:
            overrides["treatments.lines"] = treatment_lines
        rerun = _rerun_plan(body.edits, candidate_requests, body.events)
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
