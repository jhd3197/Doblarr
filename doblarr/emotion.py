"""How each line is said: the feeling, from the voice and from the picture.

Two readers, kept apart and then put side by side:

- The voice: an emotion model (emotion2vec, through FunASR) on the line's
  separated dialogue. On an anime test episode it heard how strongly a line
  is said well and what is felt less well (an excited "It went perfectly!"
  came out angry, a begging "Don't go!" happy), so it is the second opinion.
- The picture: a model that sees (``analysis.emotion_model``, a local
  ``ollama/qwen3-vl:8b`` by default) looks at a still from the middle of the
  line with the line's words and says the expression and the feeling; on the
  same episode it read faces correctly ("excited, mouth open").

The fused answer takes the picture's feeling when it saw a face, and says
whether the voice agrees; a line nobody could read stays unknown. Results
land in ``<stem>.emotion.json`` beside the script and are reused while the
lines and models are unchanged. Nothing is generated or sent to a service
other than the configured local model.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from .ffmpeg import FFmpegError, run_ffmpeg_bytes
from .fileio import write_json

log = logging.getLogger("doblarr.emotion")

READER = "line-emotion/1"
VOICE_MODEL = "emotion2vec/emotion2vec_plus_large"
FEELINGS = ("neutral", "happy", "excited", "angry", "sad", "afraid", "surprised", "tired",
            "embarrassed", "smug")
# What the voice model calls a feeling, in the picture reader's words.
VOICE_WORDS = {"neutral": "neutral", "happy": "happy", "angry": "angry", "sad": "sad",
               "fearful": "afraid", "surprised": "surprised", "disgusted": "angry"}
# Feelings close enough to count as agreeing (a shout read as anger or excitement).
NEAR = {frozenset(p) for p in (("excited", "happy"), ("excited", "angry"),
                               ("afraid", "surprised"), ("tired", "sad"))}
MIN_SECONDS = 0.6

SYSTEM = """You see one still from an anime episode, taken while a line of
dialogue is spoken, and the line itself. Say what the picture shows about how
the line is said. If no face is visible, or the speaker is clearly not on
screen, say so and give the feeling from the line's words alone with low
confidence. Also copy any text drawn in the picture itself (a title card, a
caption naming someone, a sign); leave out subtitles of the dialogue, and give
"" when there is none."""


class Seen(BaseModel):
    face_visible: bool
    speaker_on_screen: bool | None
    expression: str = Field(max_length=120)
    feeling: Literal["neutral", "happy", "excited", "angry", "sad", "afraid", "surprised",
                     "tired", "embarrassed", "smug"]
    # 0 to 1; models often answer on a 0-10 scale, which is read as tenths.
    intensity: float = Field(ge=0.0, le=10.0)
    confidence: Literal["high", "medium", "low"]
    # Text drawn on the picture (a title card, a caption naming someone, a
    # sign), never the subtitles: names on screen are evidence of who is who.
    text_on_screen: str = Field(max_length=200)


def still(video: Path, t: float, height: int = 360) -> bytes:
    """One JPEG frame of the video at `t` seconds."""
    try:
        return run_ffmpeg_bytes(
            ["-v", "error", "-ss", f"{t:.3f}", "-i", str(video), "-frames:v", "1", "-an",
             "-sn", "-vf", f"scale=-2:{height}", "-q:v", "4", "-f", "image2", "-c:v", "mjpeg",
             "-"], timeout=120)
    except FFmpegError as exc:
        log.warning("emotion: no frame at %.1f s: %s", t, exc)
        return b""


def voice(vocals: Path, lines: list[dict], device: str = "cpu", cancel=None) -> list[dict | None]:
    """The voice model's reading per line, or None (too short / model missing)."""
    try:
        import soundfile as sf
        from funasr import AutoModel
        from scipy.signal import resample_poly
    except ImportError:
        log.info("emotion: FunASR is not installed; the voice is not read")
        return [None] * len(lines)
    model = AutoModel(model=VOICE_MODEL, hub="hf", disable_update=True, device=device)
    rate = sf.info(str(vocals)).samplerate
    out: list[dict | None] = []
    for line in lines:
        if cancel is not None and cancel.is_set():
            out.append(None)
            continue
        start, end = float(line["start"]), float(line["end"])
        if end - start < MIN_SECONDS:
            out.append(None)
            continue
        audio, _ = sf.read(str(vocals), start=int(start * rate), stop=int(end * rate),
                           dtype="float32")
        if audio.ndim > 1:
            audio = audio.mean(axis=1)
        if rate != 16000:
            audio = resample_poly(audio, 16000, rate)
        result = model.generate(audio, granularity="utterance", extract_embedding=False,
                                disable_pbar=True)[0]
        scores = {str(label).split("/")[-1]: round(float(score), 3)
                  for label, score in zip(result["labels"], result["scores"], strict=True)}
        heard = max((k for k in scores if k in VOICE_WORDS), key=lambda k: scores[k],
                    default=None)
        out.append({"feeling": VOICE_WORDS.get(heard or "", ""),
                    "score": scores.get(heard or "", 0.0), "raw": heard or "", "scores": scores})
    return out


