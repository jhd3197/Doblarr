"""An episode broken down line by line, without dubbing it.

An analysis run stops where a dub would start writing: the dialogue is
separated, every line is cut (from subtitles or recognition), grouped by voice
(doblarr.speakers) and measured against its speaker's average level. This
step adds what a person needs to judge a line and a voice bank needs to pick
it:

- the line's pitch (median Hz) and how far it moves (semitones), from the
  separated dialogue, the same measures the clone reference picker ranks by;
- the original-language words. When the lines came from subtitles in another
  language (an English fansub of a Japanese show), recognition on the original dialogue
  gives the Japanese, word by word, and each word joins the line it falls in.

Everything lands in `<stem>.analysis.json` beside the script; the script keeps
the lines, speakers and levels. Nothing here is translated or generated.
"""

from __future__ import annotations

import json
import logging
import math
import statistics
from pathlib import Path

from .fileio import write_json

log = logging.getLogger("doblarr.analysis")

DETECTOR = "line-analysis/1"
RATE = 16000


def _stats(track: list[float]) -> tuple[float | None, float | None]:
    if len(track) < 5:
        return None, None
    centre = statistics.median(track)
    spread = (statistics.pstdev([12 * math.log2(f / centre) for f in track])
              if len(track) >= 10 else None)
    return round(centre, 1), (round(spread, 2) if spread is not None else None)


def voice_measures(samples, spans: list[tuple[float, float]]) -> list[dict]:
    """Pitch and pitch movement of each span of 16 kHz mono samples."""
    from .stages.synthesize import pitch_track_samples

    found = []
    for start, end in spans:
        chunk = samples[int(start * RATE):int(end * RATE)]
        pitch, movement = _stats(pitch_track_samples(chunk, RATE)) if len(chunk) else (None, None)
        found.append({"pitch_hz": pitch, "movement_st": movement})
    return found


def words_by_line(words: list[dict], spans: list[tuple[float, float]],
                  slack: float = 0.25) -> list[str]:
    """Join recognised words to the line whose window holds their midpoint."""
    texts: list[list[str]] = [[] for _ in spans]
    for word in words:
        if word.get("start") is None or word.get("end") is None:
            continue
        mid = (float(word["start"]) + float(word["end"])) / 2
        best, best_gap = None, slack
        for i, (start, end) in enumerate(spans):
            gap = 0.0 if start <= mid <= end else min(abs(mid - start), abs(mid - end))
            if gap <= best_gap:
                best, best_gap = i, gap
                if gap == 0.0:
                    break
        if best is not None:
            texts[best].append(str(word.get("word", "")).strip())
    joiner = ""   # Japanese and Chinese words carry no spaces between them
    return [joiner.join(t) if all(not any(c.isascii() and c.isalpha() for c in w) for w in t)
            else " ".join(t) for t in texts]


def run(job, work_dir: Path, *, whisper_model: str = "large-v3", device=None,
        recognise: bool = True) -> Path:
    """Measure every line and, if needed, recover its original words."""
    from .speakers import _audio
    from .stages.common import work_stem

    audio = job.vocals if job.vocals and Path(job.vocals).is_file() else job.source_audio
    spans = [(seg.start, seg.end) for seg in job.segments]
    samples = _audio(Path(audio))
    measures = voice_measures(samples, spans)
    original: list[str] | None = None
    source_lang = (job.source_lang or "").split("-")[0]
    script_lang = (job.script_lang or "").split("-")[0]
    if recognise and source_lang and script_lang and source_lang != script_lang:
        from .stages.transcribe import faster_whisper_segments

        heard = faster_whisper_segments(job, Path(audio), whisper_model, device)
        words = [w for seg in heard for w in (seg.words or [])]
        original = words_by_line(words, spans)
        log.info("analysis: %d original-language word(s) placed on %d line(s)",
                 len(words), sum(bool(t) for t in original))
    lines = []
    for i, seg in enumerate(job.segments):
        lines.append({"cue": seg.cue_id, "index": seg.index, **measures[i],
                      "original_text": original[i] if original is not None else None})
    path = Path(work_dir) / f"{work_stem(job)}.analysis.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "detector": DETECTOR, "source_lang": job.source_lang, "script_lang": job.script_lang,
        "original_text": original is not None, "lines": lines}, ensure_ascii=False),
        encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# Line features (doblarr.features) and the analysis snapshot (doblarr.snapshots)
# --------------------------------------------------------------------------

def _overlaps(job) -> list[list[str]]:
    """Per line, the known problems that make its curve less trustworthy."""
    from .levels import overlapping

    flags = []
    for seg in job.segments:
        found = []
        if overlapping(job.segments, seg):
            found.append("overlap")
        if seg.measurement.contaminated:
            found.append("contaminated")
        flags.append(found)
    return flags


