"""Check the track that was actually delivered, not the mix that produced it.

D10 in one sentence: every stage before this one measured its own output, and
none of them has heard the file the encoder and the muxer produced. Encoding is
lossy, muxing reorders streams, a container carries its own start timestamps,
and "the render finished" has never been the same statement as "the added track
is correct". This module opens the finished file and looks.

What it deliberately does not do:

- **It does not invent a standard.** With no loudness target configured, the
  loudness and true peak are *measured and reported*, never graded. A number
  without a stated target is evidence, and calling it a pass against an
  unspecified standard would be the most useless kind of green tick. Surround,
  broadcast and ADM compliance are out of scope and are never claimed.
- **It does not assume the dub is the first audio stream.** The added track is
  identified by the language tag, the title and the index the muxer reported,
  and a file where those disagree is a finding rather than a measurement of
  whichever stream happened to be first.
- **It does not compare lossy output byte for byte.** AAC will not reproduce
  the mix sample for sample and pretending otherwise would fail every correct
  export. Placement is checked as *content in the window where dialogue was
  scheduled*, with an explicit encoder-delay tolerance.
- **It does not turn silence into a defect.** A silent scene, a credits roll
  and a background-only interval are all intentional. Only windows where this
  run actually placed a clip are expected to contain anything.

Severity is not confidence and both are recorded. A structural failure — a
missing stream, a truncated track, a decode error — is a fact. A loudness
reading against no target is an observation with no verdict attached at all.
"""

from __future__ import annotations

import json
import logging
import math
import re
import wave
from pathlib import Path

from .artifacts import digest
from .ffmpeg import FFmpegError, run_ffmpeg, run_ffmpeg_stderr, run_ffprobe
from .levels import analyze as analyze_levels

log = logging.getLogger("doblarr.delivery")

# Bump when the checks or their severities change: a saved report has to say
# which revision of the profile produced it, so an old report is never reread
# as if today's rules had been applied to it.
PROFILE_VERSION = "delivery/1"
DETECTOR = "delivery/1"

# EBU R128 / ITU-R BS.1770-4 as implemented by FFmpeg's ebur128 filter with
# 4x-oversampled true peak. Named on every measurement, because a peak number
# without its meter is not comparable with anyone else's peak number.
METER = "ffmpeg-ebur128/bs1770-4"
# How often the filter prints a decode position. The last one is used as the
# dub's decoded length, so this is also the resolution of that measurement.
METER_INTERVAL = 0.1

# A decoded window whose speech-active level sits below this is silent for the
# purpose of "did the dialogue survive the encode".
SILENCE_DB = -50.0
# Samples at or above this are treated as clipped.
CLIP_PEAK = 0.999

# Severities, in the order a report should present them.
SEVERITIES = ("failure", "warning", "info")


def settings(options: dict | None) -> dict:
    """Effective delivery settings, with every default written down once.

    `mode` is the only thing that decides whether a finding can stop a
    publication: `measure` records everything and blocks nothing, `enforce`
    turns a structural failure into a withheld version.
    """
    values = dict(options or {})
    mode = str(values.get("mode", "measure"))
    if mode not in ("off", "measure", "enforce"):
        log.warning("delivery: unknown mode %r, measuring only", mode)
        mode = "measure"
    return {
        "mode": mode,
        "profile": str(values.get("profile", "local")),
        "sample_rate": max(0, int(values.get("sample_rate", 0) or 0)),
        "channels": max(0, int(values.get("channels", 0) or 0)),
        "duration_tolerance": max(0.0, float(values.get("duration_tolerance", 1.0))),
        "start_tolerance": max(0.0, float(values.get("start_tolerance", 0.10))),
        # None means "no target was chosen", which yields a measurement and no
        # verdict. It is not the same as a target of zero.
        "target_lufs": _optional_number(values.get("target_lufs")),
        "lufs_tolerance": max(0.0, float(values.get("lufs_tolerance", 2.0))),
        "true_peak_db": _optional_number(values.get("true_peak_db")),
        "placement_samples": max(0, min(12, int(values.get("placement_samples", 3)))),
        "silence_db": float(values.get("silence_db", SILENCE_DB)),
        "check_original_streams": bool(values.get("check_original_streams", True)),
    }


