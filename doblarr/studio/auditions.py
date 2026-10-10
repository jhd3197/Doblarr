"""Character auditions on fixed excerpts, through the queue, within a budget.

Generalised from `scripts/voice_audition.py`, which hardcoded one episode's
locale, engines, reference rules and Spanish direction prompt. Here those are
inputs:

- **What is auditioned** is one character's lines from a finished run, chosen
  by performance type — calm, quiet, intense, contextual — from the run's own
  source measurements. A type the material does not contain is reported as
  missing, never faked with a louder take of a calm line.
- **How** is a list of candidates: the current voice, a preset, a directed
  preset, a clone of the original actor, and (explicitly experimental) a clone
  from each line's own audio. Each candidate is checked against what its engine
  can actually do; a direction the engine cannot take is reported as
  unsupported, not silently dropped.
- **References** are cut from the original-language separated dialogue (never
  another dub, never an evaluation-only track), with their identity, duration,
  overlapping speakers and level over the bed recorded as findings. A clone
  candidate may ask for the lowest, brightest or liveliest clean line instead
  of the default: a clone copies its reference's energy and register, so an
  excitable character cloned from a flat line comes out flat.
- **Shaping** (pitch and formant shifts, in semitones) is applied to each take
  after generation and is part of the take's identity, so the same line can be
  heard plain and shaped. A shaped candidate that is cast carries its shaping
  into the voice cast; the pipeline applies it to every line of that character.

Generation is bounded (candidates x excerpts x retakes, and a request budget),
cancellable between takes, and resumable: every take has a receipt keyed by its
exact request, so rerunning an audition pays only for what is missing.
"""

from __future__ import annotations

import hashlib
import logging
import shutil
import time
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..artifacts import digest, read_json
from ..budget import RequestBudget
from ..clients.speech import SpeechError
from ..clients.voicebox import VoiceboxClient
from ..errors import DoblarrError, JobCancelled
from ..fileio import write_json
from . import records
from .media import cut_audio, duration

log = logging.getLogger("doblarr.studio.auditions")

CATEGORIES = ("calm", "quiet", "intense", "contextual")
ALL_CATEGORIES: list[Literal["calm", "quiet", "intense", "contextual"]] = [
    "calm", "quiet", "intense", "contextual"]
KINDS = ("current", "preset", "directed", "clone_character", "clone_line")
PICKS = ("", "low", "high", "lively")
SHAPE_LIMIT = 6.0             # semitones; beyond this a voice is a different person
REFERENCE_SECONDS = (4.0, 12.0)
LINE_REFERENCE_MIN = 2.0      # voicebox refuses a clone sample shorter than this
PICK_CHOICES = 3              # fewer preferred-length lines than this is no choice
PICK_MEASURED = 8             # lines measured when ranking for a pick
QUIET_DB, INTENSE_DB = -3.5, 3.5
MAX_CANDIDATES = 6
MAX_EXCERPTS = 8


class AuditionError(DoblarrError):
    http_status = 422


class CandidateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=40, pattern=r"^[A-Za-z0-9_.-]+$")
    kind: Literal["current", "preset", "directed", "clone_character", "clone_line"]
    engine: str = Field(default="", max_length=40)
    voice: str = Field(default="", max_length=200)        # voicebox profile id
    direction: str = Field(default="", max_length=500)
    reference: str | None = Field(default=None, max_length=64)   # studio voice reference
    seed: int | None = Field(default=3197, ge=0, le=2**31)
    # Which original line a clone learns from: "" keeps the default (the
    # cleanest line near 8 s); low / high / lively rank the clean lines by
    # pitch or by how much the pitch moves.
    pick: Literal["", "low", "high", "lively"] = ""
    pitch_semitones: float = Field(default=0.0, ge=-SHAPE_LIMIT, le=SHAPE_LIMIT)
    formant_semitones: float = Field(default=0.0, ge=-SHAPE_LIMIT, le=SHAPE_LIMIT)

    @model_validator(mode="after")
    def coherent(self):
        if self.pick and self.kind not in ("clone_character", "clone_line"):
            raise ValueError("choosing a reference line only applies to a clone")
        if self.pick and self.reference:
            raise ValueError("a chosen voice reference already fixes the line; "
                             "drop either the reference or the pick")
        if self.kind == "current" and (self.pitch_semitones or self.formant_semitones):
            raise ValueError("the current voice's takes are not regenerated, so they "
                             "cannot be shaped; audition a generated candidate instead")
        return self


class AuditionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    character: str = Field(min_length=1, max_length=80)
    job_id: str = Field(min_length=1, max_length=64)
    categories: list[Literal["calm", "quiet", "intense", "contextual"]] = Field(
        default_factory=lambda: list(ALL_CATEGORIES))
    per_category: int = Field(default=1, ge=1, le=3)
    candidates: list[CandidateIn] = Field(min_length=1, max_length=MAX_CANDIDATES)
    retakes: int = Field(default=0, ge=0, le=2)
    check_words: bool = False
    max_requests: int = Field(default=40, ge=1, le=400)
    language: str = Field(default="", max_length=16)

    @field_validator("candidates")
    @classmethod
    def unique(cls, value):
        names = [c.name for c in value]
        if len(set(names)) != len(names):
            raise ValueError("candidate names must be unique")
        return value


def capabilities(engine: str, vb=None, *, presets_lookup: bool = True) -> dict:
    """What an engine can be asked for, from the adapter's own declarations.

    `vb` is the configured speech client; without one, voicebox's fixed
    declarations answer. `presets_lookup=False` skips the network read.
    """
    adapter = vb if vb is not None and hasattr(vb, "supports_cloning") else VoiceboxClient
    canonical = adapter.canonical_engine(engine)
    presets: bool | None = None
    if vb is not None and canonical and presets_lookup:
        try:
            presets = bool(vb.preset_voices(canonical))
        except Exception:  # noqa: BLE001 - an engine without presets just says so
            presets = None
    return {"engine": canonical or "(service default)",
            "direction": adapter.supports_direction(canonical),
            "clone": adapter.supports_cloning(canonical) if canonical else True,
            "presets": presets,
            "note": ("Direction is only sent to engines that accept an instruction. "
                     "Cloning needs a clean sample of at least 2 s.")}


def _relative_db(row: dict) -> float | None:
    value = ((row.get("cue") or {}).get("measurement") or {}).get("relative_db")
    return float(value) if isinstance(value, int | float) else None


def _span(row: dict) -> tuple[float, float]:
    spans = ((row.get("cue") or {}).get("source") or {}).get("spans") or []
    if spans:
        return float(spans[0]["start"]), float(spans[-1]["end"])
    start = row.get("source_start")
    start = float(start) if start is not None else float(row["start"])
    return start, start + float(row["end"]) - float(row["start"])


def select_excerpts(rows: list[dict], character: str, categories, per_category: int) -> dict:
    """Fixed excerpts by performance type; missing types are reported, not faked."""
    mine = [r for r in rows if r.get("speaker") == character
            and (r.get("text_translated") or "").strip()]
    if not mine:
        raise AuditionError(f"{character} has no translated lines in this run")
    chosen: dict[str, list[dict]] = {c: [] for c in categories}
    used: set[int] = set()

    def take(category, pool):
        for row in pool:
            if len(chosen[category]) >= per_category:
                return
            if row["index"] in used:
                continue
            used.add(row["index"])
            chosen[category].append(row)

    measured = [(r, _relative_db(r)) for r in mine]
    if "quiet" in chosen:
        take("quiet", [r for r, db in sorted(measured, key=lambda x: x[1] or 0)
                       if db is not None and db <= QUIET_DB])
    if "intense" in chosen:
        take("intense", [r for r, db in sorted(measured, key=lambda x: -(x[1] or 0))
                         if db is not None and db >= INTENSE_DB])
    if "calm" in chosen:
        take("calm", sorted([r for r, db in measured if db is not None
                             and QUIET_DB < db < INTENSE_DB],
                            key=lambda r: -(float(r["end"]) - float(r["start"]))))
    if "contextual" in chosen:
        others = [r for r in rows if r.get("speaker") != character]
        exchanges = [r for r in mine if any(abs(float(o["start"]) - float(r["end"])) < 1.0
                                            or abs(float(r["start"]) - float(o["end"])) < 1.0
                                            for o in others)]
        take("contextual", exchanges)
    missing = [c for c, found in chosen.items() if not found]
    unmeasured = sum(1 for _r, db in measured if db is None)
    excerpts = []
    for category, found in chosen.items():
        for row in found:
            start, end = _span(row)
            excerpts.append({"cue_id": (row.get("cue") or {}).get("cue_id") or "",
                             "index": row["index"], "category": category,
                             "text": row.get("text_translated") or "",
                             "tts_text": row.get("tts_text") or "",
                             "source_text": row.get("text_src") or "",
                             "source": {"start": start, "end": end},
                             "relative_db": _relative_db(row)})
    excerpts = excerpts[:MAX_EXCERPTS]
    notes = []
    if missing:
        notes.append("this character's lines contain no " + ", ".join(missing)
                     + " material in this run")
    if unmeasured:
        notes.append(f"{unmeasured} line(s) have no source level measurement, so they "
                     "could only be used as contextual excerpts")
    return {"excerpts": excerpts, "missing": missing, "notes": notes}


