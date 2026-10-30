"""Voice studies: profiles from one film, tested held out on another (docs/voice-study.md).

Reads two analysed films' voice sidecars. Never translates, clones or speaks.
The probe is quick arithmetic over stored vectors, so it runs in the request
rather than as a job. Deleting a study removes its profiles and results.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .. import voice_study
from ..studio import records, voice_studies
from .media_delivery import allowed_path


class FilmIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=1000)
    names: dict[str, str] = Field(default_factory=dict, max_length=200)


class StudyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=80)
    source: FilmIn
    probes: list[FilmIn] = Field(default_factory=list, max_length=10)
    floor: float = Field(default=0.3, ge=0.0, le=1.0)
    margin: float = Field(default=0.05, ge=0.0, le=1.0)


class ProbeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=1000)


def build_router(config, db) -> APIRouter:
    api = APIRouter()

    def check(film: FilmIn) -> None:
        # A sidecar is read as given, so it must be under the work or output folder.
        if film.path.endswith(".speakers.json") and allowed_path(config, film.path) is None:
            raise HTTPException(422, "a voice sidecar must be inside the work or output folder")

    def study(sid: str) -> dict:
        found = voice_studies.detail(db, sid)
        if found is None:
            raise HTTPException(404, "no such voice study")
        return found

    @api.post("/api/voice-studies")
    def create_study(body: StudyIn):
        for film in (body.source, *body.probes):
            check(film)
        try:
            made = voice_studies.create(db, config.work_dir, body.name, body.source.model_dump(),
                                        [p.model_dump() for p in body.probes],
                                        floor=body.floor, margin=body.margin)
        except voice_study.StudyError as exc:
            raise HTTPException(422, str(exc)) from exc
        return voice_studies.detail(db, made["id"])

    @api.get("/api/voice-studies")
    def list_studies():
        return {"studies": [
            {k: s[k] for k in ("id", "name", "status", "model", "thresholds", "created_at")}
            | {"results": [{"probe": r["probe"], "top1": r["top1"], "precision": r["precision"],
                            "coverage": r["coverage"]}
                           for r in records.list_latest(db, "voice_probe_result", s["id"])]}
            for s in records.list_latest(db, "voice_study")]}

    @api.get("/api/voice-studies/{sid}")
    def get_study(sid: str):
        return study(sid)

    @api.post("/api/voice-studies/{sid}/probe")
    def probe_study(sid: str, body: ProbeIn):
        study(sid)
        try:
            return voice_studies.probe(db, config.work_dir, sid, body.path)
        except KeyError:
            raise HTTPException(404, "that film is not one of the study's probes") from None
        except voice_study.StudyError as exc:
            raise HTTPException(422, str(exc)) from exc

    @api.get("/api/voice-studies/{sid}/matrix")
    def matrix(sid: str, probe: str = ""):
        results = study(sid)["results"]
        found = next((r for r in results if not probe or r["probe"] == probe), None)
        if found is None:
            raise HTTPException(404, "no probe result yet")
        return {k: found[k] for k in ("probe", "matrix", "per_character", "top1", "precision",
                                      "coverage", "lines", "floor", "margin", "unseen")}

    @api.delete("/api/voice-studies/{sid}")
    def delete_study(sid: str):
        study(sid)
        return {"deleted": voice_studies.delete(db, sid)}

    return api