def features_request(job, audio: Path) -> dict:
    from . import features
    from .artifacts import stamp

    return {"contract": features.describe(), "audio": stamp(Path(audio)),
            "bed": stamp(Path(job.background)) if job.background else None,
            "spans": [[seg.cue_id, round(seg.start, 3), round(seg.end, 3)]
                      for seg in job.segments], "flags": _overlaps(job)}


def run_features(job, work_dir: Path, cancel=None, progress=None,
                 force: bool = False) -> tuple[Path, bool]:
    """Energy curves, pauses and peaks of every line, beside the script.

    Returns the path and whether the saved result was reused: an unchanged
    request (same audio, spans, contract) is never measured twice.
    """
    from . import features
    from .artifacts import digest, read_json
    from .stages.common import work_stem

    audio = job.vocals if job.vocals and Path(job.vocals).is_file() else job.source_audio
    path = Path(work_dir) / f"{work_stem(job)}.features.json"
    request = features_request(job, Path(audio))
    key = digest(request)[:16]
    saved = read_json(path)
    if not force and saved.get("request") == key and saved.get("lines"):
        return path, True
    bed = job.background if job.background and job.background != job.source_audio else None
    measured, stats = features.measure_lines(
        Path(audio), [(seg.start, seg.end) for seg in job.segments], flags=request["flags"],
        bed=Path(bed) if bed else None, cancel=cancel, progress=progress)
    lines = [{"cue": seg.cue_id, "speaker": seg.speaker, **found}
             for seg, found in zip(job.segments, measured, strict=True)]
    payload = {"request": key, "contract": features.describe(), "stats": stats,
               "source": "separated-vocals" if audio == job.vocals else "source-stream",
               "lines": lines}
    write_json(path, payload, indent=None)
    log.info("analysis: features for %d line(s) (%s decoded)", len(lines),
             stats.get("decoded_seconds"))
    return path, False


def stage_inputs(job, stage: str):
    """What one analysis stage was produced from, for staleness checks."""
    from .artifacts import stamp

    spans = [[seg.cue_id, round(seg.start, 3), round(seg.end, 3)] for seg in job.segments]
    labels = [[seg.cue_id, seg.speaker] for seg in job.segments]
    return {
        "probe": lambda: [stamp(job.source_audio)],
        "separate": lambda: [stamp(job.vocals), stamp(job.background)],
        # Spans carry the cue ids (which already encode the source document);
        # `script_ref` itself is filled in later on a fresh run than on a
        # restored one, so including it made identical lines look changed.
        "transcribe": lambda: spans,
        "diarize": lambda: labels,
        "measure": lambda: [[seg.cue_id, seg.measurement.inputs] for seg in job.segments],
        "baselines": lambda: [job.dialogue_baseline, labels],
        "analyze": lambda: [DETECTOR, spans],
        "emotion": lambda: spans,
        "reader": lambda: labels,
        "dub_text": lambda: [stamp(job.source_audio)],
        "features": lambda: features_request(job, Path(job.vocals or job.source_audio
                                                         or "")),
    }[stage]()


def record(db, job, stage: str, state: str = "done", *, outputs: dict | None = None,
           version: str = "", metrics: dict | None = None, error: str = "") -> None:
    """Note one stage of this run in the source revision's snapshot."""
    ident = job.metrics.get("identity") or {}
    if db is None or not ident.get("revision_id"):
        return
    from . import snapshots

    try:
        inputs = stage_inputs(job, stage) if state == "done" else None
    except (OSError, ValueError, KeyError, TypeError):
        inputs = None
    snapshots.record_stage(db, ident["revision_id"], job.source_lang or "", stage, state,
                           inputs=inputs, outputs=outputs, version=version, metrics=metrics,
                           error=error, identity=ident)


def earlier_run(db, work_dir: Path, path: str, ident: dict | None = None) -> dict | None:
    """How an earlier analysis of this content ran: its input path, languages
    and target locale, or None when it was never analysed.

    Every later analysis of the file (a rerun of some stages, "analyze
    again") must run the same way. Work folders are keyed by input, source
    language and target locale, and the subtitles a run reads depend on the
    target: under another target the lines are cut and grouped again.
    """
    from .speaking import locate_script
    from .studio import records

    script = None
    if ident and db is not None:
        for snap in records.list_latest(db, "snapshot", scope=ident["revision_id"]):
            found = ((snap.get("stages") or {}).get("transcribe") or {}).get(
                "outputs", {}).get("script")
            if found and Path(found).is_file():
                script = Path(found)
                break
    if script is None:
        script, how = locate_script(work_dir, path)
        if script is None or how == "ambiguous":
            return None
    recorded = json.loads(script.read_text(encoding="utf-8")).get("identity") or {}
    source = str(recorded.get("input") or "")
    target = str(recorded.get("target_lang") or "")
    folder = script.parent.name
    return {"input_file": source if source and Path(source).is_file() else str(path),
            "source_lang": str(recorded.get("source_lang") or "auto"),
            "target_lang": target or "es",
            "target_locale": folder if "-" in folder and folder.split("-")[0] == target else ""}