def reference_findings(clip: Path, rows: list[dict], character: str, start: float,
                       end: float, bed: Path | None = None, root: Path | None = None) -> dict:
    """Identity and fitness of a clone reference, stated as findings."""
    findings = []
    seconds = duration(clip)
    if seconds < LINE_REFERENCE_MIN:
        findings.append({"code": "reference_too_short", "severity": "error",
                         "detail": f"{seconds:.2f}s — the clone service needs 2 s or more"})
    overlapping = sorted({str(r.get("speaker") or "") for r in rows
                          if r.get("speaker") != character
                          and _span(r)[0] < end + 0.3 and _span(r)[1] > start - 0.3})
    if overlapping:
        findings.append({"code": "reference_overlap", "severity": "warning",
                         "detail": "other speakers talk within 0.3 s: " + ", ".join(
                             s for s in overlapping if s)})
    margin = None
    if bed is not None and root is not None:
        from ..levels import analyze

        voice = analyze(clip).get("speech_db")
        under = analyze(cut_audio(bed, start, end, root)).get("speech_db")
        if voice is not None and under is not None:
            margin = round(voice - under, 1)
            if margin < 10:
                findings.append({"code": "reference_noisy", "severity": "warning",
                                 "detail": f"only {margin} dB over the background bed"})
    return {"sha256": hashlib.sha256(Path(clip).read_bytes()).hexdigest(),
            "seconds": round(seconds, 2), "window": {"start": start, "end": end},
            "voice_over_bed_db": margin, "findings": findings}


def _alone(rows: list[dict], row: dict, character: str) -> bool:
    return not any(o.get("speaker") != character and _span(o)[0] < _span(row)[1] + 0.3
                   and _span(o)[1] > _span(row)[0] - 0.3 for o in rows)


