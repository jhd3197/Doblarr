"""Envelope recommendations, selections, A/B audio and feedback for one run.

Reading never generates anything. Selecting a template, changing its strength
or clearing it queues a rerender that reuses every take (processing and mix
only; the response says so before anything runs). A/B compares the same take:
the fitted take before the level owner, the shaped render, the original line
and the mix. A level-matched comparison is offered only labelled as such, so it
cannot hide the volume change being judged.
"""

from __future__ import annotations

import copy
import math
import tempfile
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from .. import envelopes, feedback, templates
from ..artifacts import read_json
from ..cues import LEVELED, apply_cue_payload
from ..models import Segment
from ..studio import records
from .media_delivery import allowed_path

# What each kind of change costs (docs/adaptive-audio.md, plan §8).
# Seconds a request waits on ffmpeg for one line's audio.
REQUEST_TIMEOUT = 30.0

IMPACT = {
    "template": ("processing", "Reprocesses the existing take and remixes. No speech."),
    "strength": ("processing", "Reprocesses the existing take and remixes. No speech."),
    "envelope_clear": ("processing", "Renders the take as generated and remixes. No speech."),
    "background": ("mix", "Only the mix is rebuilt. No speech, no reprocessing."),
    "display": ("none", "Presentation only: nothing is rendered."),
    "voice": ("speech", "A new voice needs new speech for this character's lines."),
    "text": ("speech", "Changed words need new speech for that line."),
    "direction": ("speech", "A new acting direction needs a new take."),
}


class SelectIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str = Field(min_length=1, max_length=64)
    cue: str = Field(min_length=1, max_length=64)
    template: str = Field(default="", max_length=80)
    version: int | None = Field(default=None, ge=1)
    strength: float | None = Field(default=None, ge=0, le=1.5)
    clear: bool = False          # render the take as generated (an explicit preserve)
    undo: bool = False           # drop the hand choice; recommendations decide again


class ExampleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    feedback_id: str = Field(min_length=3, max_length=64)
    job_id: str = Field(min_length=1, max_length=64)
    scope: Literal["series", "global"] = "series"


class RetireIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_revision: int | None = None
    retired: bool = True


class HoldoutIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision_id: str = Field(min_length=3, max_length=64)
    held: bool = True
    note: str = Field(default="", max_length=300)


class GuidanceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    series_id: str = Field(min_length=3, max_length=120)
    bundle: dict[str, Any] | None = None
    templates: list[str] = Field(default_factory=list, max_length=500)