def _optional_number(value):
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def profile_id(config: dict) -> str:
    """Stable id of the resolved profile, so a report names what graded it."""
    graded = {k: config[k] for k in sorted(config) if k != "mode"}
    return f"{config['profile']}:{digest({'version': PROFILE_VERSION, **graded})[:12]}"


def describe(config: dict) -> dict:
    """The profile as a report and the UI should show it, targets and all."""
    return {
        "version": PROFILE_VERSION,
        "id": profile_id(config),
        "name": config["profile"],
        "mode": config["mode"],
        "sample_rate": config["sample_rate"] or None,
        "channels": config["channels"] or None,
        "duration_tolerance": config["duration_tolerance"],
        "start_tolerance": config["start_tolerance"],
        "target_lufs": config["target_lufs"],
        "lufs_tolerance": config["lufs_tolerance"],
        "true_peak_db": config["true_peak_db"],
        "meter": METER,
        "note": ("Loudness and true peak are measured and reported; no target is "
                 "configured, so neither is graded."
                 if config["target_lufs"] is None and config["true_peak_db"] is None
                 else "Loudness and true peak are graded against this local profile "
                      "only. No broadcast or platform compliance is claimed."),
    }


# --------------------------------------------------------------------------
# Probing
# --------------------------------------------------------------------------

def probe(path: Path, cancel=None) -> dict:
    """Container and stream layout of an exported file, as ffprobe reports it."""
    raw = run_ffprobe([
        "-v", "error", "-show_entries",
        "format=duration,format_name,size:"
        "stream=index,codec_type,codec_name,sample_rate,channels,channel_layout,"
        "start_time,duration,bit_rate:stream_tags=language,title",
        "-of", "json", str(path)], cancel=cancel)
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise FFmpegError(f"ffprobe returned no readable layout for {path.name}") from exc


def audio_streams(layout: dict) -> list[dict]:
    """The audio streams, in container order, with their audio-relative index."""
    rows: list[dict] = []
    for stream in layout.get("streams", []):
        if stream.get("codec_type") != "audio":
            continue
        tags = stream.get("tags") or {}
        rows.append({
            "index": stream.get("index"),
            "audio_index": len(rows),
            "codec": stream.get("codec_name"),
            "sample_rate": _int(stream.get("sample_rate")),
            "channels": _int(stream.get("channels")),
            "layout": stream.get("channel_layout") or "",
            "start_time": _float(stream.get("start_time")),
            "duration": _float(stream.get("duration")),
            "bit_rate": _int(stream.get("bit_rate")),
            "language": str(tags.get("language") or ""),
            "title": str(tags.get("title") or ""),
        })
    return rows


def find_dub(layout: dict, expected: dict) -> tuple[dict | None, str]:
    """Identify the added dub stream, and say how it was identified.

    The muxer knows which audio index it appended, so that is checked first.
    Language and title are used to confirm it — never on their own to guess,
    because an original track can carry the same language tag as the dub.
    """
    rows = audio_streams(layout)
    if not rows:
        return None, "the export has no audio streams at all"
    wanted_index = expected.get("audio_index")
    language = str(expected.get("language") or "")
    title = str(expected.get("title") or "")
    if isinstance(wanted_index, int) and 0 <= wanted_index < len(rows):
        row = rows[wanted_index]
        if (not language or row["language"] == language) and \
                (not title or row["title"] == title):
            return row, f"audio stream {wanted_index} carries the expected language and title"
        return None, (
            f"audio stream {wanted_index} is where the dub was added, but it is "
            f"tagged {row['language'] or 'untagged'}/{row['title'] or 'untitled'} "
            f"rather than {language}/{title}")
    matches = [row for row in rows
               if (not language or row["language"] == language)
               and (not title or row["title"] == title)]
    if len(matches) == 1:
        return matches[0], "one audio stream carries the expected language and title"
    if not matches:
        return None, (f"no audio stream is tagged {language}/{title}; the dub is "
                      f"not in this file under the name it was given")
    return None, (f"{len(matches)} audio streams carry {language}/{title}; the dub "
                  f"cannot be identified without ambiguity")


