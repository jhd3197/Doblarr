"""An analysed episode: every line, who says it, how, and in which words.

The analysis itself is a queued run (`kind="analyze"`, see doblarr.analysis);
these routes read what it left beside the script, let a person hear any line
and say who speaks.

Who speaks is kept against the episode's *content*, not its file name
(doblarr.identity): a voice group (SPEAKER_03) is an identity only within one
source revision, and naming it links the group to a character of the series.
A copy of the same file elsewhere finds the same names; a different file that
happens to share the name does not. Naming also teaches the series what the
character sounds like (doblarr.voice_tags), so unnamed groups here and in later
episodes are offered the nearest characters, with the margin to the next one.

Regrouping with other voice models keeps every decision a person made: a named
group's character follows most of its speaking time, a line moved by hand is
moved again, and the speaker baselines that relative levels depend on are
recomputed for the new groups (doblarr.speaker_memory).

Older names, kept under the file name, are still read when nothing newer
exists; ``/api/analysis/migration`` previews and applies their move.
"""

from __future__ import annotations

import hashlib
import json
import threading
from difflib import SequenceMatcher
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

from .. import (
    dialogue_clues,
    doubts,
    dub_text,
    identity,
    identity_migration,
    key_terms,
    snapshots,
    speaker_memory,
    speakers,
    speaking,
    subtitle_roles,
    track_alignment,
    voice_models,
    voice_tags,
)
from ..ffmpeg import run_ffmpeg, run_ffmpeg_bytes
from ..fileio import inside, read_json
from ..studio import records

MAX_CLIP = 30.0
CLIP_CACHE = 400          # watched clips kept on disk, newest first
VIDEO_HEIGHT = 540        # clips are for recognising who talks, not for archiving
FRAME_CACHE = 5000        # stills of lines kept on disk (a few KB each)
# Seconds a request may wait on ffmpeg before it answers with an error; a
# stalled network share must not hold a server thread forever.
CLIP_TIMEOUT = 120.0
FRAME_TIMEOUT = 30.0


class NamesIn(BaseModel):
    path: str = Field(min_length=1, max_length=2000)
    names: dict[str, str] = Field(default_factory=dict)
    # Groups whose name a person kept against the dialogue: never renamed by it.
    keep: list[str] = Field(default_factory=list, max_length=200)


class TermIn(BaseModel):
    path: str = Field(min_length=1, max_length=2000)
    source: str = Field(min_length=1, max_length=120)
    # Blank: forget the show's wording for this term.
    rendering: str = Field(default="", max_length=120)
    locale: str = Field(default="", max_length=20)


class LineIn(BaseModel):
    path: str = Field(min_length=1, max_length=2000)
    cue: str = Field(min_length=1, max_length=200)
    # A character from the cast or a new one; blank: a new voice nobody named yet.
    character: str = Field(default="", max_length=80)


class LinesIn(BaseModel):
    """Several lines to one character: splitting a group a person heard as two."""

    path: str = Field(min_length=1, max_length=2000)
    cues: list[str] = Field(min_length=1, max_length=2000)
    character: str = Field(default="", max_length=80)


class UnlockIn(BaseModel):
    path: str = Field(min_length=1, max_length=2000)
    cue: str = Field(min_length=1, max_length=200)


class RegroupIn(BaseModel):
    path: str = Field(min_length=1, max_length=2000)
    models: list[str] = Field(default_factory=list, max_length=4)
    threshold: float | None = Field(default=None, gt=0, lt=2)
    tracks: list[int] | None = Field(default=None, max_length=8)   # None: the setting


class RerunIn(BaseModel):
    path: str = Field(min_length=1, max_length=2000)
    stages: list[str] = Field(default_factory=list, max_length=20)
    visual: bool | None = None
    target_lang: str = Field(default="es", max_length=16)


class TracksIn(BaseModel):
    path: str = Field(min_length=1, max_length=2000)
    base_revision: int | None = Field(default=None, ge=0)
    assign: dict[str, str] = Field(default_factory=dict)   # track -> character name ("" unknown)
    split: dict[str, float] = Field(default_factory=dict)  # track -> time to split at
    merge: list[tuple[str, str]] = Field(default_factory=list, max_length=100)


class ScenesIn(BaseModel):
    path: str = Field(min_length=1, max_length=2000)
    base_revision: int | None = Field(default=None, ge=0)
    add: list[float] = Field(default_factory=list, max_length=200)
    remove: list[float] = Field(default_factory=list, max_length=200)


class MigrationIn(BaseModel):
    fingerprint: str = Field(min_length=8, max_length=64)
    keys: list[str] | None = Field(default=None, max_length=5000)


def names_key(path: str) -> str:
    """Where older releases kept an episode's names: under its file name."""
    return "speaker-names:" + Path(str(path).replace("\\", "/")).name.casefold()


def load_plan(db, path: str) -> dict:
    return ((db.load_plan(names_key(path)) or {}).get("plan") or {})


def _feeling(row: dict | None) -> dict | None:
    """A line's feeling for the page: the fused answer and what each reader said."""
    if not row or not row.get("feeling"):
        return None
    seen, heard = row.get("picture") or {}, row.get("voice") or {}
    return {"feeling": row["feeling"], "confidence": row.get("confidence"),
            "agree": row.get("agree"), "from": row.get("from"),
            "intensity": row.get("intensity"), "expression": seen.get("expression") or "",
            "voice": heard.get("feeling") or ""}


_NOT_TEXT = {"", "null", "none", "n/a", "na", "unknown"}
_QUOTES = "\"'「」『』"
_COMMENTARY = ("note:", "the line", "original japanese", "subtitle")


def _picture_text(text: str, spoken: set[str]) -> str:
    """Text a seeing model said was on the picture, or "" when it is not usable:
    empty answers, its own commentary, a run of one repeated word, or the
    dialogue's subtitles copied back."""
    text = " ".join(str(text or "").split()).strip().strip(_QUOTES)
    key = "".join(ch for ch in text.casefold() if ch.isalnum())
    if key in _NOT_TEXT or len(key) < 2 or len(text) > 60:
        return ""
    if any(word in text.casefold() for word in _COMMENTARY):
        return ""
    if len(key) > 12 and len(set(key)) <= len(key) // 6:
        return ""                                # one word repeated across the frame
    if any(key in said or (len(said) > 6 and said in key) for said in spoken):
        return ""
    return text