def character_reference(rows: list[dict], character: str, vocals: Path, root: Path,
                        bed: Path | None = None, pick: str = "") -> tuple[Path, dict]:
    """A clean solo line of this character's ORIGINAL performance.

    By default the cleanest line near 8 s. With `pick`, the clean lines are
    measured and ranked instead: `low` and `high` by median pitch, `lively` by
    how far the pitch moves. Fewer than PICK_CHOICES lines in the preferred
    length is no choice at all, so shorter lines that can still be cloned join.
    """
    low, high = REFERENCE_SECONDS
    mine = [r for r in rows if r.get("speaker") == character
            and low <= _span(r)[1] - _span(r)[0] <= high]
    alone = [r for r in mine if _alone(rows, r, character)]
    pool = alone or mine
    if pick and len(pool) < PICK_CHOICES:
        shorter = [r for r in rows if r.get("speaker") == character and r not in pool
                   and _span(r)[1] - _span(r)[0] >= LINE_REFERENCE_MIN
                   and _alone(rows, r, character)]
        pool = pool + shorter
    if not pool:
        raise AuditionError(f"no {low:.0f}–{high:.0f} s line of {character} to clone from")
    ordered = (pool if pick else
               sorted(pool, key=lambda r: abs((_span(r)[1] - _span(r)[0]) - 8.0)))
    best = None
    for row in ordered[:PICK_MEASURED if pick else 6]:
        start, end = _span(row)
        clip = cut_audio(vocals, start, end, root)
        found = reference_findings(clip, rows, character, start, end, bed, root)
        found.update(_voice_measures(clip))
        usable = not any(f["severity"] == "error" for f in found["findings"])
        if pick:
            value = found["liveliness_st"] if pick == "lively" else found["pitch_hz"]
            if value is None:
                continue
            rank = -value if pick == "low" else value
            score: tuple = (usable, rank, not found["findings"])
        else:
            score = (not found["findings"], found["voice_over_bed_db"] or 0)
        if best is None or score > best[0]:
            best = (score, clip, {**found, "line": row["index"], "pick": pick,
                                  "text": row.get("text_src") or ""})
    if best is None:
        raise AuditionError(f"none of {character}'s lines could be measured for a "
                            f"{pick} reference")
    return best[1], best[2]


def _voice_measures(clip: Path) -> dict:
    from ..stages.synthesize import reference_liveliness, reference_pitch

    return {"pitch_hz": reference_pitch(clip), "liveliness_st": reference_liveliness(clip)}


def _spoken(excerpt: dict, pronunciations: dict) -> str:
    from ..stages.quality import spoken_form

    return excerpt.get("tts_text") or spoken_form(excerpt["text"], pronunciations or {})


def _profile(vb, state: dict, name: str, reference: Path, text: str, language: str) -> str:
    sha = hashlib.sha256(reference.read_bytes()).hexdigest()
    known = state.setdefault("profiles", {}).get(name)
    try:
        existing = {p.get("id") for p in vb.voice_profiles()}
    except Exception:  # noqa: BLE001 - an unreachable listing just means "make one"
        existing = set()
    if known and known.get("id") in existing and known.get("sha") == sha:
        return known["id"]
    profile = vb.clone_voice(name, language, reference, text or "-",
                             description="Doblarr studio audition")
    state["profiles"][name] = {"id": profile, "sha": sha}
    return profile


