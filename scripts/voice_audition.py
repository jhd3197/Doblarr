#!/usr/bin/env python
"""Audition new ways of voicing a cast on the scenes of an existing comparison.

    # generate every audition take (resumable; rerun to continue)
    python scripts/voice_audition.py generate \
        --from work/benchmarks/quality-final/<comparison>/manifest.json \
        --id 2026-09-22-mushishi-voices \
        --cast work/episode-es-latam-v1/cast.json \
        --vocals work/stems-ja/<episode>.ja.vocals.wav \
        --background work/stems-ja/<episode>.ja.background.wav

    # render the blind listening page once the takes exist
    python scripts/voice_audition.py render --id 2026-09-22-mushishi-voices ...

A comparison (`quality_compare.py`) changes processing and keeps the takes.
This changes the *performance*: each approach below generates its own takes
for exactly the same lines, and the listening page then plays them side by
side through the same processing, with the original a keypress away.

Approaches:

- `current`    the takes the completed run already has. Nothing is generated.
- `directed`   the same preset voices, but every line gets its own acting
               direction, written by the local LLM from the line, its
               neighbours and how loud the original actor was.
- `clone`      each character's voice cloned from their own original
               performance (one clean reference per character, Chatterbox).
- `clone-line` the same clone engine, but each line is cloned from *that
               line's* original audio, so the reading carries its emotion.

In every approach except `current`:

- A cue that is only a reaction ("Tsk!", "Heh heh") is not generated. The
  original actor's own sound is cut from the separated dialogue and used.
- The `dub.pronunciations` map applies to what is spoken, not to captions.
- A take with sound its words do not account for (a giggle, a hum) or with the
  wrong words is regenerated with a new seed, up to `--retakes` times. Every
  attempt is kept on disk and listed in the audition record.

Everything lands in `work/benchmarks/voice-audition/<id>/` and stays local.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import shutil
import statistics
import subprocess
import sys
import time
import wave
from array import array
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from doblarr import comparison, vocalization  # noqa: E402
from doblarr import verify as content  # noqa: E402
from doblarr.clients.voicebox import VoiceboxClient, VoiceboxError  # noqa: E402
from doblarr.config import Config  # noqa: E402
from doblarr.stages.prepare import interjection  # noqa: E402
from doblarr.stages.quality import spoken_form  # noqa: E402

DEFAULT_ROOT = Path("work") / "benchmarks" / "voice-audition"
APPROACHES = ("directed", "clone", "clone-line")
# The processing every version is rendered through: the settings the last
# listening test preferred (`improved`, without the room treatment).
PROCESSING = {
    "boundaries.trim": True,
    "boundaries.edge_fade_ms": 8,
    "timing.mode": "phrase",
    "levels.mode": "follow_source",
    "levels.target_db": -11,
    "coverage.mode": "retain",
}
CLONE_ENGINE = "chatterbox"
# A per-line reference shorter than this teaches the clone too little voice to
# keep the character recognisable; such lines use the character's reference.
LINE_REFERENCE_MIN = 2.0  # voicebox refuses a sample under 2.0 s
CHARACTER_REFERENCE = (4.0, 12.0)
DIRECTION_SYSTEM = (
    "Eres director de doblaje. Para cada línea escribes UNA indicación de actuación "
    "breve en español (6 a 14 palabras): emoción, intención y energía. No repitas el "
    "texto de la línea. No uses comillas. Nunca pidas risas salvo que el original "
    "sea una risa. /no_think")
DIRECTION_EXAMPLES = [
    ["Personaje: viajero seco y reservado.\nOriginal (inglés): Are you the one who "
     "wrote me?\nAnterior: (ninguna)\nActor original: más bajo de lo normal.\n"
     "Línea doblada: ¿Tú eres quien me escribió?",
     "Pregunta tranquila y directa, sin calidez, con curiosidad contenida."],
    ["Personaje: niño curioso y nervioso.\nOriginal (inglés): Wh-what is that "
     "thing?!\nAnterior: Look over there.\nActor original: más fuerte de lo normal.\n"
     "Línea doblada: ¿¡Q-qué es esa cosa!?",
     "Sobresalto con miedo, voz alta y entrecortada, casi un grito."],
]


# --------------------------------------------------------------------------
# Small audio helpers
# --------------------------------------------------------------------------

def ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


def cut(source: Path, start: float, end: float, dest: Path, *, rate: int = 24000,
        fade: float = 0.03) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    length = max(0.1, end - start)
    ffmpeg("-ss", f"{max(0.0, start):.3f}", "-t", f"{length:.3f}", "-i", str(source),
           "-ac", "1", "-ar", str(rate),
           "-af", f"afade=t=in:d={fade},afade=t=out:st={max(0.0, length - fade):.3f}:d={fade}",
           "-c:a", "pcm_s16le", str(dest))
    return dest


def rms_db(path: Path) -> float:
    with wave.open(str(path), "rb") as audio:
        samples = array("h", audio.readframes(audio.getnframes()))
    if not samples:
        return -120.0
    power = sum(v * v for v in samples) / len(samples)
    return 10 * math.log10(max(power, 1.0) / (32768 * 32768))


def median_pitch(path: Path) -> float | None:
    """Median fundamental frequency of the voiced frames, by autocorrelation."""
    import numpy as np

    with wave.open(str(path), "rb") as audio:
        rate = audio.getframerate()
        x = np.frombuffer(audio.readframes(audio.getnframes()), dtype=np.int16).astype(float)
    frame, hop = int(0.04 * rate), int(0.01 * rate)
    peak = np.abs(x).max() + 1
    low, high = int(rate / 400), int(rate / 70)
    found = []
    for i in range(0, len(x) - frame, hop):
        f = x[i:i + frame]
        if np.sqrt((f * f).mean()) < peak * 0.08:
            continue
        f = f - f.mean()
        ac = np.correlate(f, f, "full")[frame - 1:]
        lag = low + int(np.argmax(ac[low:high]))
        if ac[lag] > 0.45 * ac[0]:
            found.append(rate / lag)
    return round(float(np.median(found)), 1) if found else None


def sha(path: Path) -> str:
    import hashlib

    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# --------------------------------------------------------------------------
# What to audition
# --------------------------------------------------------------------------

def load_lines(manifest: dict) -> tuple[list[dict], dict, list[dict]]:
    """Every line in the comparison's scenes, plus the whole script by index."""
    source = manifest["source"]
    payload = comparison.read_script(Path(source["script"]))
    rows = comparison.inventory(payload, Path(source["clips"]))
    by_index = {row["index"]: row for row in rows}
    wanted = [t["index"] for s in manifest["scenes"] for t in s["scene"]["text"]]
    return [by_index[i] for i in wanted], by_index, rows


