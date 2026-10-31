"""The template catalogue (doblarr.templates): list, edit, version, port, preview.

Every edit is a new version; saved jobs keep the version they pinned. A preview
renders a template over existing audio (a take already on disk, or a built-in
test phrase): processing only, never a speech request.
"""

from __future__ import annotations

import math
import tempfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from .. import envelopes, features, templates
from ..fileio import inside
from ..studio import records


class SaveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    template: dict[str, Any]
    base_version: int = Field(ge=0)
    author: str = Field(default="", max_length=100)


class DuplicateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    new_id: str = Field(min_length=3, max_length=80)
    title: str = Field(default="", max_length=80)


class RetireIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_version: int = Field(ge=1)


class ExportIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ids: list[str] = Field(min_length=1, max_length=500)


class ImportIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bundle: dict[str, Any]


class PreviewIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    template_id: str = Field(default="", max_length=80)
    version: int | None = Field(default=None, ge=1)
    template: dict[str, Any] | None = None       # an unsaved draft, validated first
    params: dict[str, float] = Field(default_factory=dict)
    artifact: str = Field(default="", max_length=2000)   # a take inside the work folder


def test_phrase(path: Path, seconds: float = 2.4, rate: int = 24000) -> Path:
    """A speech-like test signal: three syllable bursts with a pause."""
    import numpy as np

    t = np.arange(int(seconds * rate)) / rate
    carrier = 0.25 * np.sin(2 * math.pi * 180 * t) + 0.1 * np.sin(2 * math.pi * 360 * t)
    bursts = np.zeros_like(t)
    for start, end in ((0.15, 0.6), (0.75, 1.2), (1.6, 2.2)):
        inside = (t >= start) & (t < end)
        bursts[inside] = np.sin(np.pi * (t[inside] - start) / (end - start))
    envelopes.write_wav(path, (carrier * bursts)[:, None], rate)
    return path


def build_router(config, db) -> APIRouter:
    api = APIRouter()

    def conflict(exc: records.StudioConflict):
        return HTTPException(409, {"error": str(exc), "current": exc.current})

    @api.get("/api/templates")
    def listing(kind: str | None = None, include_retired: bool = False):
        rows = templates.listing(db, kind, include_retired)
        for row in rows:
            if row["kind"] == "voice":
                row["preview_curve"] = templates.describe_curve(row)
        return {"templates": rows, "catalogue": templates.CATALOGUE,
                "families": templates.FAMILIES}

    @api.get("/api/templates/{template_id:path}/history")
    def history(template_id: str):
        rows = templates.history(db, template_id)
        if not rows:
            raise HTTPException(404, "no such template")
        return {"history": rows}

    @api.get("/api/templates/{template_id:path}")
    def detail(template_id: str, version: int | None = None):
        templates.ensure_builtins(db)
        found = templates.get(db, template_id, version)
        if found is None:
            raise HTTPException(404, "no such template")
        return {"template": found, "preview_curve": templates.describe_curve(found)
                if found["kind"] == "voice" else None}

    @api.put("/api/templates")
    def save(body: SaveIn):
        try:
            return templates.save(db, body.template, base_version=body.base_version,
                                  author=body.author)
        except records.StudioConflict as exc:
            raise conflict(exc) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @api.post("/api/templates/{template_id:path}/duplicate")
    def duplicate(template_id: str, body: DuplicateIn):
        try:
            return templates.duplicate(db, template_id, body.new_id, title=body.title)
        except KeyError as exc:
            raise HTTPException(404, "no such template") from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @api.post("/api/templates/{template_id:path}/retire")
    def retire(template_id: str, body: RetireIn):
        try:
            return templates.retire(db, template_id, base_version=body.base_version)
        except KeyError as exc:
            raise HTTPException(404, "no such template") from exc
        except records.StudioConflict as exc:
            raise conflict(exc) from exc

    @api.post("/api/templates-export")
    def export(body: ExportIn):
        try:
            return templates.export(db, body.ids)
        except KeyError as exc:
            raise HTTPException(404, f"no such template: {exc}") from exc

    @api.post("/api/templates-import")
    def import_bundle(body: ImportIn):
        try:
            return templates.import_bundle(db, body.bundle)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except records.StudioConflict as exc:
            raise conflict(exc) from exc

    @api.post("/api/templates-preview")
    def preview(body: PreviewIn):
        """The template rendered over existing audio. Processing only."""
        if body.template is not None:
            try:
                found = {**templates.validate(body.template).model_dump(), "version": 0}
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
        else:
            templates.ensure_builtins(db)
            found = templates.get(db, body.template_id, body.version) or {}
            if not found:
                raise HTTPException(404, "no such template")
        if found["kind"] != "voice":
            raise HTTPException(422, "only voice templates are previewed on a line")
        try:
            params = templates.resolve_params(found, body.params)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        with tempfile.TemporaryDirectory() as tmp:
            if body.artifact:
                source = Path(body.artifact)
                if not inside(source, [config.work_dir]):
                    raise HTTPException(403, "previews read audio inside the work folder")
                if not source.is_file() or source.suffix.lower() != ".wav":
                    raise HTTPException(404, "no such take")
            else:
                source = test_phrase(Path(tmp) / "phrase.wav")
            measured = features.measure_file(source)
            fitted = envelopes.fit(found, params["values"], measured, measured["duration"])
            out = Path(tmp) / "preview.wav"
            stats = envelopes.render(source, out, 0.0, fitted["curve"])
            audio = out.read_bytes()
        headers = {"X-Envelope-Range-Db": str(fitted["range_db"]),
                   "X-Envelope-Preserved": str(fitted["preserved"]),
                   "X-Envelope-Peak": str(stats["peak"])}
        return Response(content=audio, media_type="audio/wav", headers=headers)

    return api