def run(db, root: Path, audition: dict, snapshot: dict, vb, *, cancel=None, progress=None,
        pronunciations: dict | None = None) -> dict:
    """Generate every missing take for every candidate; resumable and bounded."""
    rows = snapshot.get("segments") or []
    media = snapshot.get("media") or {}
    vocals = Path(media["vocals"]) if media.get("vocals") else None
    bed = Path(media["background"]) if media.get("background") else None
    language = audition.get("language") or snapshot.get("language") or "es"
    work = Path(root) / "studio" / "auditions" / audition["id"]
    state = read_json(work / "state.json") or {}
    budget = RequestBudget(int(audition.get("max_requests") or 40), cancel)
    excerpts = audition["plan"]["excerpts"]
    takes = dict(audition.get("takes") or {})
    candidates = []
    started = time.perf_counter()
    status, error = "complete", ""
    total = max(1, len(excerpts) * len(audition["candidates"]))
    done = 0
    try:
        for candidate in audition["candidates"]:
            entry = {**candidate, "capability": capabilities(candidate.get("engine", ""), vb),
                     "status": "ready", "reason": ""}
            candidates.append(entry)
            mine = takes.setdefault(candidate["name"], {})
            if candidate["kind"] == "current":
                for excerpt in excerpts:
                    row: dict = next((r for r in rows if r["index"] == excerpt["index"]), {})
                    clip = row.get("audio_clip")
                    mine[excerpt["cue_id"]] = ({"path": clip, "state": "existing", "attempts": 0}
                                               if clip and Path(clip).is_file() else
                                               {"state": "missing", "attempts": 0})
                entry["status"] = "existing takes, nothing generated"
                done += len(excerpts)
                continue
            if candidate["kind"] == "directed" and not entry["capability"]["direction"]:
                entry.update(status="unsupported", reason=(
                    f"{entry['capability']['engine']} does not accept delivery instructions"))
                done += len(excerpts)
                continue
            if candidate["kind"] in ("clone_character", "clone_line"):
                if not entry["capability"]["clone"]:
                    entry.update(status="unsupported",
                                 reason=f"{entry['capability']['engine']} cannot clone a voice")
                    done += len(excerpts)
                    continue
                if vocals is None or not vocals.is_file():
                    entry.update(status="unavailable", reason=(
                        "cloning the original actor needs the original-language separated "
                        "dialogue, which this run does not have on disk"))
                    done += len(excerpts)
                    continue
            if candidate["kind"] in ("preset", "directed") and not candidate.get("voice"):
                entry.update(status="unavailable", reason="choose a preset voice first")
                done += len(excerpts)
                continue
            profile = candidate.get("voice") or ""
            if candidate["kind"] == "clone_character":
                clip, found = _reference_for(db, candidate, rows, audition, vocals, bed, work)
                entry["reference_findings"] = found
                profile = _profile(vb, state, f"Doblarr studio {audition['character']} "
                                   f"{candidate['name']}", clip, found.get("text", ""),
                                   language)
                write_json(work / "state.json", state)
            entry["profile"] = profile
            for excerpt in excerpts:
                if cancel is not None and cancel.is_set():
                    raise JobCancelled("cancelled between audition takes")
                line_profile = profile
                notes = []
                if candidate["kind"] == "clone_line":
                    start, end = excerpt["source"]["start"], excerpt["source"]["end"]
                    if end - start >= LINE_REFERENCE_MIN:
                        assert vocals is not None
                        clip = cut_audio(vocals, start - 0.05, end + 0.05, work)
                        line_profile = _profile(
                            vb, state, f"Doblarr studio {audition['character']} line "
                            f"{excerpt['index']}", clip, excerpt["source_text"], language)
                    else:
                        clip, found = _reference_for(db, candidate, rows, audition, vocals,
                                                     bed, work)
                        line_profile = _profile(vb, state, f"Doblarr studio "
                                                f"{audition['character']} character", clip,
                                                found.get("text", ""), language)
                        notes.append("line too short to clone from; used the character "
                                     "reference")
                    write_json(work / "state.json", state)
                result = _take(vb, work, audition, candidate, excerpt, line_profile, language,
                               pronunciations or {}, budget, cancel)
                result["notes"] = notes + result.get("notes", [])
                mine[excerpt["cue_id"]] = result
                done += 1
                if progress:
                    progress(done, total, f"{candidate['name']} line {excerpt['index']}")
                records.update(db, "audition", audition["id"],
                               {"takes": takes, "results": candidates, "status": "running"})
            entry["status"] = "generated"
    except JobCancelled:
        status, error = "cancelled", "cancelled; run again to continue where it stopped"
        raise
    except (DoblarrError, OSError, ValueError) as exc:
        status, error = "failed", str(exc)
    finally:
        records.update(db, "audition", audition["id"], {
            "takes": takes, "results": candidates, "status": status, "error": error,
            "usage": {"budget": budget.snapshot(),
                      "seconds": round(time.perf_counter() - started, 1)}})
    return records.get(db, "audition", audition["id"]) or {}


def _reference_for(db, candidate, rows, audition, vocals, bed, work):
    if candidate.get("reference"):
        reference = records.get(db, "reference", candidate["reference"])
        if reference is None or "voice" not in (reference.get("roles") or []):
            raise AuditionError("that reference is not a voice reference")
        sample = reference.get("sample") or {}
        track = reference.get("track") or {}
        if not sample or not track.get("media_path"):
            raise AuditionError("a voice reference needs a track and a sample window")
        clip = cut_audio(Path(track["media_path"]), sample["start"], sample["end"], work,
                         stream=track.get("audio_index"))
        found = reference_findings(clip, rows, audition["character"], sample["start"],
                                   sample["end"])
        return clip, {**found, "reference": reference["id"], "text": sample.get("text", "")}
    return character_reference(rows, audition["character"], vocals, work, bed,
                               pick=candidate.get("pick") or "")