def build_router(config, store, bus) -> APIRouter:
    api = APIRouter()
    db = store.db

    def snapshot(job_id: str) -> tuple[Any, dict]:
        job = store.get(job_id)
        if job is None:
            raise HTTPException(404, f"no job {job_id}")
        path = allowed_path(config, job.review_file) if job.review_file else None
        data = read_json(path) if path else {}
        if not data.get("segments"):
            raise HTTPException(409, "this run has no review snapshot yet; render a draft")
        return job, data

    def recommendations(data: dict) -> dict:
        path = ((data.get("metrics") or {}).get("adaptive") or {}).get("path")
        found = allowed_path(config, path) if path else None
        return read_json(found) if found else {}

    def segment(data: dict, cue: str) -> Segment:
        for row in data["segments"]:
            record = row.get("cue") or {}
            if record.get("cue_id") == cue:
                seg = Segment(int(row["index"]), float(row["start"]), float(row["end"]),
                              row.get("text_src", ""), speaker=row.get("speaker", ""))
                apply_cue_payload(seg, record)
                return seg
        raise HTTPException(404, "no such line in this run")

    @api.get("/api/adaptive/recommendations")
    def get_recommendations(job_id: str):
        """Per line: candidates with score components and evidence, the decision
        and who made it, and what the render actually did."""
        _job, data = snapshot(job_id)
        report = recommendations(data)
        lines = []
        for row in data["segments"]:
            cue = (row.get("cue") or {}).get("cue_id")
            envelope = (row.get("cue") or {}).get("envelope") or {}
            found = (report.get("lines") or {}).get(cue) or {}
            lines.append({"cue": cue, "index": row.get("index"), "start": row.get("start"),
                          "end": row.get("end"), "speaker": row.get("speaker"),
                          "text": row.get("text_translated") or row.get("text_src"),
                          "envelope": envelope,
                          "candidates": found.get("candidates") or [],
                          "excluded": found.get("excluded") or [],
                          "warnings": found.get("warnings") or [],
                          "decision": found.get("decision"),
                          "locked": bool(found.get("locked") or envelope.get("locked"))})
        return {"job_id": job_id, "mode": report.get("mode", "off"),
                "judge": report.get("judge"), "judge_is_model": report.get("judge_is_model"),
                "states": report.get("states") or {}, "lines": lines,
                "edits": data.get("envelope_edits") or {},
                "background": (data.get("metrics") or {}).get("background_policy"),
                "mix_ducking": (data.get("metrics") or {}).get("mix_ducking")}

    @api.get("/api/adaptive/impact")
    def impact(change: str):
        """Before acting: does this change need new speech, processing or only a mix?"""
        if change not in IMPACT:
            raise HTTPException(422, f"unknown change; one of {', '.join(IMPACT)}")
        work, note = IMPACT[change]
        return {"change": change, "work": work, "speech": work == "speech", "note": note}

    @api.post("/api/adaptive/select")
    def select(body: SelectIn):
        """Choose, retune, clear or undo one line's envelope and queue only the
        reprocessing it needs (the takes are reused; no speech is requested)."""
        original, data = snapshot(body.job_id)
        segment(data, body.cue)
        if not (body.template or body.clear or body.undo):
            raise HTTPException(422, "choose a template, clear, or undo")
        if body.template:
            templates.ensure_builtins(db)
            found = templates.get(db, body.template, body.version)
            if found is None or found["kind"] != "voice":
                raise HTTPException(404, "no such voice template")
            if body.strength is not None:
                try:
                    templates.resolve_params(found, {"strength": body.strength})
                except ValueError as exc:
                    raise HTTPException(422, str(exc)) from exc
        overrides = copy.deepcopy(original.overrides or {})
        lines = dict(overrides.get("adaptive.lines") or (data.get("envelope_edits") or {}))
        if body.undo:
            lines.pop(body.cue, None)
        elif body.clear:
            lines[body.cue] = {"clear": True}
        else:
            lines[body.cue] = {"template": body.template,
                               **({"version": body.version} if body.version else {}),
                               **({"strength": body.strength}
                                  if body.strength is not None else {})}
        overrides["adaptive.lines"] = lines
        overrides["dub.dry_run"] = False
        job = store.add(title=original.title, source=original.source,
                        source_lang=original.source_lang, target_lang=original.target_lang,
                        target_locale=original.target_locale, input_file=original.input_file,
                        knowledge_snapshot=original.knowledge_snapshot, kind=original.kind,
                        overrides=overrides,
                        narrative_snapshot=getattr(original, "narrative_snapshot", None))
        bus.publish("job", {"type": "queued", "job_id": job.id, "title": job.title})
        work, note = IMPACT["envelope_clear" if body.clear else "template"]
        return {"job": job.id, "work": work, "speech": False, "note": note,
                "lines": lines}

    @api.get("/api/adaptive/audio")
    def audio(job_id: str, cue: str, kind: Literal["dry", "shaped", "source", "mixed"],
              level_matched: bool = False):
        """One line, four ways, from the same take. `level_matched` gives the
        shaped line at the dry line's level and says so in a header."""
        _job, data = snapshot(job_id)
        seg = segment(data, cue)
        if kind in ("dry", "shaped"):
            artifact = seg.audio.upstream_of(LEVELED) if kind == "dry" \
                else seg.audio.render(LEVELED)
            path = allowed_path(config, artifact.path) if artifact and artifact.path else None
            if path is None or not path.is_file():
                raise HTTPException(404, f"no {kind} audio for this line")
            if kind == "shaped" and level_matched:
                dry = seg.audio.upstream_of(LEVELED)
                dry_path = allowed_path(config, dry.path) if dry and dry.path else None
                if dry_path is None:
                    raise HTTPException(404, "no dry take to match against")
                return _matched(path, dry_path)
            return FileResponse(path, media_type="audio/wav",
                                headers={"X-Level-Matched": "false"})
        media = data.get("media") or {}
        source = allowed_path(config, media.get("source_track") or media.get("source_audio")
                              or "") if kind == "source" else allowed_path(
            config, media.get("dubbed_track") or "")
        if source is None or not source.is_file():
            raise HTTPException(404, f"no {kind} audio for this run")
        from ..ffmpeg import run_ffmpeg_bytes

        clip = run_ffmpeg_bytes(["-v", "error", "-ss", f"{max(0.0, seg.start - 0.2):.3f}",
                                 "-t", f"{seg.duration + 0.4:.3f}", "-i", str(source), "-ac", "2",
                                 "-ar", "48000", "-f", "wav", "-"], timeout=REQUEST_TIMEOUT)
        if not clip:
            raise HTTPException(500, "could not cut the line")
        return Response(content=clip, media_type="audio/wav",
                        headers={"X-Level-Matched": "false"})

    def _matched(shaped: Path, dry: Path) -> Response:
        import numpy as np

        a, rate = envelopes.read_wav(shaped)
        b, _ = envelopes.read_wav(dry)
        ra = float(np.sqrt(np.mean(a ** 2))) or 1e-9
        rb = float(np.sqrt(np.mean(b ** 2))) or 1e-9
        gain_db = 20 * math.log10(rb / ra)
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "matched.wav"
            envelopes.write_wav(out, np.clip(a * (rb / ra), -1, 1), rate)
            content = out.read_bytes()
        return Response(content=content, media_type="audio/wav",
                        headers={"X-Level-Matched": "true",
                                 "X-Level-Match-Gain-Db": f"{gain_db:.2f}"})

    @api.post("/api/adaptive/feedback")
    def post_feedback(body: feedback.VerdictIn):
        try:
            return feedback.record_verdict(db, body)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @api.post("/api/adaptive/examples")
    def approve(body: ExampleIn):
        """Make an accepted verdict an example for future retrieval."""
        verdict = records.get(db, "feedback", body.feedback_id)
        if verdict is None:
            raise HTTPException(404, "no such verdict")
        _job, data = snapshot(body.job_id)
        report = recommendations(data)
        line = (report.get("lines") or {}).get(verdict["cue"]) or {}
        packet = line.get("packet") or {}
        source_curve = (packet.get("original") or {}).get("curve") or []
        take_curve = (packet.get("take") or {}).get("curve") or []
        ident = ((data.get("metrics") or {}).get("identity") or {})
        try:
            return feedback.approve_example(
                db, verdict, source_curve=source_curve, take_curve=take_curve,
                target_lang=str(data.get("language") or ""),
                series_id=ident.get("series_id", ""), scope=body.scope)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @api.get("/api/adaptive/examples")
    def list_examples(series_id: str | None = None, include_retired: bool = False):
        rows = feedback.examples(db, series_id, include_retired=include_retired)
        return {"examples": [{**r, "dependents": feedback.dependents(db, r["id"])}
                             for r in rows],
                "held_out": sorted(feedback.held_out(db))}

    @api.post("/api/adaptive/examples/{example_id}/retire")
    def retire(example_id: str, body: RetireIn):
        try:
            return feedback.retire_example(db, example_id, base_revision=body.base_revision,
                                           retired=body.retired)
        except KeyError as exc:
            raise HTTPException(404, "no such example") from exc
        except records.StudioConflict as exc:
            raise HTTPException(409, {"error": str(exc), "current": exc.current}) from exc

    @api.post("/api/adaptive/holdout")
    def holdout(body: HoldoutIn):
        return feedback.hold_out(db, body.revision_id, held=body.held, note=body.note)

    @api.post("/api/adaptive/guidance/export")
    def export_guidance(body: GuidanceIn):
        try:
            return feedback.export_guidance(db, template_ids=body.templates,
                                            series_id=body.series_id)
        except KeyError as exc:
            raise HTTPException(404, f"no such template: {exc}") from exc

    @api.post("/api/adaptive/guidance/import")
    def import_guidance(body: GuidanceIn):
        if not body.bundle:
            raise HTTPException(422, "a guidance bundle is required")
        try:
            return feedback.import_guidance(db, body.bundle, series_id=body.series_id)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    return api