def picture(video: Path, lines: list[dict], client, *, cancel=None,
            progress=None) -> list[dict | None]:
    """The seeing model's reading per line, or None (too short / unreadable).

    The middle of a line often shows a wide shot or someone else; when it
    shows no face, a moment near its start is tried too."""
    out: list[dict | None] = []
    for n, line in enumerate(lines):
        if cancel is not None and cancel.is_set():
            out.append(None)
            continue
        start, end = float(line["start"]), float(line["end"])
        if end - start < MIN_SECONDS:
            out.append(None)
            continue
        payload = {"line": line.get("text") or "", "original": line.get("original_text") or "",
                   "speaker": line.get("character") or "unknown"}
        best = None
        for t in ((start + end) / 2, start + 0.25):
            frame = still(video, t)
            if not frame:
                continue
            try:
                seen = client.ask(Seen, SYSTEM, payload, max_tokens=400, images=[frame])
            except Exception as exc:  # noqa: BLE001 - one unreadable line never stops the rest
                log.warning("emotion: line at %.1f s not read (%s)", start, exc)
                break
            best = {**seen.model_dump(), "at": round(t, 2)}
            if best["intensity"] > 1.0:
                best["intensity"] = round(best["intensity"] / 10.0, 2)
            if seen.face_visible:
                break
        out.append(best)
        if progress is not None:
            progress(n + 1, len(lines), f"line {n + 1} of {len(lines)}")
    return out


def fuse(heard: dict | None, seen: dict | None) -> dict:
    """One feeling per line, with how sure and whether the readers agree.

    Only the speaker's face gives a feeling: without it the line stays unknown, with the
    voice's and the words' readings kept as hints (on the test episode the
    voice alone was wrong three times in four)."""
    hints = {"voice": (heard or {}).get("feeling") or "",
             "words": (seen or {}).get("feeling") or "" if seen and not seen.get("face_visible")
             else ""}
    # The face on screen is often someone else's (a reaction shot): only the
    # speaker's own face says how the line is said.
    speaking = (seen or {}).get("speaker_on_screen") is True
    if seen and seen.get("face_visible") and speaking and seen.get("confidence") != "low":
        feeling = seen["feeling"]
        agree = None
        if heard and heard.get("feeling"):
            agree = heard["feeling"] == feeling or frozenset((heard["feeling"], feeling)) in NEAR
        confidence = "high" if agree else "medium" if agree is None else "low"
        return {"feeling": feeling, "intensity": seen.get("intensity"), "confidence": confidence,
                "agree": agree, "from": "picture", "hints": hints}
    return {"feeling": "", "intensity": None, "confidence": "none", "agree": None, "from": "",
            "hints": hints}


def run(job, work: Path, model: str, *, names: dict[str, str] | None = None,
        device: str = "cpu", cancel=None, progress=None, force: bool = False) -> tuple[Path, bool]:
    """Read every line's feeling and keep it beside the script; reuse when unchanged."""
    from . import llm
    from .artifacts import digest, read_json, stamp
    from .stages.common import work_stem

    path = Path(work) / f"{work_stem(job)}.emotion.json"
    vocals = Path(job.vocals) if job.vocals and Path(job.vocals).is_file() else None
    original = {row.get("cue"): row.get("original_text") or "" for row in read_json(
        Path(work) / f"{work_stem(job)}.analysis.json").get("lines") or []}
    lines = [{"cue": seg.cue_id, "start": seg.start, "end": seg.end, "text": seg.text_src,
              "character": (names or {}).get(seg.speaker or "", ""),
              "original_text": original.get(seg.cue_id, "")} for seg in job.segments]
    key = digest({"reader": READER, "model": model, "voice": VOICE_MODEL,
                  "audio": stamp(vocals) if vocals else None,
                  "lines": [[ln["cue"], round(ln["start"], 2), round(ln["end"], 2), ln["text"]]
                            for ln in lines]})[:16]
    saved = read_json(path)
    if not force and saved.get("request") == key and saved.get("lines"):
        return path, True
    heard = voice(vocals, lines, device, cancel) if vocals else [None] * len(lines)
    reader = llm.Client(model, timeout=300, think=False)
    seen = picture(Path(job.input_file), lines, reader, cancel=cancel, progress=progress)
    rows = [{"cue": ln["cue"], "voice": h, "picture": s, **fuse(h, s)}
            for ln, h, s in zip(lines, heard, seen, strict=True)]
    payload = {"request": key, "reader": READER, "model": model, "voice_model": VOICE_MODEL,
               "lines": rows}
    write_json(path, payload, indent=None)
    log.info("emotion: %d line(s) read, %d agreed", len(rows),
             sum(1 for r in rows if r.get("agree")))
    return path, False