def _take(vb, work: Path, audition: dict, candidate: dict, excerpt: dict, profile: str,
          language: str, pronunciations: dict, budget: RequestBudget, cancel) -> dict:
    text = _spoken(excerpt, pronunciations)
    options: dict = {}
    if candidate.get("engine"):
        options["engine"] = candidate["engine"]
    direction = candidate.get("direction") if candidate["kind"] == "directed" else ""
    if direction:
        options["instruct"] = direction[:500]
    request = {"profile": profile, "text": text, "language": language, "options": options,
               "seed": candidate.get("seed")}
    shape = _shape(candidate)
    if any(shape.values()):
        # Only present when set, so every unshaped take keeps its existing receipt.
        request["shape"] = shape
    key = digest(request)[:16]
    dest = work / candidate["name"] / f"{excerpt['cue_id'] or excerpt['index']}.wav"
    receipt = read_json(dest.with_suffix(".json"))
    if dest.is_file() and receipt.get("request") == key:
        return {**receipt["result"], "path": str(dest), "reused": True}
    attempts: list[dict] = []
    retakes = int(audition.get("retakes") or 0)
    for attempt in range(retakes + 1):
        if not budget.charge("audition"):
            if budget.cancelled:
                raise JobCancelled("cancelled before an audition take")
            return {"state": "skipped", "attempts": len(attempts),
                    "notes": ["the audition's request budget is spent"]}
        path = dest.with_name(f"{dest.stem}.t{attempt}.wav")
        seed = candidate.get("seed")
        started = time.perf_counter()
        try:
            vb.synthesize_to_file(profile, text, language, path, cancel_event=cancel,
                                  seed=None if seed is None else seed + attempt * 101,
                                  **options)
        except SpeechError as exc:
            attempts.append({"attempt": attempt, "error": str(exc)[:300]})
            continue
        judged = _judge(vb, path, excerpt["text"], language) if audition.get(
            "check_words") else {"clean": None}
        attempts.append({"attempt": attempt, "path": str(path),
                         "seconds": round(time.perf_counter() - started, 1), **judged})
        if judged.get("clean") is not False:
            break
    usable = [a for a in attempts if a.get("path")]
    if not usable:
        return {"state": "failed", "attempts": len(attempts),
                "error": attempts[-1].get("error", "") if attempts else ""}
    chosen = next((a for a in usable if a.get("clean") is not False), usable[-1])
    shutil.copy2(str(chosen["path"]), dest)
    if any(shape.values()):
        from ..stages.synthesize import shift_pitch

        shift_pitch(dest, shape["pitch_semitones"], cancel, shape["formant_semitones"])
    result = {"state": "generated", "attempts": len(attempts), "chosen": chosen["attempt"],
              "clean": chosen.get("clean"), "heard": chosen.get("heard", ""),
              "request": request, "history": attempts}
    write_json(dest.with_suffix(".json"), {"request": key, "result": result})
    return {**result, "path": str(dest)}


def _shape(candidate: dict) -> dict:
    return {"pitch_semitones": float(candidate.get("pitch_semitones") or 0.0),
            "formant_semitones": float(candidate.get("formant_semitones") or 0.0)}


def _judge(vb, path: Path, text: str, language: str) -> dict:
    from .. import verify, vocalization

    try:
        heard = str((vb.transcribe(path, language=language) or {}).get("text") or "")
    except Exception as exc:  # noqa: BLE001 - an unavailable recognizer is unverified
        return {"clean": None, "heard": "", "check": f"unverified: {exc}"[:200]}
    words = verify.compare(text, heard, language)
    extra = vocalization.check(path, text, language, vb)
    return {"heard": heard, "words": words.get("state"),
            "extra": vocalization.describe(extra),
            "clean": words.get("state") != "mismatch" and extra.get("state") != "extra"}


def plan(snapshot: dict, body: AuditionIn) -> dict:
    return select_excerpts(snapshot.get("segments") or [], body.character, body.categories,
                           body.per_category)