def measure_loudness(path: Path, audio_index: int, cancel=None) -> dict:
    """Integrated loudness and true peak of one stream, with the meter named.

    Uses FFmpeg's `ebur128` with oversampled true-peak detection. A limiter
    ceiling is a request and a sample peak is a different measurement; neither
    is reported here as true peak.
    """
    try:
        result = run_ffmpeg_stderr([
            "-nostats", "-i", str(path), "-map", f"0:a:{audio_index}",
            "-af", "ebur128=peak=true", "-f", "null", "-"], cancel=cancel)
    except FFmpegError as exc:
        return {"meter": METER, "state": "failed", "reason": str(exc),
                "lufs": None, "true_peak_db": None, "lra": None}
    head, _, summary = result.rpartition("Summary:")
    # The filter prints a position every `METER_INTERVAL` seconds as it decodes.
    # The last one is how far it actually got, which is the dub's real length.
    positions = re.findall(r"^\[Parsed_ebur128.*?\bt:\s*(\d+(?:\.\d+)?)",
                           head or result, re.MULTILINE)
    decoded = (round(float(positions[-1]) + METER_INTERVAL, 3) if positions else None)
    lufs = _search(r"I:\s*(-?\d+(?:\.\d+)?)\s*LUFS", summary)
    peak = _search(r"True peak:\s*\n\s*Peak:\s*(-?\d+(?:\.\d+)?)\s*dBFS", summary)
    lra = _search(r"LRA:\s*(-?\d+(?:\.\d+)?)\s*LU", summary)
    if lufs is None:
        return {"meter": METER, "state": "unavailable", "lufs": None,
                "true_peak_db": peak, "lra": lra, "decoded": decoded,
                "reason": "the meter produced no integrated loudness"}
    return {"meter": METER, "state": "measured", "lufs": lufs,
            "true_peak_db": peak, "lra": lra, "decoded": decoded, "reason": ""}