def neighbours(rows: list[dict], row: dict) -> tuple[str, str]:
    ordered = sorted(rows, key=lambda r: r["start"])
    at = next(i for i, r in enumerate(ordered) if r["index"] == row["index"])
    before = ordered[at - 1] if at > 0 else None
    after = ordered[at + 1] if at + 1 < len(ordered) else None

    def say(r):
        return f"{r['speaker']}: {r['text_src']}" if r else "(ninguna)"

    return say(before), say(after)


def loudness_hints(lines: list[dict], all_rows: list[dict], original: Path,
                   scratch: Path) -> dict:
    """How loud each line's original actor was against their own median."""
    levels: dict[int, float] = {}
    speakers = {row["speaker"] for row in lines}
    sample = [r for r in all_rows if r["speaker"] in speakers]
    for row in sample:
        clip = cut(original, row["start"], row["end"], scratch / f"level_{row['index']}.wav",
                   rate=16000)
        levels[row["index"]] = rms_db(clip)
    medians = {spk: statistics.median([levels[r["index"]] for r in sample
                                       if r["speaker"] == spk])
               for spk in speakers}
    hints = {}
    for row in lines:
        delta = levels[row["index"]] - medians[row["speaker"]]
        word = ("mucho más bajo de lo normal, casi susurrado" if delta < -8 else
                "más bajo de lo normal" if delta < -3.5 else
                "mucho más fuerte de lo normal" if delta > 7 else
                "más fuerte de lo normal" if delta > 3.5 else
                "volumen normal")
        hints[row["index"]] = {"delta_db": round(delta, 1), "hint": word}
    return hints


