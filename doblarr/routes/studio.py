"""The episode studio API.

Everything the studio shows comes from existing authorities: the job queue,
the run's review snapshot, the decisions sidecar and saved versions. The
studio's own records (references, alignments, experiments, auditions, casting,
notes, evaluation) live in versioned rows, and every write names the revision
it was made against so a stale edit is a conflict instead of a lost one.

Machine-local paths never reach the browser. Media is streamed through the same
path guard and range responses as the job previews.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from ..artifacts import read_json
from ..clients.speech import build_speech_client
from ..config import Config
from ..errors import NotFoundError
from ..events import EventBus
from ..jobs import JobStore, Worker
from ..knowledge import snapshot as knowledge_snapshot
from ..languages import base_language
from ..review import load_decisions, snapshot_revision  # noqa: F401 - reused identity
from ..studio import (
    alignment,
    auditions,
    casting,
    evaluation,
    experiments,
    importer,
    media,
    records,
    session,
    sources,
)
from ..studio.records import FrozenRecord, StudioConflict
from ..voices import cast_key
from .media_delivery import allowed_path, ranged_response


class SessionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=2000)
    title: str = Field(min_length=1, max_length=300)
    job_id: str = Field(default="", max_length=64)
    series_ref: str = Field(default="", max_length=200)
    target_locale: str = Field(default="", max_length=16)


class ReferenceCreate(sources.ReferenceIn):
    utterances: list[dict] | None = Field(default=None, max_length=sources.MAX_UTTERANCES)
    subtitle_stream: int | None = Field(default=None, ge=0, le=64)
    from_job: bool = False   # the run's own cues are the text (meaning source)


class ReferencePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_revision: int = Field(ge=1)
    roles: list[str] | None = Field(default=None, max_length=5)
    label: str | None = Field(default=None, max_length=120)
    notes: str | None = Field(default=None, max_length=2000)
    sample: sources.SampleWindow | None = None


class TranscribeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    windows: list[tuple[float, float]] = Field(min_length=1, max_length=40)
    engine: Literal["whisper", "voicebox"] = "whisper"


class AlignmentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str = Field(min_length=1, max_length=64)       # meaning reference id
    reference: str = Field(min_length=1, max_length=64)
    time_map: dict | None = None
    estimate: bool = False


class OverrideIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_revision: int = Field(ge=1)
    action: Literal["link", "unlink", "exclude", "include", "remap"]
    group_id: str | None = Field(default=None, max_length=64)
    source: list[str] | None = Field(default=None, max_length=20)
    reference: list[str] | None = Field(default=None, max_length=20)
    time_map: dict | None = None
    note: str = Field(default="", max_length=500)
    actor: str = Field(default="", max_length=100)


class RunIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conditions: list[Literal["A", "B", "C"]] | None = None
    repeats: int | None = Field(default=None, ge=1, le=experiments.MAX_REPEATS)


class RenderVariantIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str = Field(min_length=1, max_length=64)


class NoteIn(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    job_id: str = Field(default="", max_length=64)
    cue: str = Field(default="", max_length=64)
    line: int | None = Field(default=None, ge=0)
    source: str = Field(default="", max_length=80)     # what was playing
    domain: Literal["source", "target", "clip", "montage"] = "target"
    at: float = Field(ge=0, le=36000)
    category: str = Field(default="", max_length=60)
    severity: Literal["minor", "noticeable", "major"] = "noticeable"
    in_original: Literal["", "yes", "no", "not sure"] = ""
    note: str = Field(default="", max_length=2000)
    variant: str = Field(default="", max_length=128)
    reviewer: str = Field(default="", max_length=100)
    snapshot_revision: str = Field(default="", max_length=64)


class NotePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_revision: int = Field(ge=1)
    resolution: Literal["open", "repair_queued", "resolved", "wont_fix"] | None = None
    note: str | None = Field(default=None, max_length=2000)
    category: str | None = Field(default=None, max_length=60)
    severity: Literal["minor", "noticeable", "major"] | None = None
    verdict: str | None = Field(default=None, max_length=60)
    linked_edit: dict | None = None


class SelectIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate: str = Field(min_length=1, max_length=40)
    scope: Literal["series", "episode"] = "episode"
    actor: str = Field(default="", max_length=100)
    apply: bool = False     # also write the run's cast and queue this character only


class ImportIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=2000)
    mapping: dict | None = None
    actor: str = Field(default="", max_length=100)


class ResultsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(default="results.md", max_length=200)
    text: str = Field(min_length=1, max_length=importer.MAX_RESULTS_BYTES)


class ExportIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    takes: dict[str, str] = Field(default_factory=dict)   # cue id -> take id
    allow_generation: bool = False
    base_revision: str = Field(default="", max_length=64)


def build_router(config: Config, store: JobStore, worker: Worker, bus: EventBus) -> APIRouter:
    api = APIRouter(prefix="/api/studio")
    db = store.db

    def conflict(exc: StudioConflict | FrozenRecord):
        current = getattr(exc, "current", None)
        raise HTTPException(409, {"error": str(exc), "current": _redact(current)
                                  if current else None}) from exc

    def need(kind: str, record_id: str) -> dict:
        found = records.get(db, kind, record_id)
        if found is None:
            raise NotFoundError(f"no {kind} {record_id}")
        return found

    def need_session(sid: str) -> dict:
        return need("session", sid)

    def queue(title: str, kind: str, task: dict, *, source_lang: str = "",
              target_lang: str = "") -> dict:
        job = store.add(title=title, source="studio", source_lang=source_lang or "und",
                        target_lang=target_lang or "und", kind=kind, task=task)
        bus.publish("job", {"type": "queued", "job_id": job.id, "title": job.title})
        return {"id": job.id, "kind": kind, "status": job.status}

    def jobs_for(path: str) -> list[dict]:
        key = cast_key(path=path)
        rows = [j for j in store.list() if j.get("input_file")
                and cast_key(path=j["input_file"]) == key
                and not str(j.get("kind", "")).startswith("studio_")]
        for row in rows:
            review = allowed_path(config, row["review_file"]) if row.get("review_file") else None
            row["has_review"] = bool(review and review.is_file())
            out = allowed_path(config, row["output_file"]) if row.get("output_file") else None
            row["has_file"] = bool(out and out.is_file())
            for local in ("input_file", "output_file", "report_file", "review_file",
                          "version_file", "overrides", "knowledge_snapshot", "task"):
                row.pop(local, None)
        return rows

    def snapshot(job_id: str) -> tuple[Any, dict]:
        job = store.get(job_id)
        if job is None:
            raise NotFoundError(f"no job {job_id}")
        path = allowed_path(config, job.review_file) if job.review_file else None
        data = read_json(path) if path else {}
        if not data.get("segments"):
            raise HTTPException(409, "this run has no review snapshot yet; render a draft")
        return job, data

    def active_job_id(sess: dict) -> str:
        """The run the studio is looking at: the linked one, else the newest reviewable."""
        if sess.get("job_id"):
            return sess["job_id"]
        found = next((j for j in jobs_for(sess["path"]) if j.get("has_review")), None)
        return found["id"] if found else ""

    def studio_jobs(sid: str) -> list[dict]:
        rows = []
        for j in store.list():
            if not str(j.get("kind", "")).startswith("studio_"):
                continue
            if (j.get("task") or {}).get("session") != sid:
                continue
            rows.append({k: j.get(k) for k in ("id", "kind", "status", "progress", "message",
                                               "stage", "created_at", "updated_at")})
        return rows

    # ------------------------------------------------------------------ session
    @api.post("/sessions")
    def open_session(body: SessionIn):
        target = allowed_media(body.path)
        found = session.create(db, path=str(target), title=body.title, job_id=body.job_id,
                               series_ref=body.series_ref, target_locale=body.target_locale)
        return {"session": _public_session(found)}

    def allowed_media(path: str) -> Path:
        # A studio opens an episode that Doblarr could already dub: the file must
        # exist. It is read, never written, so it may sit in the library.
        target = Path(path)
        if not target.is_file():
            raise HTTPException(422, "open the studio on an episode or movie file that exists")
        return target.resolve()

    @api.get("/sessions")
    def list_sessions():
        return {"sessions": [_public_session(s) for s in records.list_latest(db, "session")]}

    @api.get("/sessions/{sid}")
    def get_session(sid: str):
        found = need_session(sid)
        jobs = jobs_for(found["path"])
        active = next((j for j in jobs if j["id"] == found.get("job_id")), None) or next(
            (j for j in jobs if j.get("has_review")), None)
        refs = records.list_latest(db, "reference", sid)
        state = {"job": active, "cast_decided": bool(
            (records.get(db, "casting", casting.record_id("episode", found["title_ref"]))
             or {}).get("characters"))}
        plan = None
        if active and active.get("has_review"):
            try:
                _job, data = snapshot(active["id"])
                plan = session.export_plan(data, load_decisions(config.work_dir, active["id"]))
                state["stale"] = plan["stale"]
                reviewed = (load_decisions(config.work_dir, active["id"]).get("cues") or {})
                state["script_reviewed"] = sum(
                    1 for e in reviewed.values()
                    if e.get("reviewed") and e.get("revision") == data.get("revision")) >= len(
                    data["segments"])
            except HTTPException:
                plan = None
        outputs = []
        if active and active.get("has_file"):
            job_row = store.get(active["id"])
            try:
                outputs = [{"audio_index": s["audio_index"], "language": s["language"],
                            "title": s["title"]}
                           for s in media.probe(Path(job_row.output_file))["streams"]
                           if s["type"] == "audio"] if job_row and job_row.output_file else []
            except Exception:  # noqa: BLE001 - an unreadable output just lists nothing
                outputs = []
        return {
            "session": _public_session(found),
            "jobs": jobs, "active_job": active, "output_tracks": outputs,
            "studio_jobs": studio_jobs(sid),
            "references": [_public_reference(r) for r in refs],
            "alignments": [_alignment_summary(a) for a in records.list_latest(db, "alignment",
                                                                              sid)],
            "experiments": [_experiment_summary(e) for e in records.list_latest(
                db, "experiment", sid)],
            "auditions": [_audition_summary(a) for a in records.list_latest(db, "audition",
                                                                            sid)],
            "imports": [_import_summary(i) for i in records.list_latest(db, "import", sid)],
            "notes": len([n for n in records.list_latest(db, "annotation", sid)
                          if n.get("resolution") in (None, "open", "repair_queued")]),
            "export": ({"stale": plan["stale"], "unresolved": len(plan["unresolved"])}
                       if plan else None),
            "next": session.next_action(found, state),
            "styles": session.STYLES, "checkpoints": session.CHECKPOINTS,
        }

    @api.patch("/sessions/{sid}")
    def patch_session(sid: str, body: session.SessionPatch):
        need_session(sid)
        try:
            return {"session": _public_session(session.patch(db, sid, body))}
        except (StudioConflict, FrozenRecord) as exc:
            conflict(exc)

    @api.get("/sessions/{sid}/probe")
    def probe(sid: str):
        found = need_session(sid)
        return media.probe(Path(found["path"]))

    @api.post("/sessions/{sid}/render")
    def render(sid: str, body: dict | None = None):
        """Queue a draft through the ordinary dub queue with the studio's direction."""
        found = need_session(sid)
        direction = found.get("direction") or {}
        reference_file, holdout = _direction_files(found)
        overrides = session.overrides(found, reference_file, holdout)
        target = direction.get("target_locale") or config["general"]["target_languages"][0]
        base = base_language(target)
        job = store.add(title=found["title"], source="studio",
                        source_lang=(body or {}).get("source_lang") or "auto",
                        target_lang=base, target_locale=target if target != base else "",
                        input_file=found["path"], kind=(body or {}).get("kind") or "full",
                        overrides=overrides,
                        knowledge_snapshot=knowledge_snapshot(db))
        bus.publish("job", {"type": "queued", "job_id": job.id, "title": job.title})
        session.patch(db, sid, session.SessionPatch(
            base_revision=found["revision"], job_id=job.id))
        return {"job": {"id": job.id, "status": job.status},
                "overrides": {k: v for k, v in overrides.items()
                              if not k.endswith("_file") and k != "translate.holdout_files"}}

    def _direction_files(found: dict) -> tuple[str, list[str]]:
        """Build the aligned reference file for a render, and list holdout files."""
        direction = found.get("direction") or {}
        holdout = []
        for ref_id in direction.get("evaluation") or []:
            ref = records.get(db, "reference", ref_id)
            if ref and sources.is_evaluation(ref) and (ref.get("text") or {}).get("artifact"):
                holdout.append(ref["text"]["artifact"])
        if (direction.get("reference_policy") or "original_only") == "original_only":
            return "", holdout
        ref = records.get(db, "reference", direction.get("reference") or "")
        body = records.get(db, "alignment", direction.get("alignment") or "")
        if not ref or not body or body.get("reference") != ref["id"]:
            raise HTTPException(422, "a reference policy needs an adaptation reference and "
                                     "its alignment")
        if sources.is_evaluation(ref):
            raise HTTPException(422, "an evaluation-only reference cannot inform a render")
        rows = {u["utt_id"]: u for u in sources.read_utterances(ref)}
        groups = [{"start": g["span"]["start"], "end": g["span"]["end"],
                   "state": g["state"], "confidence": g.get("confidence"),
                   "text": " ".join(rows[r]["text"] for r in g["reference"] if r in rows)}
                  for g in body.get("groups", []) if g.get("span")]
        path = (config.work_dir / "studio" / "references" / found["id"]
                / f"aligned-{body['id']}-r{body['revision']}.json")
        from ..fileio import write_json

        write_json(path, {"reference": ref["id"], "language": ref["language"],
                          "alignment": body["id"], "revision": body["revision"],
                          "groups": groups})
        return str(path), holdout

    # -------------------------------------------------------------- references
    @api.post("/sessions/{sid}/references")
    def add_reference(sid: str, body: ReferenceCreate):
        found = need_session(sid)
        data = sources.ReferenceIn.model_validate(
            body.model_dump(exclude={"utterances", "subtitle_stream", "from_job"}))
        if data.track and not data.track.media_path:
            # A track of the episode the studio is open on.
            data.track.media_path = found["path"]
        if data.track and data.track.media_path:
            track_path = Path(data.track.media_path)
            if not track_path.is_file():
                raise HTTPException(422, "that track's media file does not exist")
            data.track.media_key = media.probe(track_path)["media_key"]
        ref_id = sources.reference_id(sid, data)
        document = data.model_dump()
        folder = "evaluation" if "evaluation" in data.roles else "text"
        target = config.work_dir / "studio" / "references" / sid / folder / f"{ref_id}.json"
        rows = None
        if body.utterances is not None:
            rows = body.utterances
            document["text"]["provenance"] = document["text"]["provenance"] or "manual"
        elif body.subtitle_stream is not None and data.track:
            sub = config.work_dir / "studio" / "cache" / f"{ref_id}.subs.ass"
            sub.parent.mkdir(parents=True, exist_ok=True)
            from ..ffmpeg import run_ffmpeg

            run_ffmpeg(["-y", "-v", "error", "-i", data.track.media_path,
                        "-map", f"0:s:{body.subtitle_stream}", str(sub)])
            rows = sources.from_subtitles(sub, prefix=f"s{body.subtitle_stream}")
            document["text"]["provenance"] = f"subtitle:{body.subtitle_stream}"
        elif body.from_job:
            job_id = found.get("job_id")
            if not job_id:
                raise HTTPException(409, "no run is linked to this studio yet")
            _job, data_snapshot = snapshot(job_id)
            rows = [{"utt_id": (r.get("cue") or {}).get("cue_id") or f"cue-{r['index']}",
                     "start": s0, "end": s1, "text": r.get("text_src") or "",
                     "speaker": r.get("speaker") or ""}
                    for r in data_snapshot["segments"]
                    for s0, s1 in [auditions._span(r)] if s1 > s0]
            document["text"]["provenance"] = f"run:{job_id}"
        if rows is not None:
            try:
                document["text"].update(sources.write_utterances(target, rows))
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
        try:
            saved = records.put(db, "reference", ref_id, {**document, "session": sid},
                                scope=sid, create_only=True)
        except StudioConflict as exc:
            conflict(exc)
        return {"reference": _public_reference(saved)}

    @api.patch("/references/{rid}")
    def patch_reference(rid: str, body: ReferencePatch):
        current = need("reference", rid)
        merged = {**current}
        for key in ("roles", "label", "notes"):
            value = getattr(body, key)
            if value is not None:
                merged[key] = value
        if body.sample is not None:
            merged["sample"] = body.sample.model_dump()
        try:
            sources.ReferenceIn.model_validate({k: merged.get(k) for k in (
                "label", "language", "edition", "roles", "track", "text", "sample", "notes")})
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        if set(merged.get("roles") or []) != set(current.get("roles") or []):
            used = [e["id"] for e in records.list_latest(db, "experiment", current["scope"])
                    if rid in ([e.get("meaning"), e.get("adaptation")]
                               + list(e.get("evaluation") or []))]
            if used:
                raise HTTPException(409, "this reference is frozen into experiment(s) "
                                         f"{', '.join(used)}; its roles cannot change under "
                                         "them. Add a new reference instead.")
        try:
            return {"reference": _public_reference(records.put(
                db, "reference", rid, merged, base_revision=body.base_revision))}
        except (StudioConflict, FrozenRecord) as exc:
            conflict(exc)

    @api.post("/references/{rid}/transcribe")
    def transcribe_reference(rid: str, body: TranscribeIn):
        ref = need("reference", rid)
        for start, end in body.windows:
            if end <= start or end - start > media.MAX_WINDOW:
                raise HTTPException(422, "each window must be under "
                                         f"{media.MAX_WINDOW:.0f}s")
        return {"job": queue(f"Transcribe {ref['label']}", "studio_transcribe",
                             {"reference": rid, "session": ref["scope"],
                              "windows": [list(w) for w in body.windows],
                              "engine": body.engine}, source_lang=ref["language"])}

    @api.get("/references/{rid}/utterances")
    def reference_utterances(rid: str):
        ref = need("reference", rid)
        if sources.is_evaluation(ref):
            raise HTTPException(403, "an evaluation-only reference is shown only through an "
                                     "evaluation session, after its reveal is recorded")
        return {"utterances": sources.read_utterances(ref)}

    # --------------------------------------------------------------- alignment
    @api.post("/sessions/{sid}/alignments")
    def create_alignment(sid: str, body: AlignmentIn):
        need_session(sid)
        src, ref = need("reference", body.source), need("reference", body.reference)
        time_map = alignment.TimeMap.from_dict(body.time_map) if body.time_map else None
        estimated = None
        if body.estimate:
            estimated = _estimate(src, ref)
            time_map = alignment.TimeMap.from_dict(estimated)
        try:
            aligned = alignment.align(sources.read_utterances(src),
                                      sources.read_utterances(ref), time_map)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        aid = "al-" + records.digest_id(sid, body.source, body.reference)
        document = {**aligned, "source_ref": src["id"], "reference": ref["id"],
                    "estimate": estimated and {k: estimated[k] for k in (
                        "points", "jumps", "unmapped_windows")}}
        saved = records.put(db, "alignment", aid, document, scope=sid)
        return {"alignment": _alignment_view(saved)}

    def _estimate(src: dict, ref: dict) -> dict:
        a, b = src.get("track") or {}, ref.get("track") or {}
        if not a.get("media_path") or not b.get("media_path"):
            raise HTTPException(422, "estimating a time map needs both references' audio")
        env_a = media.envelope(Path(a["media_path"]), config.work_dir,
                               stream=a.get("audio_index"))
        env_b = media.envelope(Path(b["media_path"]), config.work_dir,
                               stream=b.get("audio_index"))
        return media.estimate_time_map(env_a, env_b)

    @api.get("/alignments/{aid}")
    def get_alignment(aid: str):
        return {"alignment": _alignment_view(need("alignment", aid))}

    @api.post("/alignments/{aid}/overrides")
    def override_alignment(aid: str, body: OverrideIn):
        current = need("alignment", aid)
        if body.base_revision != current["revision"]:
            raise HTTPException(409, {"error": "this alignment changed; reload it",
                                      "current": _alignment_view(current)})
        src = sources.read_utterances(need("reference", current["source_ref"]))
        ref = sources.read_utterances(need("reference", current["reference"]))
        try:
            if body.action == "remap":
                time_map = alignment.TimeMap.from_dict(body.time_map or {})
                updated = {**alignment.align(src, ref, time_map),
                           "overrides": list(current.get("overrides") or [])
                           + [{"action": "remap", "actor": body.actor}]}
            else:
                updated = alignment.apply_override(current, body.model_dump(
                    exclude={"base_revision"}), src, ref)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        saved = records.put(db, "alignment", aid, {**current, **updated},
                            base_revision=body.base_revision)
        return {"alignment": _alignment_view(saved)}

    def _alignment_view(body: dict) -> dict:
        src = records.get(db, "reference", body.get("source_ref") or "")
        ref = records.get(db, "reference", body.get("reference") or "")
        if src is None or ref is None:
            return _alignment_summary(body)
        evaluation_only = sources.is_evaluation(ref)
        s_rows = {u["utt_id"]: u for u in sources.read_utterances(src)}
        r_rows = ({} if evaluation_only else
                  {u["utt_id"]: u for u in sources.read_utterances(ref)})
        groups = []
        for g in body.get("groups", []):
            s_text = " ".join(s_rows[i]["text"] for i in g["source"] if i in s_rows)
            r_text = " ".join(r_rows[i]["text"] for i in g["reference"] if i in r_rows)
            groups.append({**g, "source_text": s_text,
                           "reference_text": "(evaluation-only: hidden)" if evaluation_only
                           else r_text,
                           "differences": ([] if evaluation_only
                                           else alignment.differences(s_text, r_text))})
        return {**_alignment_summary(body), "groups": groups,
                "time_map": body.get("time_map"), "estimate": body.get("estimate"),
                "overrides": body.get("overrides") or []}

    # ------------------------------------------------------------- experiments
    @api.post("/sessions/{sid}/experiments")
    def create_experiment(sid: str, body: experiments.ExperimentIn):
        need_session(sid)
        try:
            saved = experiments.freeze_definition(db, sid, body)
        except StudioConflict as exc:
            conflict(exc)
        except experiments.ExperimentError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"experiment": _experiment_summary(saved)}

    @api.get("/experiments/{eid}")
    def get_experiment(eid: str):
        exp = need("experiment", eid)
        variants = [v for v in records.list_latest(db, "variant", exp["scope"])
                    if v.get("experiment") == eid]
        evals = [e for e in records.list_latest(db, "evaluation", exp["scope"])
                 if e.get("experiment") == eid]
        return {"experiment": {**_experiment_summary(exp), **{k: exp.get(k) for k in (
                    "excerpts", "decision_criteria", "shared", "policy", "limitations",
                    "prompt_revision", "max_requests", "memory", "meaning", "adaptation",
                    "evaluation", "alignment", "alignment_revision")}},
                "variants": [_variant_view(v) for v in variants],
                "evaluations": [{"id": e["id"], "name": e.get("name"),
                                 "revealed": e.get("revealed")} for e in evals]}

    @api.post("/experiments/{eid}/run")
    def run_experiment(eid: str, body: RunIn):
        exp = need("experiment", eid)
        return {"job": queue(f"Experiment {exp['name']}", "studio_experiment",
                             {"experiment": eid, "session": exp["scope"],
                              "conditions": body.conditions, "repeats": body.repeats},
                             target_lang=base_language(exp["target_locale"]))}

    @api.post("/experiments/{eid}/variants/{vid}/render")
    def render_variant(eid: str, vid: str, body: RenderVariantIn):
        """Speak a frozen script through a finished run's cast and processing."""
        need("experiment", eid)
        variant = need("variant", vid)
        if not variant.get("frozen"):
            raise HTTPException(409, "only a finished, frozen variant can be rendered")
        original, data = snapshot(body.job_id)
        by_cue = {(r.get("cue") or {}).get("cue_id"): r for r in data["segments"]}
        edits = copy.deepcopy(config.with_overrides(original.overrides or {})["dub"].get(
            "line_edits", {}))
        count = 0
        for unit in (variant.get("outputs") or {}).values():
            for line in unit["lines"]:
                row = by_cue.get(line["slot_id"])
                if row is None:
                    raise HTTPException(409, "this variant was written for different cues "
                                             "than that run has; render it on the run whose "
                                             "cues were the meaning source")
                edit = edits.setdefault(str(row["index"]), {})
                edit.update({"cue": line["slot_id"], "text": line["text"],
                             "start": row["start"], "end": row["end"],
                             "delivery": row.get("delivery", ""),
                             "revision": row.get("revision", 0)})
                count += 1
        overrides = copy.deepcopy(original.overrides or {})
        overrides["dub.line_edits"] = edits
        overrides["dub.dry_run"] = False
        job = store.add(title=f"{original.title} · {vid}", source="studio",
                        source_lang=original.source_lang, target_lang=original.target_lang,
                        target_locale=original.target_locale, input_file=original.input_file,
                        knowledge_snapshot=original.knowledge_snapshot, kind=original.kind,
                        overrides=overrides,
                        version_name=f"experiment {variant['condition']}")
        bus.publish("job", {"type": "queued", "job_id": job.id, "title": job.title})
        return {"job": {"id": job.id}, "lines": count,
                "note": "Same run, same cast and processing; only these lines get new speech. "
                        "A different script necessarily produces different takes."}

    @api.post("/experiments/{eid}/evaluations")
    def create_evaluation(eid: str, body: dict | None = None):
        exp = need("experiment", eid)
        try:
            found = evaluation.create(db, exp, str((body or {}).get("name") or ""))
        except StudioConflict as exc:
            found = exc.current or {}
        except evaluation.EvaluationError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"evaluation": evaluation.view(db, need("evaluation", found["id"]))}

    @api.get("/evaluations/{vid}")
    def get_evaluation(vid: str):
        found = need("evaluation", vid)
        return {"evaluation": evaluation.view(db, found),
                "summary": evaluation.summarize(db, found)}

    @api.post("/evaluations/{vid}/judgments")
    def judge(vid: str, body: evaluation.JudgmentIn, base_revision: int):
        found = need("evaluation", vid)
        try:
            saved = evaluation.judge(db, found, body, base_revision)
        except (StudioConflict, FrozenRecord) as exc:
            conflict(exc)
        except evaluation.EvaluationError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"evaluation": evaluation.view(db, saved)}

    @api.post("/evaluations/{vid}/reveal/{what}")
    def reveal(vid: str, what: str, base_revision: int, actor: str = ""):
        found = need("evaluation", vid)
        try:
            saved = evaluation.reveal(db, found, what, actor, base_revision)
        except (StudioConflict, FrozenRecord) as exc:
            conflict(exc)
        except evaluation.EvaluationError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"evaluation": evaluation.view(db, saved)}

    @api.get("/evaluations/{vid}/holdout")
    def holdout(vid: str):
        found = need("evaluation", vid)
        exp = need("experiment", found["experiment"])
        bodies = {}
        for ref_id in exp.get("evaluation") or []:
            body = next((a for a in records.list_latest(db, "alignment", exp["scope"])
                         if a.get("reference") == ref_id), None)
            if body:
                bodies[ref_id] = body
        try:
            return {"holdout": evaluation.holdout_lines(db, found, exp, bodies)}
        except evaluation.EvaluationError as exc:
            raise HTTPException(409, str(exc)) from exc

    @api.post("/evaluations/{vid}/revisions")
    def revise(vid: str, body: evaluation.RevisionIn, base_revision: int):
        found = need("evaluation", vid)
        try:
            saved = evaluation.revise(db, found, body, base_revision)
        except (StudioConflict, FrozenRecord) as exc:
            conflict(exc)
        except evaluation.EvaluationError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"evaluation": evaluation.view(db, saved)}

    @api.get("/evaluations/{vid}/export")
    def export_evaluation(vid: str):
        found = need("evaluation", vid)
        return evaluation.export(db, found, need("experiment", found["experiment"]))

    # ---------------------------------------------------------------- auditions
    @api.post("/sessions/{sid}/auditions")
    def create_audition(sid: str, body: auditions.AuditionIn):
        need_session(sid)
        _job, data = snapshot(body.job_id)
        try:
            plan = auditions.plan(data, body)
        except auditions.AuditionError as exc:
            raise HTTPException(422, str(exc)) from exc
        for candidate in body.candidates:
            if candidate.reference:
                ref = records.get(db, "reference", candidate.reference)
                if ref is None or sources.is_evaluation(ref) or "voice" not in ref.get(
                        "roles", []):
                    raise HTTPException(422, f"{candidate.name}: a clone reference must be a "
                                             "voice reference, never an evaluation-only track")
        aid = "aud-" + records.digest_id(sid, body.character, body.job_id,
                                         [c.model_dump() for c in body.candidates])
        document = {**body.model_dump(), "plan": plan, "status": "planned", "takes": {},
                    "candidates": [c.model_dump() for c in body.candidates],
                    "capabilities": {c.engine or "(default)": auditions.capabilities(
                         c.engine, build_speech_client(config), presets_lookup=False)
                                     for c in body.candidates}}
        existing = records.get(db, "audition", aid)
        saved = existing or records.put(db, "audition", aid, document, scope=sid)
        return {"audition": _audition_view(saved)}

    @api.post("/auditions/{aid}/run")
    def run_audition(aid: str):
        found = need("audition", aid)
        return {"job": queue(f"Audition {found['character']}", "studio_audition",
                             {"audition": aid, "session": found["scope"]})}

    @api.get("/auditions/{aid}")
    def get_audition(aid: str):
        return {"audition": _audition_view(need("audition", aid))}

    @api.get("/auditions/{aid}/takes/{candidate}/{cue}")
    def audition_take(aid: str, candidate: str, cue: str, request: Request):
        found = need("audition", aid)
        take = ((found.get("takes") or {}).get(candidate) or {}).get(cue) or {}
        path = allowed_path(config, take.get("path") or "")
        if path is None or not path.is_file():
            raise NotFoundError("that take is not on disk")
        return ranged_response(path, request.headers.get("range"))

    @api.post("/auditions/{aid}/select")
    def select_candidate(aid: str, body: SelectIn):
        found = need("audition", aid)
        sess = need_session(found["scope"])
        results = {r["name"]: r for r in found.get("results") or []}
        candidate = next(({**c, **results.get(c["name"], {})}
                          for c in found.get("candidates") or []
                          if c["name"] == body.candidate), None)
        if candidate is None:
            raise HTTPException(404, "no such candidate in this audition")
        profile = candidate.get("profile") or candidate.get("voice") or ""
        if candidate["kind"] == "clone_line":
            raise HTTPException(422, "per-line cloning is experimental and has no single "
                                     "voice to cast; choose a character-level candidate")
        if candidate["kind"] == "current":
            raise HTTPException(422, "the current voice is already cast")
        if not profile:
            raise HTTPException(409, "this candidate has no generated voice yet; run the "
                                     "audition first")
        scope_ref = sess.get("series_ref") if body.scope == "series" else sess["title_ref"]
        if not scope_ref:
            raise HTTPException(422, "this studio is not linked to a series; choose episode "
                                     "scope")
        saved = casting.decide(db, casting.ChoiceIn(
            character=found["character"], scope=body.scope, scope_ref=scope_ref,
            voice=profile, engine=candidate.get("engine") or "",
            direction=candidate.get("direction") or "", reference=candidate.get(
                "reference") or "", audition=aid, candidate=candidate["name"],
            actor=body.actor, pitch_semitones=float(candidate.get("pitch_semitones") or 0),
            formant_semitones=float(candidate.get("formant_semitones") or 0)))
        result: dict[str, Any] = {"casting": _casting_view(saved)}
        if body.apply:
            result["rerender"] = _apply_cast(sess, found["job_id"], found["character"],
                                             profile, candidate)
        return result

    def _apply_cast(sess: dict, job_id: str, character: str, profile: str,
                    candidate: dict) -> dict:
        """Write the run's voice cast and queue only this character's lines."""
        original, data = snapshot(job_id)
        key = cast_key(path=str(original.input_file))
        existing = (db.load_cast(key) or {}).get("cast") or []
        chosen = casting.effective(db, series_ref=sess.get("series_ref") or "",
                                   episode_ref=sess["title_ref"],
                                   speakers=sorted({r["speaker"] for r in data["segments"]}))
        db.save_cast(key, Path(original.input_file).stem,
                     casting.to_voice_cast(existing, chosen))
        edits = copy.deepcopy(config.with_overrides(original.overrides or {})["dub"].get(
            "line_edits", {}))
        lines = [r for r in data["segments"] if r.get("speaker") == character]
        for row in lines:
            edit = edits.setdefault(str(row["index"]), {})
            for k, v in {"text": row.get("text_translated") or row["text_src"],
                         "start": row["start"], "end": row["end"],
                         "delivery": row.get("delivery", ""),
                         "revision": row.get("revision", 0)}.items():
                edit.setdefault(k, v)
            edit["cue"] = (row.get("cue") or {}).get("cue_id")
            edit["voice"] = profile
        overrides = copy.deepcopy(original.overrides or {})
        overrides["dub.line_edits"] = edits
        overrides["dub.dry_run"] = False
        job = store.add(title=original.title, source=original.source,
                        source_lang=original.source_lang, target_lang=original.target_lang,
                        target_locale=original.target_locale, input_file=original.input_file,
                        knowledge_snapshot=original.knowledge_snapshot, kind=original.kind,
                        overrides=overrides)
        bus.publish("job", {"type": "queued", "job_id": job.id, "title": job.title})
        return {"job": job.id, "generating": len(lines),
                "note": f"Only {character}'s {len(lines)} line(s) get new speech; every "
                        "other line reuses its existing audio."}

    # ----------------------------------------------------------------- casting
    @api.get("/sessions/{sid}/casting")
    def get_casting(sid: str):
        sess = need_session(sid)
        speakers: list[str] = []
        if active_job_id(sess):
            try:
                _job, data = snapshot(active_job_id(sess))
                speakers = sorted({r["speaker"] for r in data["segments"]})
            except HTTPException:
                speakers = []
        return casting.effective(db, series_ref=sess.get("series_ref") or "",
                                 episode_ref=sess["title_ref"], speakers=speakers)

    @api.post("/sessions/{sid}/casting")
    def set_casting(sid: str, body: casting.ChoiceIn, base_revision: int | None = None):
        need_session(sid)
        try:
            return {"casting": _casting_view(casting.decide(db, body, base_revision))}
        except (StudioConflict, FrozenRecord) as exc:
            conflict(exc)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @api.get("/sessions/{sid}/casting/affected")
    def affected(sid: str, character: str):
        sess = need_session(sid)
        if not sess.get("series_ref"):
            return {"follow": [], "keep": [], "note": "not linked to a series"}
        episodes = sorted({s["title_ref"] for s in records.list_latest(db, "session")
                           if s.get("series_ref") == sess["series_ref"]})
        return casting.affected(db, series_ref=sess["series_ref"], character=character,
                                episodes=episodes)

    # ------------------------------------------------------------- annotations
    @api.get("/sessions/{sid}/notes")
    def list_notes(sid: str, job_id: str = ""):
        need_session(sid)
        notes = [n for n in records.list_latest(db, "annotation", sid)
                 if not job_id or n.get("job_id") == job_id]
        current = None
        if job_id:
            try:
                _job, data = snapshot(job_id)
                current = data.get("revision")
            except HTTPException:
                current = None
        for n in notes:
            n["stale"] = bool(current and n.get("snapshot_revision")
                              and n["snapshot_revision"] != current)
        return {"notes": notes}

    @api.post("/sessions/{sid}/notes")
    def add_note(sid: str, body: NoteIn):
        need_session(sid)
        nid = "note-" + records.digest_id(sid, body.model_dump(), records.now_marker())
        saved = records.put(db, "annotation", nid, {**body.model_dump(),
                                                    "resolution": "open", "history": []},
                            scope=sid, create_only=True)
        return {"note": saved}

    @api.patch("/notes/{nid}")
    def patch_note(nid: str, body: NotePatch):
        current = need("annotation", nid)
        changes = body.model_dump(exclude_none=True, exclude={"base_revision"})
        history = list(current.get("history") or []) + [{"at": records.now_marker(),
                                                         **changes}]
        try:
            return {"note": records.put(db, "annotation", nid,
                                        {**current, **changes, "history": history[-50:]},
                                        base_revision=body.base_revision)}
        except (StudioConflict, FrozenRecord) as exc:
            conflict(exc)

    # ------------------------------------------------------------------- media
    @api.get("/sessions/{sid}/media/audio")
    def media_audio(sid: str, request: Request, source: str, start: float, end: float,
                    job_id: str = ""):
        path = _window(sid, source, start, end, job_id)["path"]
        return ranged_response(path, request.headers.get("range"))

    @api.get("/sessions/{sid}/media/window")
    def media_window(sid: str, source: str, start: float, end: float, job_id: str = ""):
        """What playing `source` over this window means: mapping, confidence, peaks."""
        from ..levels import analyze

        found = _window(sid, source, start, end, job_id)
        # The level is measured so a listener can choose a level-matched
        # preview; the render is never changed by it.
        return {k: v for k, v in found.items() if k != "path"} | {
            "peaks": media.peaks(found["path"]),
            "level_db": analyze(found["path"]).get("speech_db"),
            "duration": round(media.duration(found["path"]), 3)}

    @api.get("/sessions/{sid}/media/video")
    def media_video(sid: str, request: Request, start: float, end: float):
        sess = need_session(sid)
        try:
            clip = media.cut_video(Path(sess["path"]), start, end, config.work_dir)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return ranged_response(clip, request.headers.get("range"))

    def _window(sid: str, source: str, start: float, end: float, job_id: str) -> dict:
        sess = need_session(sid)
        if end <= start or end - start > media.MAX_WINDOW:
            raise HTTPException(422, f"a window must be under {media.MAX_WINDOW:.0f}s")
        mapped = {"source": source, "start": start, "end": end, "state": "exact",
                  "note": ""}
        if source == "original":
            track = _meaning_track(sid)
            if track:
                path = media.cut_audio(Path(track["media_path"]), start, end, config.work_dir,
                                       stream=track.get("audio_index"))
                mapped["note"] = "the original performance (the meaning reference's track)"
            elif job_id:
                # No meaning reference yet: the audio this run was made from,
                # named as that — which may itself be a dub, not the original.
                _job, data = snapshot(job_id)
                local = allowed_path(config, str((data.get("media") or {}).get("source_track")
                                                 or (data.get("media") or {}).get("source_audio")
                                                 or ""))
                if local is None or not local.is_file():
                    raise HTTPException(409, "this run's source audio is not on disk; assign a "
                                             "meaning reference to hear the original")
                path = media.cut_audio(local, start, end, config.work_dir)
                mapped.update(state="run_source", note=(
                    f"the audio this run was made from ({data.get('source_language') or '?'}); "
                    "assign a meaning reference to choose the original track"))
            else:
                path = media.cut_audio(Path(sess["path"]), start, end, config.work_dir)
                mapped.update(state="first_track", note=(
                    "the file's first audio track — not necessarily the original; assign a "
                    "meaning reference"))
        elif source.startswith("output:"):
            job, _data = snapshot(job_id)
            local = allowed_path(config, job.output_file or "")
            if local is None or not local.is_file():
                raise HTTPException(409, "this run's output file is not on disk")
            index = int(source.split(":", 1)[1])
            path = media.cut_audio(local, start, end, config.work_dir, stream=index)
            mapped["note"] = f"audio track {index} of the delivered file (actual levels)"
        elif source == "dub":
            if not job_id:
                raise HTTPException(422, "choose a run to hear its dub")
            job, data = snapshot(job_id)
            rendered = str((data.get("media") or {}).get("dubbed_track")
                           or (data.get("media") or {}).get("output") or "")
            local = allowed_path(config, rendered)
            if local is None or not local.is_file():
                raise HTTPException(409, "this run's dubbed track is not on disk")
            path = media.cut_audio(local, start, end, config.work_dir)
            mapped["note"] = "the dubbed mix as rendered (actual levels)"
        elif source.startswith("reference:"):
            ref = need("reference", source.split(":", 1)[1])
            if sources.is_evaluation(ref):
                raise HTTPException(403, "an evaluation-only track is played from its "
                                         "evaluation session after the reveal")
            track = ref.get("track") or {}
            if not track.get("media_path"):
                raise HTTPException(409, "this reference has no audio track")
            body = next((a for a in records.list_latest(db, "alignment", sid)
                         if a.get("reference") == ref["id"]), None)
            time_map = alignment.TimeMap.from_dict((body or {}).get("time_map"))
            placed = time_map.to_reference(start, end)
            if placed is None:
                raise HTTPException(409, "this moment falls in a stretch the reference "
                                         "does not have (a cut), so there is nothing to play")
            r0, r1, seg = placed
            path = media.cut_audio(Path(track["media_path"]), max(0.0, r0), r1,
                                   config.work_dir, stream=track.get("audio_index"))
            mapped.update(mapped_start=round(r0, 3), mapped_end=round(r1, 3),
                          confidence=seg.confidence, method=seg.method,
                          state=("unmapped" if body is None else
                                 "uncertain" if (seg.confidence or 0) < 0.5
                                 and seg.method != "manual" else "mapped"),
                          note=("no alignment yet: played at the same timestamps, which "
                                "is only right if both releases share a clock"
                                if body is None else "mapped through the alignment's "
                                "time map"))
        elif source.startswith("version:"):
            job, _data = snapshot(job_id)
            vid = source.split(":", 1)[1]
            output = allowed_path(config, job.output_file or "")
            if output is None:
                raise HTTPException(409, "this run has no output")
            root = output.parent.parent if output.parent.parent.name == "versions" else \
                output.parent / "versions"
            manifest = read_json(root / vid / "version.json")
            local = allowed_path(config, manifest.get("output") or "")
            if local is None or not local.is_file():
                raise HTTPException(409, "that version's media is not on disk")
            path = media.cut_audio(local, start, end, config.work_dir)
            mapped["note"] = f"saved version {manifest.get('name') or vid}"
        else:
            raise HTTPException(422, "unknown source")
        return {**mapped, "path": path}

    def _meaning_track(sid: str) -> dict:
        for ref in records.list_latest(db, "reference", sid):
            if "meaning" in (ref.get("roles") or []) and (ref.get("track") or {}).get(
                    "media_path"):
                return ref["track"]
        return {}

    # ----------------------------------------------------------------- imports
    @api.post("/sessions/{sid}/imports/preview")
    def import_preview(sid: str, body: ImportIn):
        need_session(sid)
        try:
            return {"preview": importer.preview(Path(body.path), [config.work_dir,
                                                                  config.output_dir],
                                                Path.cwd())}
        except importer.ImportError_ as exc:
            raise HTTPException(422, str(exc)) from exc

    @api.post("/sessions/{sid}/imports")
    def import_apply(sid: str, body: ImportIn):
        need_session(sid)
        try:
            found = importer.preview(Path(body.path), [config.work_dir, config.output_dir],
                                     Path.cwd())
            saved = importer.apply(db, sid, found, body.mapping or {}, body.actor)
        except importer.ImportError_ as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"import": _import_summary(saved), "repeated": bool(saved.get("repeated"))}

    @api.post("/imports/{iid}/results")
    def import_results(iid: str, body: ResultsIn):
        found = need("import", iid)
        try:
            parsed = importer.parse_results(body.text)
            saved = importer.attach_results(db, found, parsed, Path(body.name).name)
        except importer.ImportError_ as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"import": _import_summary(saved)}

    @api.get("/imports/{iid}")
    def get_import(iid: str):
        found = need("import", iid)
        return {"import": {**_import_summary(found), **{k: found.get(k) for k in (
            "scenes", "labels", "tracks", "approaches", "references", "mapping",
            "experimental_settings", "settings_note", "judgments_imported", "honesty",
            "judgments", "generates", "media", "profiles_note")}}}

    @api.get("/imports/{iid}/media")
    def import_media(iid: str, request: Request, scene: int, kind: str, name: str = ""):
        """A legacy comparison's own files, played where they are."""
        found = need("import", iid)
        manifest = read_json(Path(found["manifest"]))
        row = next((s for s in manifest.get("scenes", []) if s["scene"]["index"] == scene),
                   None)
        if row is None:
            raise NotFoundError("no such scene in that import")
        if kind == "source":
            value = row.get("source_excerpt")
        elif kind == "reference":
            value = next((r["path"] for r in row.get("references", [])
                          if r.get("language") == name), None)
        elif kind in ("mixed", "matched"):
            value = next((v.get(kind) for v in row.get("variants", []) if v["name"] == name),
                         None)
        else:
            raise HTTPException(422, "unknown legacy media kind")
        local = importer._resolve(value, Path(found["manifest"]).parent, Path.cwd())
        path = allowed_path(config, str(local)) if local else None
        if path is None or not path.is_file():
            raise NotFoundError("that legacy file is missing")
        return ranged_response(path, request.headers.get("range"))

    # ------------------------------------------------------------------ export
    @api.get("/sessions/{sid}/export")
    def export_plan(sid: str, job_id: str):
        need_session(sid)
        job, data = snapshot(job_id)
        plan = session.export_plan(data, load_decisions(config.work_dir, job_id))
        return {**plan, "revision": data.get("revision"), "version_id": job.version_id,
                "version_saved": bool(job.version_id), "job": job_id}

    @api.post("/sessions/{sid}/export")
    def export(sid: str, job_id: str, body: ExportIn):
        """Export the selection: reuse the saved version, or re-mix chosen takes only."""
        sess = need_session(sid)
        original, data = snapshot(job_id)
        if body.base_revision and body.base_revision != data.get("revision"):
            raise HTTPException(409, "this run changed since you looked at it; reload")
        plan = session.export_plan(data, load_decisions(config.work_dir, job_id))
        if plan["unresolved"] and sess.get("unresolved") == "block_export":
            raise HTTPException(409, f"{len(plan['unresolved'])} unresolved finding(s) block "
                                     "export under this studio's policy")
        if not body.takes:
            if not original.version_id:
                raise HTTPException(409, "this run has no saved version to export")
            return {"exported": "existing", "version_id": original.version_id,
                    "generated": 0, "unresolved": len(plan["unresolved"]),
                    "note": "The saved version already holds the selected takes; nothing "
                            "was rendered."}
        rows = {(r.get("cue") or {}).get("cue_id"): r for r in data["segments"]}
        edits = copy.deepcopy(config.with_overrides(original.overrides or {})["dub"].get(
            "line_edits", {}))
        for cue_id, take_id in body.takes.items():
            row = rows.get(cue_id)
            takes = {t.get("take_id") for t in ((row or {}).get("cue") or {}).get(
                "audio", {}).get("takes", [])}
            if row is None or take_id not in takes:
                raise HTTPException(409, "that take is not on that line any more")
            edit = edits.setdefault(str(row["index"]), {})
            for k, v in {"text": row.get("text_translated") or row["text_src"],
                         "start": row["start"], "end": row["end"],
                         "delivery": row.get("delivery", ""),
                         "revision": row.get("revision", 0)}.items():
                edit.setdefault(k, v)
            edit["cue"] = cue_id
            edit["take"] = take_id
        regenerating = [k for k, e in edits.items()
                        if str(k) in {str(r["index"]) for r in data["segments"]}
                        and _would_generate(e, rows)]
        if regenerating and not body.allow_generation:
            raise HTTPException(409, {"error": "this export would generate new speech for "
                                               f"{len(regenerating)} line(s); confirm to "
                                               "allow it", "lines": regenerating})
        overrides = copy.deepcopy(original.overrides or {})
        overrides["dub.line_edits"] = edits
        overrides["dub.dry_run"] = False
        job = store.add(title=original.title, source=original.source,
                        source_lang=original.source_lang, target_lang=original.target_lang,
                        target_locale=original.target_locale, input_file=original.input_file,
                        knowledge_snapshot=original.knowledge_snapshot, kind=original.kind,
                        overrides=overrides, version_name="studio export")
        bus.publish("job", {"type": "queued", "job_id": job.id, "title": job.title})
        return {"exported": "queued", "job": job.id, "generated": len(regenerating),
                "note": "Chosen takes are re-mixed from audio that already exists. The "
                        "original media and earlier versions are not touched."}

    def _would_generate(edit: dict, rows: dict) -> bool:
        row = rows.get(edit.get("cue"))
        if row is None:
            return False
        text = row.get("text_translated") or row.get("text_src")
        return (("text" in edit and edit["text"] != text)
                or ("voice" in edit and edit["voice"] != row.get("voice"))
                or ("delivery" in edit and edit["delivery"] != (row.get("delivery") or ""))
                or int(edit.get("revision", 0)) > int(row.get("revision", 0)))

    # ----------------------------------------------------------------- views
    def _public_session(found: dict) -> dict:
        return {k: v for k, v in found.items() if k != "path"} | {
            "media_name": Path(found["path"]).name}

    def _public_reference(ref: dict) -> dict:
        row = copy.deepcopy(ref)
        track = row.get("track") or {}
        if track.get("media_path"):
            track["media_name"] = Path(track.pop("media_path")).name
        text = row.get("text") or {}
        text.pop("artifact", None)
        row["evaluation_only"] = sources.is_evaluation(ref)
        return row

    def _alignment_summary(body: dict) -> dict:
        return {"id": body["id"], "revision": body["revision"],
                "source_ref": body.get("source_ref"), "reference": body.get("reference"),
                "method": body.get("method"), "stats": body.get("stats"),
                "segments": len((body.get("time_map") or {}).get("segments") or [])}

    def _experiment_summary(exp: dict) -> dict:
        return {"id": exp["id"], "revision": exp["revision"], "name": exp.get("name"),
                "conditions": exp.get("conditions"), "target_locale": exp.get("target_locale"),
                "policy": exp.get("policy"), "frozen": exp.get("frozen"),
                "excerpts": len(exp.get("excerpts") or []), "provider": exp.get("provider"),
                "model": exp.get("model")}

    def _variant_view(variant: dict) -> dict:
        return {k: variant.get(k) for k in (
            "id", "revision", "condition", "repeat", "status", "error", "frozen", "outputs",
            "missing_input", "usage", "memory_rejected", "manifest")} | {
            "guard_audit": len(variant.get("guard_audit") or [])}

    def _audition_view(found: dict) -> dict:
        view = {k: found.get(k) for k in (
            "id", "revision", "character", "job_id", "plan", "status", "error", "usage",
            "retakes", "check_words", "max_requests", "categories")}
        # The candidates as they were asked for, with what the last run made of each.
        results = {r["name"]: r for r in found.get("results") or []}
        view["candidates"] = [{**c, **{k: v for k, v in results.get(c["name"], {}).items()
                                       if k in ("status", "reason", "capability",
                                                "reference_findings", "profile")}}
                              for c in found.get("candidates") or []]
        takes = {}
        for name, rows in (found.get("takes") or {}).items():
            takes[name] = {cue: {k: v for k, v in row.items()
                                 if k not in ("path", "history", "request")}
                           | {"available": bool(row.get("path")
                                                and Path(row["path"]).is_file())}
                           for cue, row in rows.items()}
        view["takes"] = takes
        return view

    def _audition_summary(found: dict) -> dict:
        return {"id": found["id"], "character": found.get("character"),
                "status": found.get("status"), "candidates": len(found.get("candidates") or []),
                "excerpts": len((found.get("plan") or {}).get("excerpts") or []),
                "missing": (found.get("plan") or {}).get("missing") or []}

    def _import_summary(found: dict) -> dict:
        return {"id": found["id"], "kind": found.get("kind"), "legacy_id": found.get("legacy_id"),
                "missing": found.get("missing") or [], "speakers": found.get("speakers"),
                "judgments": len(found.get("judgments_imported") or []),
                "mapping": found.get("mapping")}

    def _casting_view(found: dict) -> dict:
        return {k: found.get(k) for k in ("id", "revision", "scope_kind", "characters",
                                           "lines")}

    return api


def _redact(document: dict | None) -> dict | None:
    if not document:
        return document
    return {k: v for k, v in document.items() if k not in ("path", "manifest")}
