"""Characters of a series and their voice profiles (doblarr.identity, doblarr.profiles).

Reads never generate speech and never clone: a profile, its references, its
proposals and its engine capabilities are all metadata. Writes name the
revision they were made against; a stale write is a 409 carrying the newer
document. Assignments of a voice to a character are casting choices
(doblarr.studio.casting) at series scope, optionally per locale and variant.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .. import identity, profiles, speaking
from ..studio import casting, records


class CharacterIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    series_id: str = Field(min_length=3, max_length=120)
    name: str = Field(min_length=1, max_length=80)
    role: str = Field(default="", max_length=40)


class VariantIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1, max_length=80)
    kind: Literal["age", "transformation", "edition", "disguise", "other"] = "other"
    notes: str = Field(default="", max_length=500)


class CharacterPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_revision: int = Field(ge=0)
    name: str | None = Field(default=None, min_length=1, max_length=80)
    merge_from: str | None = Field(default=None, max_length=40)
    variant: VariantIn | None = None
    retired: bool | None = None


class ProfilePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_revision: int = Field(ge=0)
    set: dict[str, Any] = Field(default_factory=dict)
    lock: list[str] = Field(default_factory=list, max_length=100)
    unlock: list[str] = Field(default_factory=list, max_length=100)


class ApproveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_revision: int = Field(ge=0)
    ids: list[str] = Field(min_length=1, max_length=200)


class ReferenceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_revision: int = Field(ge=0)
    reference: dict[str, Any]


class ReferenceReview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_revision: int = Field(ge=0)
    approved: bool | None = None
    retired: bool | None = None


class AuditionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_revision: int = Field(ge=0)
    audition: dict[str, Any]


class AssignmentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    voice: str = Field(default="", max_length=200)
    engine: str = Field(default="", max_length=40)
    locale: str = Field(default="", max_length=16)
    variant: str = Field(default="", max_length=40)
    direction: str = Field(default="", max_length=500)
    pitch_semitones: float = Field(default=0.0, ge=-6.0, le=6.0)
    formant_semitones: float = Field(default=0.0, ge=-6.0, le=6.0)
    clear: bool = False
    base_revision: int | None = None



def build_router(config, services, db) -> APIRouter:
    api = APIRouter()

    def character(cid: str) -> dict:
        found = records.get(db, "character", cid)
        if found is None:
            raise HTTPException(404, "no such character")
        return found

    def conflict(exc: records.StudioConflict):
        return HTTPException(409, {"error": str(exc), "current": exc.current})

    @api.get("/api/characters")
    def list_characters(series_id: str = "", include_retired: bool = False):
        """The characters of one series, or of every series when none is given."""
        if series_id:
            found_all = identity.characters(db, series_id, include_retired=include_retired)
        else:
            found_all = [c for c in records.list_latest(db, "character")
                         if include_retired or not (c.get("retired") or c.get("merged_into"))]
        rows = []
        for found in found_all:
            profile = profiles.get(db, found["id"])
            chosen = casting.assignments(db, identity.casting_ref(found["series_id"]),
                                         found["name"])
            rows.append({**found, "profile_revision": profile["revision"],
                         "references": profiles.coverage(profile),
                         "assignments": len(chosen),
                         "voices": sorted({str(c["voice"]) for c in chosen if c.get("voice")})})
        return {"series_id": series_id, "characters": sorted(
            rows, key=lambda r: (str(r.get("series_id") or ""), r["name"].casefold()))}

    @api.post("/api/characters")
    def create_character(body: CharacterIn):
        return identity.ensure_character(db, body.series_id, body.name, role=body.role)

    @api.patch("/api/characters/{cid}")
    def patch_character(cid: str, body: CharacterPatch):
        current = character(cid)
        try:
            if body.merge_from:
                return identity.merge_characters(db, cid, body.merge_from,
                                                 base_revision=body.base_revision)
            if body.name:
                return identity.rename_character(db, cid, body.name,
                                                 base_revision=body.base_revision)
            if body.variant:
                return identity.add_variant(db, cid, body.variant.label, body.variant.kind,
                                            notes=body.variant.notes,
                                            base_revision=body.base_revision)
            if body.retired is not None:
                return records.update(db, "character", cid, {"retired": body.retired},
                                      base_revision=body.base_revision)
        except records.StudioConflict as exc:
            raise conflict(exc) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return current

    def capabilities(found: dict) -> list[dict]:
        rows = []
        try:
            speech = services.speech
        except Exception:  # noqa: BLE001 - no speech service configured: unknown
            speech = None
        for choice in casting.assignments(db, identity.casting_ref(found["series_id"]),
                                          found["name"]):
            caps = profiles.engine_capabilities(speech, choice.get("engine") or "") \
                if speech is not None else {"engine": choice.get("engine") or "",
                                            "direction": None, "cloning": None,
                                            "supported_controls": [], "unsupported": []}
            rows.append({**choice, "capabilities": caps,
                         "variations": profiles.variation_support(caps)})
        return rows

    @api.get("/api/characters/{cid}/profile")
    def get_profile(cid: str):
        found = character(cid)
        profile = profiles.get(db, cid)
        return {"character": found, "profile": profile,
                "references": profiles.coverage(profile),
                "assignments": capabilities(found)}

    @api.patch("/api/characters/{cid}/profile")
    def patch_profile(cid: str, body: ProfilePatch):
        character(cid)
        try:
            return profiles.update(db, cid, set_fields=body.set, lock=body.lock,
                                   unlock=body.unlock, base_revision=body.base_revision)
        except records.StudioConflict as exc:
            raise conflict(exc) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    def character_lines(found: dict) -> list[dict]:
        """Every analysed line identified as this character, across revisions."""
        rows: list[dict] = []
        for link in records.list_latest(db, "association"):
            if link.get("subject") != "cluster" or link.get("state") not in ("manual",
                                                                              "accepted"):
                continue
            target = identity.resolve_character(db, link.get("character_id") or "")
            if target is None or target["id"] != found["id"]:
                continue
            revision_id = link["revision_id"]
            for snap in records.list_latest(db, "snapshot", scope=revision_id):
                script = ((snap.get("stages") or {}).get("transcribe") or {}).get(
                    "outputs", {}).get("script")
                if not script or not Path(script).is_file():
                    continue
                extra_path = Path(script).with_name(
                    Path(script).name.replace(".script.json", ".analysis.json"))
                extra = {}
                if extra_path.is_file():
                    extra = {r["cue"]: r for r in json.loads(
                        extra_path.read_text(encoding="utf-8")).get("lines") or []}
                source = str((json.loads(Path(script).read_text(encoding="utf-8"))
                              .get("identity") or {}).get("input") or "")
                for seg in speaking.load_segments(Path(script)):
                    if seg.get("speaker") != link["ref"]:
                        continue
                    cue = seg.get("cue") or {}
                    level = (cue.get("measurement") or {}).get("relative_db")
                    analysed = extra.get(cue.get("cue_id"), {})
                    rows.append({"revision_id": revision_id, "cue": cue.get("cue_id"),
                             "input": source, "episode": Path(source).stem if source else "",
                                 "start": seg.get("start"), "end": seg.get("end"),
                                 "text": seg.get("text_src") or "",
                                 "original_text": analysed.get("original_text"),
                                 "pitch_hz": analysed.get("pitch_hz"),
                                 "relative_db": level, "band": speaking.band(level),
                                 "uncertain": "speaker_uncertain" in (seg.get("issues")
                                                                      or [])})
                break
        return rows

    @api.get("/api/characters/{cid}/appearances")
    def appearances(cid: str):
        """Where this character speaks: each analysed episode with its lines,
        so the page can show stills and play them. Reads only."""
        episodes: dict[str, dict] = {}
        for row in character_lines(character(cid)):
            found = episodes.setdefault(row["revision_id"], {
                "revision_id": row["revision_id"], "episode": row["episode"],
                "path": row["input"], "reachable": bool(row["input"])
                and Path(row["input"]).is_file(), "lines": []})
            found["lines"].append({k: row[k] for k in (
                "cue", "start", "end", "text", "original_text", "pitch_hz", "relative_db",
                "band")})
        rows = sorted(episodes.values(), key=lambda e: e["episode"])
        for row in rows:
            row["lines"].sort(key=lambda line: line["start"] or 0)
            row["seconds"] = round(sum((line["end"] or 0) - (line["start"] or 0)
                                       for line in row["lines"]), 1)
        return {"episodes": rows, "lines": sum(len(e["lines"]) for e in rows)}

    @api.get("/api/characters/{cid}/proposals")
    def proposals(cid: str):
        """Field and reference proposals from analysed lines. Writes nothing."""
        found = character(cid)
        profile = profiles.get(db, cid)
        lines = character_lines(found)
        rows = profiles.proposals_from_lines(lines)
        for row in rows:
            row["locked"] = profiles.locked(profile, row["field"])
        return {"lines": len(lines), "proposals": rows, "revision": profile["revision"]}

    @api.post("/api/characters/{cid}/proposals/approve")
    def approve(cid: str, body: ApproveIn):
        found = character(cid)
        rows = profiles.proposals_from_lines(character_lines(found))
        try:
            return profiles.apply_proposals(db, cid, rows, body.ids,
                                            base_revision=body.base_revision)
        except records.StudioConflict as exc:
            raise conflict(exc) from exc

    @api.post("/api/characters/{cid}/references")
    def add_reference(cid: str, body: ReferenceIn):
        character(cid)
        try:
            return profiles.add_reference(db, cid, body.reference,
                                          base_revision=body.base_revision)
        except records.StudioConflict as exc:
            raise conflict(exc) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @api.patch("/api/characters/{cid}/references/{rid}")
    def review_reference(cid: str, rid: str, body: ReferenceReview):
        character(cid)
        try:
            return profiles.review_reference(db, cid, rid, approved=body.approved,
                                             retired=body.retired,
                                             base_revision=body.base_revision)
        except KeyError as exc:
            raise HTTPException(404, "no such reference") from exc
        except records.StudioConflict as exc:
            raise conflict(exc) from exc

    @api.post("/api/characters/{cid}/auditions")
    def add_audition(cid: str, body: AuditionIn):
        character(cid)
        try:
            return profiles.record_audition(db, cid, body.audition,
                                            base_revision=body.base_revision)
        except records.StudioConflict as exc:
            raise conflict(exc) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @api.get("/api/characters/{cid}/assignments")
    def list_assignments(cid: str):
        found = character(cid)
        return {"assignments": capabilities(found)}

    @api.put("/api/characters/{cid}/assignments")
    def put_assignment(cid: str, body: AssignmentIn):
        """Cast a voice for this character, for a locale and variant or for any.
        Recorded as a series casting choice; nothing is rendered here."""
        found = character(cid)
        series_ref = identity.casting_ref(found["series_id"])
        try:
            saved = casting.decide(db, casting.ChoiceIn(
                character=found["name"], scope="series", scope_ref=series_ref,
                voice=body.voice, engine=body.engine, direction=body.direction,
                pitch_semitones=body.pitch_semitones,
                formant_semitones=body.formant_semitones, character_id=cid,
                variant=body.variant, locale=body.locale, clear=body.clear),
                body.base_revision)
        except records.StudioConflict as exc:
            raise conflict(exc) from exc
        return {"casting_revision": saved["revision"], "assignments": capabilities(found)}

    return api