def on_screen(felt: dict, lines: list[dict], signs: list[dict]) -> list[dict]:
    """Text drawn on the picture: the subtitle track's signs and title cards
    (translated and timed), then what the picture reader saw that they lack."""
    def fold(text: str) -> str:
        return "".join(ch for ch in text.casefold() if ch.isalnum())

    by_start = sorted(lines, key=lambda line: line["start"])
    found, seen = [], set()
    for sign in signs:
        near = min(by_start, key=lambda line: abs(line["start"] - sign["start"]), default=None)
        seen.add(fold(sign["text"]))
        found.append({"cue": near["cue"] if near else None, "at": round(sign["start"], 2),
                      "text": sign["text"], "kind": sign["kind"], "from": "subtitles"})
    spoken = {fold(line.get("text") or "") for line in lines} - {""}
    at = {line["cue"]: line["start"] for line in lines}
    for cue, row in felt.items():
        text = _picture_text(str((row.get("picture") or {}).get("text_on_screen") or ""), spoken)
        # The same banner read a little differently on each frame is one text.
        if not text or fold(text) in seen or any(
                SequenceMatcher(None, fold(text), other).ratio() > 0.6 for other in seen):
            continue
        seen.add(fold(text))
        found.append({"cue": cue, "at": round(float(at.get(cue) or 0.0), 2), "text": text,
                      "kind": "picture", "from": "picture"})
    return sorted(found, key=lambda row: row["at"])


def identify(db, path: str, cache_dir: Path | None = None) -> dict | None:
    """The canonical identity of an episode path, or None when it cannot be told.

    A reachable file is identified by its content. A file that is not
    reachable here (a NAS offline, a moved copy) is identified only through a
    location an earlier identification recorded.
    """
    file = Path(str(path))
    try:
        if file.is_file():
            return identity.resolve(db, file, cache_dir=cache_dir)
    except OSError:
        pass
    media = identity.find_media_by_path(db, path)
    if media is None:
        return None
    wanted = identity.norm_path(path)
    for revision_id, revision in (media.get("revisions") or {}).items():
        if wanted in [identity.norm_path(p) for p in revision.get("locations") or []]:
            return {"series_id": media.get("series_id"), "media_id": media["id"],
                    "revision_id": revision_id, "content_key": revision.get("content_key"),
                    "kind": media.get("kind"), "season": media.get("season"),
                    "episode": media.get("episode"), "edition": revision.get("edition", ""),
                    "duration": revision.get("duration"), "how": "known-location"}
    return None


def load_names(db, path: str, ident: dict | None = None) -> dict[str, str]:
    """Voice group -> character name for an episode.

    Character links of its source revision win; the older per-file names are
    read only when the revision has none yet.
    """
    ident = ident if ident is not None else identify(db, path)
    if ident:
        linked = identity.cluster_characters(db, ident["revision_id"])
        if linked or identity.associations(db, ident["revision_id"], "cluster"):
            return {label: character["name"] for label, character in linked.items()}
    return dict(load_plan(db, path).get("names") or {})


def label_for(names: dict[str, str], labels: set[str], character: str) -> tuple[str, bool]:
    """The voice group a character's lines go to: theirs, or a new one."""
    if character:
        for label, name in sorted(names.items()):
            if name.casefold() == character.casefold():
                return label, False
    taken = {int(x[8:]) for x in labels | set(names) if x[8:].isdigit()}
    return f"SPEAKER_{max(taken, default=-1) + 1:02d}", True


def sidecar(script: Path, suffix: str) -> Path | None:
    """`<stem>.<suffix>` next to the script or in the media folder above it."""
    stem = script.name.removesuffix(".script.json")
    for folder in (script.parent, *script.parents[:3]):
        found = folder / f"{stem}.{suffix}"
        if found.is_file():
            return found
    return None