def character_of(cast: dict, speaker: str) -> str:
    delivery = str((cast.get(speaker) or {}).get("delivery") or "")
    # The cast's standing direction opens with locale guidance; the character
    # is what follows it.
    marker = "Voz "
    return delivery[delivery.find(marker):] if marker in delivery else delivery


# --------------------------------------------------------------------------
# Generation
# --------------------------------------------------------------------------

class Audition:
    def __init__(self, args):
        self.args = args
        self.root = Path(args.out) / args.id
        self.root.mkdir(parents=True, exist_ok=True)
        self.state_path = self.root / "audition.json"
        self.state = (json.loads(self.state_path.read_text(encoding="utf-8"))
                      if self.state_path.is_file() else {})
        self.manifest = json.loads(Path(args.source_manifest).read_text(encoding="utf-8"))
        self.lines, self.by_index, self.all_rows = load_lines(self.manifest)
        cast_payload = json.loads(Path(args.cast).read_text(encoding="utf-8"))
        self.cast = cast_payload.get("speakers", cast_payload)
        self.config = Config.load(args.config)
        self.pronunciations = dict(self.config["dub"].get("pronunciations", {}) or {})
        self.vb = VoiceboxClient(args.voicebox, timeout=3600)
        self.media = Path(self.manifest["source"]["media"])
        self.stream = int(self.manifest["source"]["stream"]["audio_index"])
        self.scratch = self.root / "scratch"
        self.scratch.mkdir(exist_ok=True)
        self.state.setdefault("source_manifest", str(Path(args.source_manifest).resolve()))
        self.state.setdefault("lines", [r["index"] for r in self.lines])
        self.state.setdefault("approaches", {})
        self.state.setdefault("directions", {})
        self.state.setdefault("profiles", {})

    def save(self):
        tmp = self.state_path.with_suffix(".partial.json")
        tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.state_path)

    def log(self, *parts):
        print(time.strftime("%H:%M:%S"), *parts, flush=True)

    # -- sources ------------------------------------------------------------
    def original_mix(self) -> Path:
        dest = self.root / "original.wav"
        if not dest.is_file():
            ffmpeg("-i", str(self.media), "-map", f"0:a:{self.stream}", "-vn", "-ac", "1",
                   "-ar", "24000", "-c:a", "pcm_s16le", str(dest))
        return dest

    def vocals(self) -> Path:
        if not self.args.vocals or not Path(self.args.vocals).is_file():
            raise SystemExit("this approach needs --vocals: the original language's "
                             "separated dialogue (the reference must be the original "
                             "actor, not another dub)")
        return Path(self.args.vocals)

    def has_vocals(self) -> bool:
        return bool(self.args.vocals) and Path(self.args.vocals).is_file()

    # -- directions ---------------------------------------------------------
    def directions(self) -> dict:
        saved = self.state["directions"]
        original = self.vocals() if self.has_vocals() else self.original_mix()
        if "hints" not in self.state:
            self.state["hints"] = {str(k): v for k, v in loudness_hints(
                self.lines, self.all_rows, original, self.scratch).items()}
            self.state["hints_from"] = str(original)
            self.save()
        for row in self.lines:
            key = str(row["index"])
            if key in saved or interjection(row["text_src"]):
                continue
            before, _after = neighbours(self.all_rows, row)
            prompt = (f"Personaje: {row['speaker'].title()}, "
                      f"{character_of(self.cast, row['speaker'])}\n"
                      f"Original (inglés): {row['text_src']}\n"
                      f"Anterior: {before}\n"
                      f"Actor original: {self.state['hints'][key]['hint']}.\n"
                      f"Línea doblada: {row['text_translated']}")
            started = time.time()
            reply = self.vb._post("/llm/generate", json={
                "system": DIRECTION_SYSTEM, "examples": DIRECTION_EXAMPLES,
                "prompt": prompt, "model_size": "4B", "max_tokens": 80,
                "temperature": 0.3})
            text = str(reply.get("text") or "").strip().strip('"').strip()
            text = text.split("\n")[0][:240]
            saved[key] = {"direction": text, "prompt": prompt,
                          "seconds": round(time.time() - started, 1)}
            self.save()
            self.log(f"direction {key} {row['speaker']}: {text}")
        return saved

    # -- voices -------------------------------------------------------------
    def profile(self, name: str, reference: Path, reference_text: str) -> str:
        known = self.state["profiles"].get(name)
        existing = {p["id"] for p in self.vb.voice_profiles()}
        if known and known["id"] in existing and known.get("sha") == sha(reference):
            return known["id"]
        pid = self.vb.create_profile(name, "es", description="Doblarr voice audition")
        self.vb.add_sample(pid, reference, reference_text or "-")
        self.state["profiles"][name] = {"id": pid, "reference": str(reference),
                                        "sha": sha(reference)}
        self.save()
        return pid

    def forget_empty(self, name: str) -> None:
        """Delete a profile left without a sample by a refused upload."""
        for profile in self.vb.voice_profiles():
            if profile.get("name") == name:
                with contextlib.suppress(VoiceboxError):
                    self.vb._request("DELETE", f"/profiles/{profile['id']}")

    def character_reference(self, speaker: str) -> Path:
        """The cleanest single line of this character's original performance."""
        dest = self.root / "references" / f"{speaker}.wav"
        record = self.state.setdefault("character_references", {})
        if dest.is_file() and speaker in record:
            return dest
        low, high = CHARACTER_REFERENCE
        mine = [r for r in self.all_rows if r["speaker"] == speaker
                and low <= r["end"] - r["start"] <= high]
        alone = [r for r in mine if not any(
            o["speaker"] != speaker and o["start"] < r["end"] + 0.3
            and o["end"] > r["start"] - 0.3 for o in self.all_rows)]
        candidates = alone or mine
        if not candidates:
            raise SystemExit(f"no usable reference line for {speaker}")
        background = Path(self.args.background) if self.args.background else None
        scored = []
        for row in sorted(candidates, key=lambda r: abs((r["end"] - r["start"]) - 8.0))[:8]:
            voice = cut(self.vocals(), row["start"], row["end"],
                        self.scratch / f"ref_{row['index']}.wav")
            bed = (rms_db(cut(background, row["start"], row["end"],
                              self.scratch / f"bed_{row['index']}.wav"))
                   if background else -90.0)
            scored.append((rms_db(voice) - bed, row, voice))
        margin, row, voice = max(scored, key=lambda item: item[0])
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(voice, dest)
        record[speaker] = {"line": row["index"], "text": row["text_src"],
                           "seconds": round(row["end"] - row["start"], 2),
                           "voice_over_bed_db": round(margin, 1)}
        self.save()
        self.log(f"reference {speaker}: line {row['index']} "
                 f"({record[speaker]['seconds']}s, {margin:.1f} dB over the bed)")
        return dest

    # -- one take -----------------------------------------------------------
    def spoken(self, row: dict) -> str:
        return spoken_form(row["text_translated"], self.pronunciations)

    def judge(self, path: Path, text: str) -> dict:
        heard = str((self.vb.transcribe(path, language="es") or {}).get("text") or "")
        verdict = content.compare(text, heard, "es")
        extra = vocalization.check(path, text, "es", self.vb)
        clean = verdict.get("state") != "mismatch" and extra["state"] != "extra"
        return {"heard": heard, "words": verdict.get("state"),
                "similarity": verdict.get("similarity"),
                "extra": vocalization.describe(extra), "clean": clean}

    def generate(self, approach: str, row: dict, request: dict, dest: Path) -> dict:
        receipt = dest.with_suffix(".json")
        if dest.is_file() and receipt.is_file():
            saved = json.loads(receipt.read_text(encoding="utf-8"))
            asked = {k: v for k, v in saved.get("request", {}).items() if k != "text"}
            if asked == request:
                return saved
        attempts = []
        base_seed = int(request["options"].get("seed") or 0)
        for attempt in range(self.args.retakes + 1):
            path = dest.with_name(f"{dest.stem}.t{attempt}.wav")
            options = dict(request["options"], seed=base_seed + attempt * 101)
            started = time.time()
            self.vb.synthesize_to_file(request["profile"], request["tts_text"], "es",
                                       path, **{k: v for k, v in options.items()
                                                if v is not None})
            judged = self.judge(path, request["tts_text"])
            judged.update(attempt=attempt, seed=options["seed"], path=str(path),
                          seconds=round(time.time() - started, 1))
            attempts.append(judged)
            self.log(f"  {approach} line {row['index']} attempt {attempt}: "
                     f"{'clean' if judged['clean'] else 'FLAGGED'} "
                     f"({judged['seconds']}s) heard “{judged['heard']}” {judged['extra']}")
            if judged["clean"]:
                break
        chosen = next((a for a in attempts if a["clean"]), None) or max(
            attempts, key=lambda a: (not a["extra"], a["similarity"] or 0))
        shutil.copy2(chosen["path"], dest)
        payload = {
            # `text` is what the line *says* (captions, recognition); the
            # respelled form sent to the engine is kept beside it.
            "request": {"text": row["text_translated"], **request},
            "sha256": sha(dest), "attempts": attempts, "chosen": chosen["attempt"],
        }
        receipt.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                           encoding="utf-8")
        return payload

    def retain(self, row: dict, dest: Path) -> dict:
        """The original actor's own reaction, cut from the separated dialogue."""
        cut(self.vocals(), row["start"] - 0.05, row["end"] + 0.05, dest, rate=48000,
            fade=0.02)
        payload = {"request": {"text": row["text_translated"], "engine": "retained-original",
                               "options": {"source": str(self.vocals()),
                                           "start": row["start"], "end": row["end"]}},
                   "sha256": sha(dest), "retained": True}
        dest.with_suffix(".json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return payload

    # -- recasting one character ----------------------------------------------
    def candidate_references(self, speaker: str) -> list[dict]:
        """This character's clean, solo lines of usable length, with their pitch."""
        low, high = CHARACTER_REFERENCE
        rows = [r for r in self.all_rows if r["speaker"] == speaker
                and low <= r["end"] - r["start"] <= high
                and not any(o["speaker"] != speaker and o["start"] < r["end"] + 0.3
                            and o["end"] > r["start"] - 0.3 for o in self.all_rows)]
        found = []
        for row in rows:
            clip = cut(self.vocals(), row["start"], row["end"],
                       self.scratch / f"ref_{row['index']}.wav")
            pitch = median_pitch(clip)
            if pitch:
                found.append({"row": row, "clip": clip, "pitch": pitch})
        return found

    def recast_reference(self, speaker: str, mode: str) -> tuple[Path, dict]:
        """A reference chosen to sit lower: one line (`low`) or three (`blend`)."""
        dest = self.root / "references" / f"{speaker}.{mode}.wav"
        found = sorted(self.candidate_references(speaker), key=lambda c: c["pitch"])
        if not found:
            raise SystemExit(f"no usable reference line for {speaker}")
        chosen = found[:1] if mode == "low" else found[:3]
        dest.parent.mkdir(parents=True, exist_ok=True)
        if len(chosen) == 1:
            shutil.copy2(chosen[0]["clip"], dest)
        else:
            # Joined with a short silence so the clone hears three calm
            # readings, not one line whose delivery sets the whole voice.
            inputs = []
            for c in chosen:
                inputs += ["-i", str(c["clip"])]
            parts = "".join(f"[{i}]apad=pad_dur=0.3[a{i}];" for i in range(len(chosen)))
            joined = "".join(f"[a{i}]" for i in range(len(chosen)))
            ffmpeg(*inputs, "-filter_complex",
                   f"{parts}{joined}concat=n={len(chosen)}:v=0:a=1,atrim=0:14",
                   "-ac", "1", "-ar", "24000", "-c:a", "pcm_s16le", str(dest))
        record = {"mode": mode, "lines": [c["row"]["index"] for c in chosen],
                  "pitch": [c["pitch"] for c in chosen],
                  "text": [c["row"]["text_src"] for c in chosen],
                  "character_median_pitch": round(statistics.median(
                      c["pitch"] for c in found), 1)}
        self.log(f"recast {speaker} ({mode}): lines {record['lines']} at "
                 f"{record['pitch']} Hz; character median {record['character_median_pitch']} Hz")
        return dest, record

    def recast(self, base: str, speaker: str, mode: str) -> str:
        """A copy of `base` in which only `speaker`'s generated lines are redone."""
        name = f"{base}-{speaker.lower()}-{mode}"
        source = Path(self.state["approaches"][base]["takes"])
        takes = self.root / "takes" / name
        takes.mkdir(parents=True, exist_ok=True)
        reference, record = self.recast_reference(speaker, mode)
        profile = self.profile(f"Doblarr audition {speaker} {mode}", reference,
                               " ".join(record["text"])[:300])
        summary = self.state["approaches"].setdefault(name, {})
        summary.update(takes=str(takes.resolve()), base=base, speaker=speaker,
                       reference=record)
        for row in self.lines:
            dest = takes / f"line_{row['index']:04d}.wav"
            if row["speaker"] != speaker or interjection(row["text_src"]):
                for suffix in (".wav", ".json"):
                    shutil.copy2(source / f"line_{row['index']:04d}{suffix}",
                                 dest.with_suffix(suffix))
                continue
            result = self.generate(name, row, {
                "engine": CLONE_ENGINE, "profile": profile,
                "reference": str(reference), "tts_text": self.spoken(row),
                "options": {"engine": CLONE_ENGINE, "seed": 3197},
            }, dest)
            chosen = next((a for a in result.get("attempts", [])
                           if a["attempt"] == result.get("chosen")), {})
            summary[str(row["index"])] = {"clean": bool(chosen.get("clean")),
                                          "attempts": len(result.get("attempts", [])),
                                          "pitch": median_pitch(dest)}
            self.save()
        return name

    # -- approaches ---------------------------------------------------------
    def run(self, approach: str) -> None:
        takes = self.root / "takes" / approach
        takes.mkdir(parents=True, exist_ok=True)
        summary = self.state["approaches"].setdefault(approach, {})
        summary["takes"] = str(takes.resolve())
        directions = self.directions() if approach == "directed" else {}
        for row in self.lines:
            dest = takes / f"line_{row['index']:04d}.wav"
            speaker = row["speaker"]
            if interjection(row["text_src"]):
                if not self.has_vocals():
                    self.log(f"{approach} line {row['index']}: reaction waits for the "
                             f"separated original; rerun once it exists")
                    continue
                result = self.retain(row, dest)
                self.log(f"{approach} line {row['index']} {speaker}: kept the original "
                         f"reaction for “{row['text_src']}”")
            elif approach == "directed":
                entry = self.cast.get(speaker) or {}
                direction = directions.get(str(row["index"]), {}).get("direction", "")
                standing = str(entry.get("delivery") or "")
                result = self.generate(approach, row, {
                    "engine": "qwen_custom_voice", "profile": entry["voice"],
                    "tts_text": self.spoken(row),
                    "options": {"engine": "qwen_custom_voice", "model_size": "1.7B",
                                "seed": 3197,
                                "instruct": f"{standing} {direction}".strip()[:500]},
                }, dest)
            else:
                reference = self.character_reference(speaker)
                name = f"Doblarr audition {speaker}"
                length = row["end"] - row["start"]
                if approach == "clone-line" and length >= LINE_REFERENCE_MIN:
                    reference = cut(self.vocals(), row["start"] - 0.05, row["end"] + 0.05,
                                    self.root / "references" / f"line_{row['index']:04d}.wav")
                    name = f"Doblarr audition {speaker} line {row['index']}"
                try:
                    profile = self.profile(name, reference, row["text_src"])
                except VoiceboxError as exc:
                    if "too short" not in str(exc):
                        raise
                    # The service measures the voiced part, which can fall
                    # under its minimum even when the cut does not.
                    self.log(f"  {approach} line {row['index']}: line reference too "
                             f"short for voicebox; using the character's")
                    self.forget_empty(name)
                    reference = self.character_reference(speaker)
                    profile = self.profile(f"Doblarr audition {speaker}", reference,
                                           row["text_src"])
                result = self.generate(approach, row, {
                    "engine": CLONE_ENGINE, "profile": profile,
                    "reference": str(reference), "tts_text": self.spoken(row),
                    "options": {"engine": CLONE_ENGINE, "seed": 3197},
                }, dest)
            chosen = next((a for a in result.get("attempts", [])
                           if a["attempt"] == result.get("chosen")), {})
            summary[str(row["index"])] = {
                "clean": bool(result.get("retained") or chosen.get("clean")),
                "attempts": len(result.get("attempts", [])),
                "retained": bool(result.get("retained")),
            }
            self.save()


def render(args) -> int:
    root = Path(args.out) / args.id
    state = json.loads((root / "audition.json").read_text(encoding="utf-8"))
    manifest = json.loads(Path(state["source_manifest"]).read_text(encoding="utf-8"))
    source = manifest["source"]
    wanted = [v for v in (args.versions or "").split(",") if v] or [
        "current", *APPROACHES]
    variants = {}
    for approach in wanted:
        if approach == "current":
            variants["current"] = dict(PROCESSING)
            continue
        takes = state["approaches"].get(approach, {}).get("takes")
        if takes:
            variants[approach] = {**PROCESSING, "takes": takes}
    keep = {int(i) for i in (args.scenes or "").split(",") if i != ""}
    scenes = [s for i, s in enumerate(manifest["scenes"]) if not keep or i in keep]
    windows = [(s["scene"]["window"]["start"], s["scene"]["window"]["end"])
               for s in scenes]
    titles = {i: s["scene"]["title"] for i, s in enumerate(scenes)}
    stems = ({"vocals": args.vocals, "background": args.background}
             if args.vocals and args.background else dict(source.get("stems") or {}))
    config = Config.load(args.config)
    result = comparison.run(
        config, comparison_id=args.page_id or f"{args.id}-page",
        source=Path(source["media"]), script=Path(source["script"]),
        clips=Path(source["clips"]), windows=windows, titles=titles,
        variants=variants, root=root, stems=stems,
        source_lang=source["source_language"], target_lang=source["target_language"],
        target_locale=source.get("target_locale", ""),
        note=("Voice audition: each version is a different way of voicing the same "
              "lines, rendered through the same processing. The original is the "
              "Japanese track; the reference is the English dub."))
    page = root / (args.page_id or f"{args.id}-page") / "index.html"
    print(f"page: {page}")
    for finding in result["take_findings"]:
        print("  !", finding)
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("generate", "render"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--id", required=True)
        cmd.add_argument("--out", type=Path, default=DEFAULT_ROOT)
        cmd.add_argument("--config", type=Path, default=Path("config.yaml"))
        cmd.add_argument("--vocals", help="the ORIGINAL language's separated dialogue")
        cmd.add_argument("--background", help="the original language's separated bed")
    gen = sub.choices["generate"]
    gen.add_argument("--from", dest="source_manifest", required=True, type=Path)
    gen.add_argument("--cast", required=True, type=Path)
    gen.add_argument("--voicebox", default="http://127.0.0.1:17493")
    gen.add_argument("--approach", action="append", choices=APPROACHES,
                     help="repeatable; default all")
    gen.add_argument("--retakes", type=int, default=2)
    sub.choices["render"].add_argument("--page-id", default="")
    sub.choices["render"].add_argument("--versions", default="",
                                       help="comma-separated approaches; default all")
    sub.choices["render"].add_argument("--scenes", default="",
                                       help="comma-separated scene indexes; default all")
    gen.add_argument("--recast", metavar="BASE:SPEAKER:MODE", action="append",
                     help="redo one character's lines in BASE with a `low` or "
                          "`blend` reference, e.g. clone:SHINRA:low; repeatable")
    args = parser.parse_args(argv)
    if args.command == "render":
        return render(args)
    audition = Audition(args)
    if args.recast:
        for spec in args.recast:
            base, speaker, mode = spec.split(":")
            if mode not in ("low", "blend"):
                parser.error("--recast MODE is low or blend")
            audition.log(f"== recast {spec}")
            audition.recast(base, speaker, mode)
        audition.log("done")
        return 0
    for approach in args.approach or APPROACHES:
        audition.log(f"== {approach}")
        audition.run(approach)
    audition.log("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