def decode_window(path: Path, audio_index: int, start: float, end: float,
                  dest: Path, cancel=None) -> Path:
    """Decode one interval of one stream to 16-bit mono PCM for measurement."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    run_ffmpeg(["-y", "-ss", f"{max(0.0, start):g}", "-i", str(path),
                "-map", f"0:a:{audio_index}", "-t", f"{max(0.02, end - start):g}",
                "-vn", "-ac", "1", "-ar", "48000", "-c:a", "pcm_s16le",
                str(dest)], cancel=cancel)
    return dest


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------

def validate(job, options: dict | None = None, cancel=None,
             work_dir: Path | None = None) -> dict:
    """Open the exported file and report what the delivered dub actually is.

    Returns the report and also writes it onto `job.delivery`. Nothing here
    changes media: a failure withholds a claim, it never edits the export and
    never touches the original.
    """
    config = settings(options)
    report: dict = {
        "state": "skipped",
        # Set on every return path below, never left to a caller's default: the
        # pipeline reads this to decide whether to save a version, and a hard
        # failure that forgot to say "no" would publish itself.
        "publishable": True,
        "profile": describe(config),
        "output": None,
        "output_sha256": None,
        "stream": None,
        "identification": "",
        "loudness": {},
        "placement": [],
        "findings": [],
        "checked": 0,
    }
    if config["mode"] == "off":
        report["reason"] = "export validation is off for this run"
        job.delivery = report
        return report
    output = job.output_file
    if output is None or not Path(output).is_file():
        report["state"] = "unavailable"
        report["reason"] = "there is no exported file to check"
        job.delivery = report
        return report
    output = Path(output)
    report["output"] = str(output)
    findings: list[dict] = []
    try:
        layout = probe(output, cancel)
    except FFmpegError as exc:
        findings.append(_finding("export_unreadable", "failure",
                                 {"error": str(exc)},
                                 "the exported container could not be read"))
        report.update(state="failed", findings=findings,
                      publishable=config["mode"] != "enforce")
        job.delivery = report
        return report

    expected = dict(job.metrics.get("mux") or {})
    stream, how = find_dub(layout, expected)
    report["identification"] = how
    if stream is None:
        findings.append(_finding("dub_stream_missing", "failure",
                                 {"expected": expected,
                                  "streams": audio_streams(layout)}, how))
        report.update(state="failed", findings=findings,
                      publishable=config["mode"] != "enforce")
        job.delivery = report
        return report
    report["stream"] = stream
    container = _float((layout.get("format") or {}).get("duration"))

    # The meter runs first because it is also the only reliable way to learn
    # how long the added track really is: Matroska stores no per-stream
    # duration, and believing the container's own would make a truncated dub
    # inside a full-length file look correct.
    loudness = measure_loudness(output, stream["audio_index"], cancel)
    report["loudness"] = loudness
    report["decoded_seconds"] = loudness.get("decoded")
    findings += _structure(stream, container, layout, expected, config,
                           decoded=loudness.get("decoded"))
    findings += _loudness_findings(loudness, config)

    root = Path(work_dir or (job.artifacts_dir or output.parent)) / "delivery"
    placement, placement_findings = _placement(job, output, stream, config, root, cancel)
    report["placement"] = placement
    findings += placement_findings

    report["findings"] = findings
    report["checked"] = len(findings)
    failures = [f for f in findings if f["severity"] == "failure"]
    warnings = [f for f in findings if f["severity"] == "warning"]
    report["state"] = ("failed" if failures else "warned" if warnings else "passed")
    report["publishable"] = not (failures and config["mode"] == "enforce")
    report["summary"] = _summarise(report, failures, warnings)
    job.delivery = report
    log.info("delivery: %s — %d failure(s), %d warning(s) on %s",
             report["state"], len(failures), len(warnings), output.name)
    return report


def _structure(stream: dict, container: float | None, layout: dict,
               expected: dict, config: dict,
               decoded: float | None = None) -> list[dict]:
    """Format, duration, start time and preservation of the original streams."""
    found: list[dict] = []
    if config["sample_rate"] and stream["sample_rate"] != config["sample_rate"]:
        found.append(_finding(
            "sample_rate_mismatch", "failure",
            {"found": stream["sample_rate"], "expected": config["sample_rate"]},
            f"the dub is {stream['sample_rate']} Hz; this profile expects "
            f"{config['sample_rate']} Hz"))
    if config["channels"] and stream["channels"] != config["channels"]:
        found.append(_finding(
            "channel_mismatch", "failure",
            {"found": stream["channels"], "layout": stream["layout"],
             "expected": config["channels"]},
            f"the dub has {stream['channels']} channel(s); this profile expects "
            f"{config['channels']}"))
    # Decoded length first, then the stream tag, then the container. Only the
    # first of those is a measurement of the dub itself.
    length = decoded if decoded is not None else stream["duration"]
    if length is None:
        length = container
    if length is not None and container:
        drift = abs(length - container)
        if drift > config["duration_tolerance"]:
            found.append(_finding(
                "duration_mismatch", "failure",
                {"dub_seconds": round(length, 3), "container_seconds": round(container, 3),
                 "drift": round(drift, 3), "tolerance": config["duration_tolerance"],
                 "measured_by": "decode" if decoded is not None else "container"},
                f"the dub runs {length:.2f}s inside a {container:.2f}s file, "
                f"{drift:.2f}s apart — the track is truncated or over-long"))
    start = stream["start_time"]
    if start is not None and abs(start) > config["start_tolerance"]:
        found.append(_finding(
            "start_offset", "warning",
            {"start_time": round(start, 4), "tolerance": config["start_tolerance"]},
            f"the dub stream starts at {start:+.3f}s; beyond encoder delay this "
            f"shifts the whole track against the picture"))
    if config["check_original_streams"]:
        kept = expected.get("original_streams")
        if isinstance(kept, dict):
            now = _stream_counts(layout)
            # The dub adds exactly one audio stream; everything else must survive.
            wanted = {**kept, "audio": kept.get("audio", 0) + 1}
            lost = {k: [wanted[k], now.get(k, 0)] for k in wanted
                    if now.get(k, 0) < wanted[k]}
            if lost:
                found.append(_finding(
                    "original_streams_lost", "failure",
                    {"expected": wanted, "found": now, "missing": lost},
                    "the export has fewer streams than the original; adding a dub "
                    "must never drop a track the source had"))
    return found


def _loudness_findings(loudness: dict, config: dict) -> list[dict]:
    """Grade loudness only where a target exists; otherwise report the number."""
    found: list[dict] = []
    if loudness.get("state") == "failed":
        found.append(_finding("decode_failed", "failure",
                              {"reason": loudness.get("reason", "")},
                              "the delivered dub stream could not be decoded"))
        return found
    lufs, peak = loudness.get("lufs"), loudness.get("true_peak_db")
    target, ceiling = config["target_lufs"], config["true_peak_db"]
    if target is not None and lufs is not None:
        drift = lufs - target
        if abs(drift) > config["lufs_tolerance"]:
            found.append(_finding(
                "loudness_off_target", "warning",
                {"lufs": lufs, "target": target, "drift": round(drift, 2),
                 "tolerance": config["lufs_tolerance"], "meter": METER},
                f"the dub measures {lufs:.1f} LUFS against a {target:.1f} LUFS "
                f"target for this local profile"))
    if ceiling is not None and peak is not None and peak > ceiling:
        found.append(_finding(
            "true_peak_over", "warning",
            {"true_peak_db": peak, "ceiling": ceiling, "meter": METER},
            f"true peak is {peak:.1f} dBFS, over this profile's {ceiling:.1f} dBFS "
            f"ceiling"))
    if target is None and ceiling is None and lufs is not None:
        found.append(_finding(
            "loudness_measured", "info",
            {"lufs": lufs, "true_peak_db": peak, "lra": loudness.get("lra"),
             "meter": METER},
            f"the delivered dub measures {lufs:.1f} LUFS, true peak "
            f"{'unknown' if peak is None else f'{peak:.1f}'} dBFS. No target is "
            f"configured, so this is a measurement and not a pass."))
    return found


def _placement(job, output: Path, stream: dict, config: dict, root: Path,
               cancel) -> tuple[list[dict], list[dict]]:
    """Check head, middle and tail dialogue actually survived the encode.

    Only windows where this run placed a clip are expected to contain
    anything. A quiet scene is not evidence of a broken export, and treating
    it as one would make every atmospheric episode fail.
    """
    placed = [s for s in job.segments
              if s.audio_clip and Path(s.audio_clip).exists() and s.duration > 0]
    if not placed or not config["placement_samples"]:
        return [], []
    placed.sort(key=lambda s: s.start)
    count = min(config["placement_samples"], len(placed))
    if count == 1:
        picks = [placed[0]]
    else:
        step = (len(placed) - 1) / (count - 1)
        picks = [placed[int(round(i * step))] for i in range(count)]
    rows: list[dict] = []
    found: list[dict] = []
    for seg in picks:
        # Widened by the profile's start tolerance on both sides, because the
        # encoder's own delay is a known and allowed shift, not a defect.
        pad = config["start_tolerance"]
        start = max(0.0, seg.start - pad)
        end = seg.end + pad + seg.treatment.tail
        clip = root / f"place-{seg.cue_id or seg.index}.wav"
        try:
            decode_window(output, stream["audio_index"], start, end, clip, cancel)
            stats = analyze_levels(clip)
        except (FFmpegError, OSError, wave.Error, EOFError) as exc:
            found.append(_finding(
                "placement_unreadable", "failure",
                {"cue": seg.cue_id, "line": seg.index, "error": str(exc)},
                f"the delivered dub could not be decoded around {seg.start:.2f}s"))
            continue
        speech = stats["speech_db"]
        row = {
            "cue": seg.cue_id, "line": seg.index,
            "window": [round(start, 3), round(end, 3), "target"],
            "speech_db": speech, "peak": round(stats["peak"], 4),
            "state": "present" if speech is not None and speech > config["silence_db"]
                     else "silent",
        }
        rows.append(row)
        if row["state"] == "silent":
            found.append(_finding(
                "cue_missing_in_export", "failure",
                {"cue": seg.cue_id, "line": seg.index, "speech_db": speech,
                 "window": row["window"], "silence_db": config["silence_db"]},
                f"line {seg.index} was placed at {seg.start:.2f}s but the delivered "
                f"track is silent there"))
        if stats["peak"] >= CLIP_PEAK:
            found.append(_finding(
                "clipping_in_export", "warning",
                {"cue": seg.cue_id, "line": seg.index, "peak": round(stats["peak"], 4),
                 "window": row["window"]},
                f"the delivered track reaches full scale around {seg.start:.2f}s"))
    return rows, found


def _summarise(report: dict, failures: list, warnings: list) -> str:
    stream = report["stream"] or {}
    parts = [
        f"{stream.get('codec', 'unknown')} {stream.get('channels', '?')}ch "
        f"{stream.get('sample_rate', '?')} Hz, "
        f"{stream.get('language') or 'untagged'}/{stream.get('title') or 'untitled'}"
    ]
    loudness = report.get("loudness") or {}
    if loudness.get("lufs") is not None:
        peak = loudness.get("true_peak_db")
        parts.append(f"{loudness['lufs']:.1f} LUFS"
                     + (f", true peak {peak:.1f} dBFS" if peak is not None else ""))
    if failures:
        parts.append(f"{len(failures)} structural failure(s)")
    elif warnings:
        parts.append(f"{len(warnings)} warning(s)")
    else:
        parts.append("all configured structural checks passed")
    return "; ".join(parts)


def _finding(code: str, severity: str, evidence: dict, detail: str) -> dict:
    if severity not in SEVERITIES:
        raise ValueError(f"unknown delivery severity {severity!r}")
    return {"code": code, "severity": severity, "detail": detail,
            "evidence": evidence, "detector": DETECTOR}


# --------------------------------------------------------------------------
# Revision comparison
# --------------------------------------------------------------------------

def compare(previous: dict, current: dict) -> dict:
    """What changed between two saved version manifests, and what should have.

    Deliberately distinguishes three things a reviewer conflates otherwise: a
    line whose *generation* changed, a line whose *processing* changed, and a
    neighbouring region that legitimately moved because ducking and effect
    tails propagate past the cue that was edited.
    """
    left = {row.get("cue_id"): row for row in (previous.get("cues") or []) if row.get("cue_id")}
    right = {row.get("cue_id"): row for row in (current.get("cues") or []) if row.get("cue_id")}
    changes = []
    for cue_id, row in right.items():
        was = left.get(cue_id)
        if was is None:
            changes.append({"cue": cue_id, "change": "added",
                            "line": row.get("index")})
            continue
        fields = _cue_differences(was, row)
        if fields:
            changes.append({"cue": cue_id, "change": "changed",
                            "line": row.get("index"), **fields})
    removed = [cue for cue in left if cue not in right]
    windows = _affected_windows(changes, right)
    return {
        "previous_version": previous.get("version_id"),
        "version": current.get("version_id"),
        "previous_output": previous.get("output_sha256"),
        "output": current.get("output_sha256"),
        "identical_output": (previous.get("output_sha256") == current.get("output_sha256")
                             and bool(current.get("output_sha256"))),
        "regenerated": [c for c in changes if c.get("kind") == "generation"],
        "reprocessed": [c for c in changes if c.get("kind") == "processing"],
        "decided": [c for c in changes if c.get("kind") == "decision"],
        "added": [c for c in changes if c["change"] == "added"],
        "removed": removed,
        "expected_changed_windows": windows,
        "settings": _settings_differences(previous.get("settings") or {},
                                          current.get("settings") or {}),
        "note": ("A window listed here is expected to differ in the decoded mix. "
                 "Ducking and effect tails reach past the cue that changed, so a "
                 "neighbouring region moving is not evidence of a second edit."),
    }


def _cue_differences(was: dict, now: dict) -> dict:
    """What moved on one cue, classified by what it costs to redo."""
    old_audio = was.get("audio") or {}
    new_audio = now.get("audio") or {}
    old_take = (old_audio.get("selection") or {}).get("take_id") or ""
    new_take = (new_audio.get("selection") or {}).get("take_id") or ""
    old_renders = {a.get("role"): a.get("fingerprint") for a in old_audio.get("renders") or []}
    new_renders = {a.get("role"): a.get("fingerprint") for a in new_audio.get("renders") or []}
    old_takes = {t.get("take_id"): t.get("fingerprint") for t in old_audio.get("takes") or []}
    new_takes = {t.get("take_id"): t.get("fingerprint") for t in new_audio.get("takes") or []}
    detail: dict = {}
    if new_take != old_take:
        detail["take"] = [old_take, new_take]
    fresh = [t for t in new_takes if t not in old_takes]
    if fresh:
        detail["new_takes"] = fresh
    roles = sorted(set(old_renders) | set(new_renders))
    moved = [r for r in roles if old_renders.get(r) != new_renders.get(r)]
    if moved:
        detail["renders"] = moved
    old_treatment = (was.get("treatment") or {})
    new_treatment = (now.get("treatment") or {})
    for key in ("preset", "intensity", "outcome"):
        if old_treatment.get(key) != new_treatment.get(key):
            detail.setdefault("treatment", {})[key] = [old_treatment.get(key),
                                                       new_treatment.get(key)]
    if not detail:
        return {}
    if fresh or (new_take and new_take not in old_takes):
        detail["kind"] = "generation"
    elif moved or "treatment" in detail or "take" in detail:
        detail["kind"] = "processing"
    else:
        detail["kind"] = "decision"
    return detail


def _affected_windows(changes: list, cues: dict) -> list[dict]:
    """Target windows a reviewer should expect to differ, tails included."""
    windows = []
    for change in changes:
        row = cues.get(change["cue"])
        if not row:
            continue
        placement = row.get("placement") or {}
        onset = placement.get("onset")
        treatment = row.get("treatment") or {}
        tail = float(treatment.get("tail") or 0.0)
        windows.append({
            "cue": change["cue"], "line": row.get("index"),
            "kind": change.get("kind", change["change"]),
            "onset": onset,
            "tail": tail,
            "note": ("this cue's own audio changed"
                     + (f"; its treatment rings out for a further {tail:.2f}s"
                        if tail else "")),
        })
    return windows


def _settings_differences(previous: dict, current: dict) -> dict:
    """Which settings sections differ, without dumping both of them."""
    moved = {}
    for section in sorted(set(previous) | set(current)):
        if previous.get(section) != current.get(section):
            moved[section] = {"previous": previous.get(section),
                              "current": current.get(section)}
    return moved


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def _stream_counts(layout: dict) -> dict:
    counts: dict = {}
    for stream in layout.get("streams", []):
        kind = stream.get("codec_type") or "unknown"
        counts[kind] = counts.get(kind, 0) + 1
    return counts


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _search(pattern: str, text: str) -> float | None:
    found = re.search(pattern, text)
    return round(float(found.group(1)), 3) if found else None