def build_router(config, store) -> APIRouter:
    api = APIRouter()
    db = store.db
    cache_dir = Path(config.work_dir) / "cache"

    def who(path: str) -> dict | None:
        return identify(db, path, cache_dir)

    def find(path: str, ident: dict | None) -> tuple[Path | None, str]:
        """The analysis script for a path: the one its revision's snapshot
        names, else one made from exactly this path, else a same-name match."""
        if ident:
            for snap in records.list_latest(db, "snapshot", scope=ident["revision_id"]):
                script = ((snap.get("stages") or {}).get("transcribe") or {}).get(
                    "outputs", {}).get("script")
                if script and Path(script).is_file():
                    return Path(script), "snapshot"
        return speaking.locate_script(config.work_dir, path)

    def locate(path: str, ident: dict | None = None) -> Path:
        script, _how = find(path, ident if ident is not None else who(path))
        if script is None:
            raise HTTPException(404, "This episode has not been analysed yet")
        return script

    def language_of(script: Path) -> str:
        data = read_json(script)
        return str((data.get("identity") or {}).get("source_lang")
                   or read_json(sidecar(script, "analysis.json")).get("source_lang") or "")

    def target_of_script(script: Path, data: dict) -> str:
        """The language a script was translated (or is to be dubbed) into."""
        return str((data.get("translation_options") or {}).get("target_locale")
                   or script.parent.name)

    def show_terms(show_ref: str, locale: str) -> list:
        """The show's own term entries for this locale (the base language too)."""
        from ..knowledge import store as knowledge_store
        from ..languages import base_language

        base = base_language(locale)
        return [e for e in knowledge_store.latest_entries(db)
                if e.kind == "term" and e.scope == "show" and e.scope_ref == show_ref
                and e.status not in ("retired",) and e.source_form
                and e.locale in (locale, base)]

    def show_ref_of(ident: dict | None) -> str:
        return identity.show_ref((ident or {}).get("series_id"))

    def terms_of(script: Path, data: dict, lines: list[dict], ident: dict | None) -> dict:
        """The episode's names and terms: how this dub and the official one
        say each, and the show's saved wording (doblarr.key_terms)."""
        segments = data.get("segments") or []
        texts = [s.get("text_src") or "" for s in segments]
        renders = [s.get("text_translated") or "" for s in segments]
        locale = target_of_script(script, data)
        show = show_ref_of(ident)
        saved = ({key_terms.fold(e.source_form): e for e in show_terms(show, locale)}
                 if show else {})
        official = dub_text.for_script(
            script.parent.parent, script.name.replace(".script.json", ""),
            [(float(s.get("start") or 0), float(s.get("end") or 0)) for s in segments], locale)
        rows = key_terms.episode(texts, renders, dub=official["lines"] if official else None,
                                 decided={e.source_form: e.phrase for e in saved.values()})
        cue = [str(line.get("cue")) for line in lines]

        def cues(positions):
            return [cue[n] for n in positions if n < len(cue)]

        def side(found):
            if not found:
                return None
            return {"rendering": found["example"] or found["rendering"], "share": found["share"],
                    "variants": [{"rendering": v["rendering"], "cues": cues(v["lines"])}
                                 for v in found["variants"]],
                    "missing": cues(found["missing"])}

        out = []
        for row in rows:
            entry = saved.get(key_terms.fold(row["term"]))
            out.append({
                "term": row["term"], "kind": row["kind"], "count": len(row["lines"]),
                "lines": [{"cue": cue[n] if n < len(cue) else None,
                           "start": segments[n].get("start"), "text": texts[n],
                           "ours": renders[n],
                           "dub": official["lines"][n] if official else ""}
                          for n in row["lines"]],
                "ours": side(row["ours"]) if any(renders) else None,
                "dub": side(row.get("dub")),
                "saved": {"id": entry.id, "rendering": entry.phrase,
                          "reviewed": entry.status == "reviewed"} if entry else None,
                "holds": cues(row.get("holds") or []) if entry else [],
            })
        known = {key_terms.fold(r["term"]) for r in out}
        # Saved wording for a term this episode never says still belongs to
        # the show; listed after, so a person sees everything it keeps.
        others = [{"term": e.source_form, "rendering": e.phrase, "id": e.id,
                   "reviewed": e.status == "reviewed"}
                  for k, e in sorted(saved.items()) if k not in known]
        return {"locale": locale, "show": bool(show), "rows": out, "other_saved": others,
                "official": {"title": official["title"]} if official else None}

    def running(path: str) -> dict | None:
        return next((j for j in store.list() if j.get("kind") == "analyze"
                     and j.get("status") in ("queued", "running")
                     and Path(str(j.get("input_file") or "")).name.casefold()
                     == Path(path.replace("\\", "/")).name.casefold()), None)

    def require_identity(path: str) -> dict:
        ident = who(path)
        if ident is None:
            raise HTTPException(409, "This episode's file cannot be read here, so who speaks "
                                "cannot be tied to it; make the file reachable and retry")
        return ident

    def character_names(series_id: str) -> dict[str, str]:
        return {c["id"]: c["name"] for c in identity.characters(db, series_id,
                                                                include_retired=True)}

    def cast_rows(series_id: str | None, names: dict[str, str]) -> list[dict]:
        """Every character of the series, most heard first (for the picker)."""
        if not series_id:
            return voice_tags.cast(db, "", names)
        heard: dict[str, dict[str, int]] = {}
        doc = (db.load_plan(voice_tags.series_key(series_id)) or {}).get("plan") or {}
        for part in (doc.get("partitions") or {}).values():
            for revision, taught in part.items():
                for character, entry in taught.items():
                    per = heard.setdefault(character, {})
                    per[revision] = max(per.get(revision, 0), int(entry.get("n", 0)))
        rows = []
        for character in identity.characters(db, series_id):
            per = heard.get(character["id"], {})
            rows.append({"name": character["name"], "character_id": character["id"],
                         "aliases": character.get("aliases") or [],
                         "lines": sum(per.values()), "episodes": len(per)})
        known = {r["name"].casefold() for r in rows}
        rows += [{"name": n, "character_id": None, "aliases": [], "lines": 0, "episodes": 0}
                 for n in sorted(set(names.values())) if n.casefold() not in known]
        return sorted(rows, key=lambda r: (-r["lines"], r["name"].casefold()))

    def teach(ident: dict, script: Path) -> dict[str, int]:
        return speaker_memory.teach(db, ident, sidecar(script, "speakers.json"),
                                    language_of(script))

    def membership_changed(ident: dict, script: Path, reason: str) -> dict:
        """Who is in each group changed: baselines, memory and snapshot follow."""
        refreshed = speaker_memory.refresh_baselines(script)
        language = language_of(script)
        data = read_json(script)
        labels = [[(s.get("cue") or {}).get("cue_id"), s.get("speaker")]
                  for s in data.get("segments") or []]
        snapshots.record_stage(db, ident["revision_id"], language, "diarize", "done",
                               inputs=labels, version=speakers.DETECTOR,
                               metrics={"reason": reason}, identity=ident)
        snapshots.record_stage(db, ident["revision_id"], language, "baselines", "done",
                               inputs=[refreshed["baseline"], labels],
                               metrics={"changed": refreshed["changed"],
                                        "overlap_changes": refreshed["overlaps"]},
                               identity=ident)
        taught = teach(ident, script)
        snapshots.record_stage(db, ident["revision_id"], language, "speaker_memory", "done",
                               inputs=[labels, sorted(taught.items())], identity=ident)
        return {**refreshed, "taught": taught}

    @api.get("/api/voice-models")
    def list_voice_models():
        return {"models": voice_models.describe(config),
                "folder": str(voice_models.folder(config))}

    def named_by_dialogue(ident: dict | None) -> dict:
        """Groups the dialogue named, with the name each replaced (for an undo)."""
        found = {}
        for row in identity.associations(db, ident["revision_id"], "cluster") if ident else []:
            said = next((e for e in row.get("evidence") or []
                         if e.get("kind") in ("dialogue", "voice-memory")), None)
            if said and row.get("state") == "accepted":
                found[row["ref"]] = said
        return found

    @api.get("/api/analysis")
    def get_analysis(path: str):
        active = running(path)
        ident = who(path)
        script, how = find(path, ident)
        if script is None:
            return {"analysed": False, "job": active and {k: active.get(k) for k in (
                "id", "status", "stage", "progress")}}
        data = read_json(script)
        extra = read_json(sidecar(script, "analysis.json"))
        feats = read_json(sidecar(script, "features.json"))
        felt = {row.get("cue"): row for row in read_json(sidecar(script, "emotion.json")).get(
            "lines") or []}
        reading = read_json(sidecar(script, "reader.json"))
        read_lines = {row.get("cue"): row for row in reading.get("lines") or []}
        by_cue = {row["cue"]: row for row in extra.get("lines") or []}
        feature_rows = {row.get("cue"): row for row in feats.get("lines") or []}
        grouped = voice_tags.read_sidecar(sidecar(script, "speakers.json")) or {}
        why = {line.get("cue"): line.get("why") for line in grouped.get("lines") or []}
        names = load_names(db, path, ident)
        source = "identity" if ident and identity.associations(
            db, ident["revision_id"], "cluster") else ("legacy" if names else "none")
        locks = speaker_memory.line_locks(db, ident["revision_id"]) if ident else {}
        legacy_moved = load_plan(db, path).get("lines") or {}
        lines = []
        for seg in data.get("segments") or []:
            cue = (seg.get("cue") or {})
            cue_id = cue.get("cue_id")
            row = by_cue.get(cue_id, {})
            level = (cue.get("measurement") or {}).get("relative_db")
            feature = feature_rows.get(cue_id) or {}
            lines.append({
                "index": seg.get("index"), "start": seg.get("start"), "end": seg.get("end"),
                "cue": cue_id,
                "moved": str(cue_id) in locks or (not ident and str(cue_id) in legacy_moved),
                "locked": str(cue_id) in locks,
                "speaker": seg.get("speaker"),
                "character": names.get(seg.get("speaker") or "", ""),
                "text": seg.get("text_src") or "", "original_text": row.get("original_text"),
                "relative_db": level, "band": speaking.band(level),
                "measurement": (cue.get("measurement") or {}).get("state"),
                "pitch_hz": row.get("pitch_hz"), "movement_st": row.get("movement_st"),
                "uncertain": "speaker_uncertain" in (seg.get("issues") or []),
                "why": why.get(cue_id),
                "emotion": _feeling(felt.get(cue_id)),
                "features": {k: feature.get(k) for k in (
                    "quality", "active_seconds", "range_db", "mean_db", "reasons")}
                if feature else None,
            })
        # Each line's role from the subtitle track's styles (doblarr.subtitle_roles).
        styled: list[dict] = []
        if (data.get("segments") or [{}])[0].get("cue", {}).get("source", {}).get(
                "method") != "asr":
            kept = subtitle_roles.ensure_styled(
                script, Path(str((data.get("identity") or {}).get("input") or "")),
                str(data.get("script_lang") or ""))
            styled = subtitle_roles.events(kept) if kept else []
        roles = subtitle_roles.annotate(lines, styled)
        for line in lines:
            line.update(roles.get(str(line["cue"])) or {"role": "dialogue", "italic": False})
        summary = speaking.talk_share([{"id": 0, "label": "", "segments": [
            {**s, "speaker": names.get(s.get("speaker") or "", "") or s.get("speaker")}
            for s in data.get("segments") or []]}])
        language = language_of(script)
        suggestions: dict = {}
        if ident and ident.get("series_id"):
            labels_named = set(names)
            by_id = character_names(ident["series_id"])
            for label, picks in voice_tags.suggest_series(
                    db, ident["series_id"], ident["revision_id"], grouped, labels_named,
                    language, exclude=speaker_memory.held_out_revisions(db)).items():
                suggestions[label] = [{**p, "name": by_id.get(p["character_id"],
                                                              p["character_id"])}
                                      for p in picks if p["character_id"] in by_id]
        else:
            suggestions = voice_tags.suggest(db, path, grouped, names)
        cast = cast_rows(ident.get("series_id") if ident else None, names)
        clues = dialogue_clues.read(
            [{"speaker": line["speaker"], "start": line["start"], "end": line["end"],
              "text": line["text"], "cue": line["cue"]} for line in lines],
            [n for row in cast for n in [row["name"], *(row.get("aliases") or [])]])["groups"]
        snapshot = None
        if ident:
            snapshot = snapshots.get(db, ident["revision_id"], language)
        from ..vision import capability as vision_capability

        unsupported = vision_capability.unsupported_stages(config)
        return {"analysed": True, "lines": lines, "names": names, "names_from": source,
                "identity": ident, "script_match": how,
                "model": grouped.get("model"),
                "grouped_tracks": grouped.get("tracks") or [],
                "track_evidence": grouped.get("track_evidence") or [],
                "grouping": grouped.get("diagnostics") or {},
                "cast": cast,
                "suggestions": suggestions,
                "named_by": named_by_dialogue(ident),
                # Text drawn on the picture (title cards, captions naming someone),
                # read with the emotions: names on screen are evidence of who is who.
                "on_screen": on_screen(felt, lines, subtitle_roles.on_screen(styled)),
                "roles": {r: sum(1 for line in lines if line.get("role") == r)
                          for r in subtitle_roles.SPOKEN_ROLES},
                # The episode's parts (cold open, opening, episode, ending, preview).
                "structure": subtitle_roles.structure(
                    styled, max((float(line["end"]) for line in lines), default=0.0))
                if styled else [],
                # Names and terms the episode keeps saying (doblarr.key_terms).
                "terms": terms_of(script, data, lines, ident),
                # The few questions worth a person's time (doblarr.doubts).
                "doubts": doubts.queue(lines, names, dialogue=clues, suggestions=suggestions,
                                       reader=read_lines),
                # A strong model's reading of the script, when one is configured.
                "reader": {"model": reading.get("model"), "voices": reading.get("voices") or {}}
                if reading else None,
                # What the lines themselves say about each voice (names it
                # answers to, names it calls): catches a misnamed group.
                "dialogue": clues,
                "speakers": summary["speakers"], "total_seconds": summary["total_seconds"],
                "languages": {"text": data.get("script_lang"),
                              "original": extra.get("source_lang"),
                              "original_text": bool(extra.get("original_text"))},
                "measured": bool(extra), "features": bool(feats),
                "coverage": snapshots.coverage(snapshot, unsupported),
                "rerunnable": snapshots.rerunnable(snapshot),
                "baseline": data.get("dialogue_baseline") or {},
                "job": active and {k: active.get(k) for k in (
                    "id", "status", "stage", "progress")}}

    @api.put("/api/analysis/names")
    def put_names(body: NamesIn):
        clean = {k: v.strip()[:80] for k, v in body.names.items()
                 if k.startswith("SPEAKER_") and v.strip()}
        ident = who(body.path)
        script = find(body.path, ident)[0]
        if ident is None or not ident.get("series_id"):
            # Nothing to tie names to: kept the old way, under the file name.
            kept = load_plan(db, body.path).get("lines") or {}
            db.save_plan(names_key(body.path), Path(body.path).name,
                         {"names": clean, "lines": kept})
            legacy = voice_tags.remember(
                db, body.path, voice_tags.read_sidecar(script and sidecar(script,
                                                                          "speakers.json")),
                clean)
            return {"names": clean, "tagged": legacy, "identity": None}
        current = {r["ref"]: r for r in identity.associations(db, ident["revision_id"],
                                                              "cluster")}
        labels = set(clean) | set(current)
        if script is not None:
            labels |= {str(s.get("speaker")) for s in read_json(script).get("segments") or []}
        for label in sorted(labels):
            if not label.startswith("SPEAKER_"):
                continue
            name = clean.get(label)
            row = current.get(label)
            if name:
                character = identity.ensure_character(db, ident["series_id"], name)
                known = row or {}
                same = known.get("character_id") == character["id"]
                kept = label in body.keep or (not same and any(
                    e.get("kind") == "dialogue" for e in known.get("evidence") or []))
                if same and not kept and known.get("state") in ("manual", "accepted"):
                    continue
                # A name chosen over the dialogue's is a person's answer to it.
                identity.associate(db, ident["revision_id"], "cluster", label, character["id"],
                                   state="manual",
                                   evidence=[{"kind": "kept" if kept else "named"}],
                                   base_revision=row["revision"] if row else 0)
            elif row and row.get("character_id"):
                identity.associate(db, ident["revision_id"], "cluster", label, None,
                                   state="manual", evidence=[{"kind": "kept" if label in body.keep
                                                              else "cleared"}],
                                   base_revision=row["revision"])
        tagged: dict[str, int] = {}
        if script is not None:
            by_id = character_names(ident["series_id"])
            tagged = {by_id.get(k, k): v for k, v in teach(ident, script).items()}
            language = language_of(script)
            snapshots.record_stage(db, ident["revision_id"], language, "speaker_memory", "done",
                                   inputs=[sorted(clean.items())], identity=ident)
        return {"names": clean, "tagged": tagged, "identity": ident}

    def move(path: str, cues: list[str], character: str) -> dict:
        if running(path):
            raise HTTPException(409, "This episode is being analysed; wait for it to finish")
        ident = who(path)
        script = locate(path, ident)
        data = read_json(script)
        known_cues = {str((seg.get("cue") or {}).get("cue_id")) for seg in data.get("segments")
                      or []}
        missing = [c for c in cues if c not in known_cues]
        if missing:
            raise HTTPException(404, "no such line in this episode")
        names = load_names(db, path, ident)
        # "mina" is Mina: the cast's spelling, never a second character.
        series_id = ident.get("series_id") if ident else None
        cast_names = [c["name"] for c in cast_rows(series_id, names)] if series_id else \
            [c["name"] for c in voice_tags.cast(db, path)]
        known = {n.casefold(): n for n in [*names.values(), *cast_names]}
        character = " ".join(character.split())
        character = known.get(character.casefold(), character)
        labels = {str(seg.get("speaker")) for seg in data.get("segments") or []}
        label, new = label_for(names, labels, character)
        speakers.move_lines(script, sidecar(script, "speakers.json"),
                            {cue: label for cue in cues})
        if ident is None:
            if new and character:
                names[label] = character
            lines = dict(load_plan(db, path).get("lines") or {})
            for cue in cues:
                if character:
                    lines[cue] = character
                else:
                    lines.pop(cue, None)
            db.save_plan(names_key(path), Path(path).name, {"names": names, "lines": lines})
            voice_tags.remember(db, path, voice_tags.read_sidecar(
                sidecar(script, "speakers.json")), names)
            return {"speaker": label, "character": character, "new_voice": new,
                    "identity": None}
        found = identity.ensure_character(db, ident["series_id"], character) \
            if character else None
        if new and found is not None:
            identity.associate(db, ident["revision_id"], "cluster", label, found["id"],
                               state="manual", evidence=[{"kind": "new-voice-from-line"}])
        for cue in cues:
            current = next((r for r in identity.associations(db, ident["revision_id"], "line")
                            if r["ref"] == cue), None)
            identity.associate(db, ident["revision_id"], "line", cue,
                               found["id"] if found else None, state="manual",
                               evidence=[{"kind": "moved", "to": label}],
                               base_revision=current["revision"] if current else 0)
        changed = membership_changed(ident, script, "lines moved by hand")
        return {"speaker": label, "character": character, "new_voice": new,
                "baseline_changes": changed["changed"], "identity": ident}

    @api.put("/api/analysis/terms")
    def put_term(body: TermIn):
        """Keep (or forget) the show's wording for one term. A person chose
        it, so it is reviewed and every later translation of the show uses it."""
        from dataclasses import replace

        from ..knowledge import store as knowledge_store
        from ..knowledge.models import Entry

        ident = who(body.path)
        show = show_ref_of(ident)
        if not show:
            raise HTTPException(409, "This episode is not linked to a show yet")
        script = find(body.path, ident)[0]
        locale = body.locale or (target_of_script(script, read_json(script)) if script else "")
        if not locale:
            raise HTTPException(422, "Which language is this wording for?")
        source = body.source.strip()
        rendering = body.rendering.strip()
        same = [e for e in show_terms(show, locale)
                if key_terms.fold(e.source_form) == key_terms.fold(source)]
        if not rendering:
            for entry in same:
                knowledge_store.save_entry(db, replace(entry, status="retired", revision=1))
            return {"term": source, "rendering": None}
        # The wording the subtitles say, not the original audio's language.
        said_in = (str(read_json(script).get("script_lang") or "") or None) if script else None
        if same:
            entry = knowledge_store.save_entry(db, replace(
                same[0], phrase=rendering, status="reviewed", locale=locale,
                source_lang=said_in or same[0].source_lang, revision=1))
            for extra in same[1:]:
                knowledge_store.save_entry(db, replace(extra, status="retired", revision=1))
        else:
            entry = knowledge_store.save_entry(db, Entry(
                phrase=rendering, kind="term", locale=locale, source_form=source,
                source_lang=said_in, scope="show",
                scope_ref=show, status="reviewed",
                usage="kept from the episode analysis's names and terms"))
        return {"term": source, "rendering": entry.phrase, "id": entry.id}

    @api.put("/api/analysis/line")
    def move_line(body: LineIn):
        """One line, said by someone the grouping got wrong: a side character
        it filed under a main character, or a voice it never found."""
        return move(body.path, [body.cue], body.character)

    @api.put("/api/analysis/lines")
    def move_many(body: LinesIn):
        """Several lines at once: split a group into the two people it holds."""
        return move(body.path, list(dict.fromkeys(body.cues)), body.character)

    @api.post("/api/analysis/line/unlock")
    def unlock_line(body: UnlockIn):
        """Let the grouping decide a line again (the next regroup will)."""
        ident = require_identity(body.path)
        current = next((r for r in identity.associations(db, ident["revision_id"], "line")
                        if r["ref"] == body.cue), None)
        if current is None or current.get("state") != "manual":
            raise HTTPException(404, "that line was not assigned by hand")
        identity.associate(db, ident["revision_id"], "line", body.cue,
                           current.get("character_id"), state="rejected", locked=False,
                           evidence=[{"kind": "unlocked"}], base_revision=current["revision"])
        return {"unlocked": body.cue}

    def regroup_tracks(script: Path, vocals: Path, wanted, ident: dict | None):
        identity_data = read_json(script).get("identity") or {}
        source = Path(str(identity_data.get("input") or ""))
        try:
            found = speakers.other_tracks(
                source, str(identity_data.get("source_lang") or ""), vocals.parent,
                script.name.removesuffix(".script.json"),
                [str(t) for t in wanted] if isinstance(wanted, list) else wanted)
        except OSError:
            return [], []        # the video moved: the separated dialogue is enough
        if not found:
            return [], []
        checks = dict(config.get("analysis", {}) or {})
        described = {t["stream"]: t for t in speakers.audio_tracks(source)} \
            if source.is_file() else {}
        reference = sidecar(script, "source.wav") or vocals
        evidence = track_alignment.check_tracks(
            reference, [{"stream": s, "path": p, "lang": described.get(s, {}).get("lang", ""),
                         "title": described.get(s, {}).get("title", "")} for s, p in found],
            evaluation_streams=set() if (config.get("speakers") or {}).get(
                "evaluation_tracks", True) else speaker_memory.evaluation_streams(db, source),
            max_offset=float(checks.get("max_track_offset", 2.0)),
            min_correlation=float(checks.get("min_track_correlation", 0.45)),
            verify=bool(checks.get("verify_tracks", True)))
        allowed = track_alignment.usable(evidence)
        return [(s, p) for s, p in found if s in allowed], evidence

    @api.post("/api/analysis/regroup")
    def regroup(body: RegroupIn):
        """Sort the lines into voices again with other models, in seconds:
        the separated dialogue is already on disk. Characters follow their
        lines, hand-moved lines stay moved, and baselines are recomputed."""
        if running(body.path):
            raise HTTPException(409, "This episode is being analysed; wait for it to finish")
        ident = who(body.path)
        script = locate(body.path, ident)
        vocals = sidecar(script, "vocals.wav")
        if vocals is None:
            raise HTTPException(404, "the separated dialogue is no longer on disk; analyse again")
        grouped = sidecar(script, "speakers.json") or (
            vocals.parent / f"{script.name.removesuffix('.script.json')}.speakers.json")
        try:
            models = voice_models.resolve(body.models or voice_models.chosen(config), config)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        wanted = body.tracks if body.tracks is not None else (
            config.get("speakers") or {}).get("tracks", "all")
        tracks, evidence = regroup_tracks(script, vocals, wanted, ident)
        try:
            before, after, seconds = speakers.regroup(
                script, vocals, grouped, models, voice_models.folder(config),
                body.threshold if body.threshold is not None
                else (config.get("speakers") or {}).get("threshold"), tracks=tracks,
                track_evidence=evidence)
        except (OSError, RuntimeError, ImportError) as exc:
            raise HTTPException(502, f"could not group with {voice_models.combined_id(models)}: "
                                f"{exc}") from exc
        if ident is None:
            names = voice_tags.carry_names(before, after, seconds,
                                           dict(load_plan(db, body.path).get("names") or {}))
            moves: dict[str, str] = {}
            for cue, character in (load_plan(db, body.path).get("lines") or {}).items():
                label, new = label_for(names, set(after) | set(moves.values()), character)
                if new and character:
                    names[label] = character
                moves[cue] = label
            speakers.move_lines(script, grouped, moves)
            db.save_plan(names_key(body.path), Path(body.path).name,
                         {"names": names, "lines": load_plan(db, body.path).get("lines") or {}})
            voice_tags.remember(db, body.path, voice_tags.read_sidecar(grouped), names)
            return {"model": voice_models.combined_id(models), "voices": len(set(after)),
                    "names": names, "tracks": [stream for stream, _ in tracks],
                    "track_evidence": evidence, "identity": None}
        cues = [str((seg.get("cue") or {}).get("cue_id"))
                for seg in read_json(script).get("segments") or []]
        speaker_memory.carry_identities(db, ident, before, after, seconds, cues)
        relocked = speaker_memory.reapply_line_locks(db, ident, script, grouped)
        speaker_memory.name_from_dialogue(db, ident, [
            {"speaker": seg.get("speaker"), "start": seg["start"], "end": seg["end"],
             "text": seg.get("text_src") or "", "cue": (seg.get("cue") or {}).get("cue_id")}
            for seg in read_json(script).get("segments") or []])
        speaker_memory.name_from_memory(db, ident, grouped, language_of(script))
        changed = membership_changed(ident, script, "regrouped")
        names = load_names(db, body.path, ident)
        return {"model": voice_models.combined_id(models), "voices": len(set(after)),
                "names": names, "tracks": [stream for stream, _ in tracks],
                "track_evidence": evidence, "relocked": relocked,
                "baseline_changes": changed["changed"], "identity": ident}

    @api.get("/api/analysis/coverage")
    def coverage(path: str):
        """Which analysis stages are done, stale, failed, missing or unsupported."""
        ident = who(path)
        script, _how = find(path, ident)
        from ..vision import capability as vision_capability

        snapshot = snapshots.get(db, ident["revision_id"], language_of(script)) \
            if ident and script else None
        return {"identity": ident,
                "coverage": snapshots.coverage(snapshot,
                                               vision_capability.unsupported_stages(config)),
                "rerunnable": snapshots.rerunnable(snapshot)}

    def earlier_run(path: str, ident: dict | None, target: str) -> dict:
        from ..analysis import earlier_run as recorded_run

        return recorded_run(db, config.work_dir, path, ident) or {
            "input_file": path, "source_lang": "auto", "target_lang": target_of(target),
            "target_locale": ""}

    def target_of(target: str) -> str:
        from ..languages import base_language

        return base_language(target) or "es"

    @api.post("/api/analysis/rerun")
    def rerun(body: RerunIn):
        """Queue an analysis that runs only the asked-for stages (earlier ones
        are reused from their checkpoints). Never translates or generates."""
        unknown = [s for s in body.stages if s not in snapshots.STAGES]
        if unknown:
            raise HTTPException(422, f"unknown stage(s): {', '.join(unknown)}")
        if running(body.path):
            raise HTTPException(409, "This episode is already being analysed")
        if not Path(body.path).is_file():
            raise HTTPException(404, "the episode's file is not reachable from here")
        ident = who(body.path)
        overrides: dict = {"analysis.stages": body.stages}
        if body.visual is not None:
            overrides["analysis.visual"] = body.visual
        earlier = earlier_run(body.path, ident, body.target_lang)

        job = store.add(title=f"Analysis · {Path(body.path).name}", source="analysis",
                        **earlier, kind="analyze", overrides=overrides,
                        show_ref=show_ref_of(ident))
        return {"job_id": job.id, "stages": body.stages}

    # ------------------------------------------------------------- visual
    def visual_job(path: str, script: Path, ident: dict | None):
        """Enough of an analysis job to re-fuse visual evidence (no media decoded)."""
        from types import SimpleNamespace

        from ..models import Segment

        data = read_json(script)
        segments = []
        for seg in data.get("segments") or []:
            row = Segment(int(seg.get("index") or 0), float(seg["start"]), float(seg["end"]),
                          seg.get("text_src") or "", speaker=seg.get("speaker") or "")
            row.cue_id = str((seg.get("cue") or {}).get("cue_id") or "")
            segments.append(row)
        vocals = sidecar(script, "vocals.wav")
        identity_input = (data.get("identity") or {}).get("input") or path
        return SimpleNamespace(segments=segments, metrics={"identity": ident or {}},
                               vocals=vocals, input_file=Path(identity_input))

    def visual_file(script: Path) -> Path | None:
        return sidecar(script, "visual.json")

    @api.get("/api/analysis/visual")
    def get_visual(path: str):
        """Shots, visible tracks, scenes and who-speaks evidence, if analysed."""
        from ..vision import capability as vision_capability

        ident = who(path)
        script = locate(path, ident)
        found = visual_file(script)
        status = vision_capability.status(config)
        if found is None:
            return {"analysed": False, "capability": status}
        data = read_json(found)
        names = character_names(ident["series_id"]) if ident and ident.get("series_id") \
            else {}
        tracks_out = []
        for track in data.get("tracks") or []:
            matches = [{**m, "name": names.get(m["character_id"], m["character_id"])}
                       for m in track.get("matches") or []]
            tracks_out.append({k: track.get(k) for k in (
                "id", "shot", "start", "end", "detector", "domain", "frames", "thumbnail",
                "assigned", "split_from", "merged")} | {
                "character": track.get("character"),
                "character_name": names.get(track.get("character") or ""),
                "matches": matches})
        associations = []
        for row in data.get("associations") or []:
            decision = dict(row.get("decision") or {})
            decision["name"] = names.get(decision.get("character_id") or "")
            associations.append({**row, "decision": decision, "candidates": [
                {**c, "name": names.get(c["character_id"], c["character_id"])}
                for c in row.get("candidates") or []]})
        faces_data = data.get("faces") or {}
        return {"analysed": True, "capability": status, "identity": ident,
                "shots": len((data.get("shots") or {}).get("shots") or []),
                "shot_method": (data.get("shots") or {}).get("method"),
                "frames": faces_data.get("sampled"), "backend": faces_data.get("backend"),
                "tracks": tracks_out, "scenes": data.get("scenes") or [],
                "associations": associations, "timings": data.get("timings") or {}}

    @api.get("/api/analysis/visual/thumb")
    def visual_thumb(path: str, name: str):
        """One face crop. Only files inside this analysis' crop folder are served."""
        if "/" in name or "\\" in name or not name.endswith(".jpg"):
            raise HTTPException(422, "not a thumbnail name")
        script = locate(path)
        found = visual_file(script)
        crops = Path(str((read_json(found).get("faces") or {}).get("crops") or "")) if found \
            else None
        if crops is None or not (crops / name).is_file():
            raise HTTPException(404, "no such thumbnail")
        if not inside(crops / name, [config.work_dir]):
            raise HTTPException(403, "outside the work folder")
        return FileResponse(crops / name, media_type="image/jpeg",
                            headers={"Cache-Control": "private, max-age=86400"})

    @api.post("/api/analysis/visual/tracks")
    def correct_tracks(body: TracksIn):
        """Assign (or mark unknown), split or merge visible tracks. Assigning a
        character keeps that face as an approved reference for the series."""
        from ..vision import pipeline as vision_pipeline
        from ..vision import references as vision_refs
        from ..vision import tracks as vision_tracks

        ident = require_identity(body.path)
        script = locate(body.path, ident)
        found = visual_file(script)
        if found is None:
            raise HTTPException(404, "this episode has no visual analysis yet")
        data = read_json(found)
        current = {t["id"]: t for t in data.get("tracks") or []}
        unknown = [t for t in [*body.assign, *body.split, *[x for p in body.merge for x in p]]
                   if t not in current]
        if unknown:
            raise HTTPException(404, f"no such track: {unknown[0]}")
        assign: dict[str, str | None] = {}
        for track_id, name in body.assign.items():
            if not name:
                assign[track_id] = None
                continue
            character = identity.ensure_character(db, ident["series_id"], name)
            assign[track_id] = character["id"]
            vision_refs.add_from_track(db, ident["series_id"], current[track_id],
                                       character["id"], revision_id=ident["revision_id"])
        try:
            vision_tracks.correct(db, ident["revision_id"], base_revision=body.base_revision,
                                  assign=assign, split=body.split,
                                  merge=[list(p) for p in body.merge],
                                  tracks=list(current.values()))
        except records.StudioConflict as exc:
            raise HTTPException(409, {"error": str(exc), "current": exc.current}) from exc
        refreshed = vision_pipeline.refresh(db, visual_job(body.path, script, ident), found,
                                            config)
        return {"tracks": len(refreshed.get("tracks") or []),
                "corrections": vision_tracks.corrections(db, ident["revision_id"])}

    @api.post("/api/analysis/visual/scenes")
    def correct_scenes(body: ScenesIn):
        from ..vision import pipeline as vision_pipeline
        from ..vision import scenes as vision_scenes

        ident = require_identity(body.path)
        script = locate(body.path, ident)
        found = visual_file(script)
        if found is None:
            raise HTTPException(404, "this episode has no visual analysis yet")
        try:
            vision_scenes.correct(db, ident["revision_id"], add=body.add, remove=body.remove,
                                  base_revision=body.base_revision)
        except records.StudioConflict as exc:
            raise HTTPException(409, {"error": str(exc), "current": exc.current}) from exc
        refreshed = vision_pipeline.refresh(db, visual_job(body.path, script, ident), found,
                                            config)
        return {"scenes": refreshed.get("scenes") or []}

    @api.get("/api/vision/status")
    def vision_status():
        from ..vision import capability as vision_capability

        return vision_capability.status(config)

    @api.post("/api/vision/models/{model_id}/download")
    def vision_download(model_id: str):
        """Fetch one visual model file (explicit; checked against its checksum)."""
        from ..vision import capability as vision_capability

        if model_id not in vision_capability.MODELS:
            raise HTTPException(404, "no such visual model")
        try:
            path = vision_capability.download(model_id, config)
        except OSError as exc:
            raise HTTPException(502, str(exc)) from exc
        return {"model": model_id, "path": str(path)}

    @api.get("/api/analysis/migration")
    def migration_preview():
        """What moving older name-keyed memory onto identities would do."""
        return identity_migration.preview(db, config.work_dir, cache_dir=cache_dir)

    @api.post("/api/analysis/migration")
    def migration_apply(body: MigrationIn):
        try:
            return identity_migration.apply(db, config.work_dir, body.fingerprint,
                                            cache_dir=cache_dir, keys=body.keys)
        except identity_migration.StalePreview as exc:
            raise HTTPException(409, str(exc)) from exc

    def source_video(script: Path) -> Path:
        """The video the analysis ran on, from the script; never a client path."""
        data = json.loads(script.read_text(encoding="utf-8"))
        source = Path(str((data.get("identity") or {}).get("input") or ""))
        if not source.name or not source.is_file():
            raise HTTPException(404, "the episode's video is not reachable from here")
        return source

    audio_streams = speakers.audio_tracks

    @api.get("/api/analysis/tracks")
    def tracks(path: str):
        """The episode's audio tracks, to watch a line in any of them."""
        script = locate(path)
        source = source_video(script)
        streams = audio_streams(source)
        original = (json.loads(script.read_text(encoding="utf-8")).get("identity") or {}).get(
            "source_lang")
        default = next((s["stream"] for s in streams if s["lang"] == original),
                       streams[0]["stream"] if streams else None)
        return {"tracks": streams, "default": default}

    cutting = threading.Lock()

    @api.get("/api/analysis/video")
    def video(path: str, start: float, end: float, audio: int | None = None):
        """One stretch of the episode as a small MP4 any browser plays: the
        source may be 10-bit HEVC with four audio tracks; a clip is H.264 at
        540p with the one track asked for."""
        if end <= start or end - start > MAX_CLIP or start < 0:
            raise HTTPException(422, f"a clip is between 0 and {MAX_CLIP:.0f} seconds")
        script = locate(path)
        source = source_video(script)
        streams = audio_streams(source)
        if audio is not None and audio not in {s["stream"] for s in streams}:
            raise HTTPException(422, "no such audio track in this episode")
        cache = Path(config.work_dir) / "cache" / "clips"
        key = hashlib.sha1(f"{source}|{source.stat().st_mtime_ns}|{start:.2f}|{end:.2f}|"
                           f"{audio}".encode()).hexdigest()[:20]
        clip = cache / f"{key}.mp4"
        if not clip.is_file():
            cache.mkdir(parents=True, exist_ok=True)
            temp = cache / f"{key}.{threading.get_ident()}.part"
            mapping = ["-map", "0:v:0"] + (["-map", f"0:{audio}"] if audio is not None
                                           else ["-map", "0:a:0?"])
            try:
                run_ffmpeg(["-v", "error", "-y", "-ss", f"{start:.3f}", "-i", str(source),
                            "-t", f"{end - start:.3f}", *mapping, "-sn",
                            "-vf", f"scale=-2:'min({VIDEO_HEIGHT},ih)'", "-c:v", "libx264",
                            "-preset", "veryfast", "-crf", "24", "-pix_fmt", "yuv420p",
                            "-c:a", "aac", "-b:a", "128k", "-ac", "2", "-movflags", "+faststart",
                            "-f", "mp4", str(temp)], timeout=CLIP_TIMEOUT)
                temp.replace(clip)
            finally:
                temp.unlink(missing_ok=True)
            with cutting:
                kept = sorted(cache.glob("*.mp4"), key=lambda f: f.stat().st_mtime,
                              reverse=True)
                for old in kept[CLIP_CACHE:]:
                    old.unlink(missing_ok=True)
        return FileResponse(clip, media_type="video/mp4",
                            headers={"Cache-Control": "private, max-age=86400"})

    @api.get("/api/analysis/frame")
    def frame(path: str, t: float, height: int = 180):
        """One still of the episode at `t` seconds, to see who is talking."""
        if t < 0 or not 90 <= height <= 540:
            raise HTTPException(422, "a frame is at t >= 0, 90 to 540 pixels tall")
        source = source_video(locate(path))
        cache = Path(config.work_dir) / "cache" / "frames"
        key = hashlib.sha1(f"{source}|{source.stat().st_mtime_ns}|{t:.2f}|{height}"
                           .encode()).hexdigest()[:20]
        still = cache / f"{key}.jpg"
        if not still.is_file():
            cache.mkdir(parents=True, exist_ok=True)
            temp = cache / f"{key}.{threading.get_ident()}.part"
            try:
                run_ffmpeg(["-v", "error", "-y", "-ss", f"{t:.3f}", "-i", str(source),
                            "-frames:v", "1", "-an", "-sn", "-vf", f"scale=-2:{height}",
                            "-q:v", "4", "-f", "image2", "-c:v", "mjpeg", str(temp)],
                           timeout=FRAME_TIMEOUT)
                temp.replace(still)
            finally:
                temp.unlink(missing_ok=True)
            with cutting:
                kept = sorted(cache.glob("*.jpg"), key=lambda f: f.stat().st_mtime,
                              reverse=True)
                for old in kept[FRAME_CACHE:]:
                    old.unlink(missing_ok=True)
        return FileResponse(still, media_type="image/jpeg",
                            headers={"Cache-Control": "private, max-age=86400"})

    @api.get("/api/analysis/clip")
    def clip(path: str, start: float, end: float, track: str = "vocals"):
        """One line as audio: the separated dialogue, or the original mix."""
        if end <= start or end - start > MAX_CLIP or start < 0:
            raise HTTPException(422, f"a clip is between 0 and {MAX_CLIP:.0f} seconds")
        script = locate(path)
        stem = script.name.removesuffix(".script.json")
        source = (sidecar(script, "vocals.wav") if track == "vocals"
                  else sidecar(script, "source.wav"))
        if source is None:
            raise HTTPException(404, "the analysed audio is no longer on disk")
        if not inside(source, [config.work_dir]):
            raise HTTPException(403, "outside the work folder")
        audio = run_ffmpeg_bytes(
            ["-v", "error", "-ss", f"{max(0.0, start - 0.15):.3f}",
             "-t", f"{end - start + 0.3:.3f}", "-i", str(source), "-ac", "1", "-ar", "24000",
             "-f", "wav", "-"], timeout=FRAME_TIMEOUT)
        if not audio:
            raise HTTPException(500, f"could not cut {stem}")
        return Response(content=audio, media_type="audio/wav")

    return api
