"""One episode or film before dubbing: its analysis, its voices, its published dub.

    doblarr analyze FILE [--to es-419] [--force]   # separate, transcribe, group voices
    doblarr voices FILE [--lines] [--name SPEAKER_01=Mina] [--move 12,14=Kaito]
    doblarr dubref FILE [--compare JOB] [--lines]  # what the file's own dub says
"""

from __future__ import annotations

import json
import statistics
from collections import defaultdict
from pathlib import Path

from ..hardware import resolve_device
from .base import ApiError, Ctx, default_target, find_job, pairs, wait
from .jobs import queue_job

# -- analyze -----------------------------------------------------------------------


def analysis(ctx: Ctx, file: str) -> dict:
    return ctx.api.get("/api/analysis", path=file)


def analyze(ctx: Ctx, file: str, *, to: str, source: str = "auto", force: bool = False) -> dict:
    """The episode's analysis, running one first when there is none."""
    data = {} if force else analysis(ctx, file)
    if data.get("lines"):
        return data
    job = queue_job(ctx, file, to=to, source=source, kind="analyze",
                    title=f"Analyse · {Path(file).stem}", force=force)
    job = wait(ctx, job["id"])
    if job["status"] != "done":
        raise ApiError(f"analysis {job['status']}: {job.get('message') or ''}")
    return analysis(ctx, file)


def summary(data: dict) -> dict:
    """Voice groups, named or not, with how much each one says."""
    names = dict(data.get("names") or {})
    groups: dict[str, list[dict]] = defaultdict(list)
    for line in data.get("lines") or []:
        groups[line["speaker"]].append(line)
    rows = []
    for label in sorted(groups, key=lambda k: -len(groups[k])):
        lines = groups[label]
        pitches = [r["pitch_hz"] for r in lines if r.get("pitch_hz")]
        rows.append({"group": label, "name": names.get(label, ""), "lines": len(lines),
                     "pitch_hz": round(statistics.median(pitches)) if pitches else None,
                     "samples": [{"index": r["index"], "text": r["text"]} for r in lines[:3]]})
    tracks = [{"stream": t.get("stream"), "lang": t.get("lang"), "title": t.get("title"),
               "state": t.get("state")} for t in data.get("track_evidence") or []]
    identity = data.get("identity") or {}
    return {"series": identity.get("series_id"), "media": identity.get("media_id"),
            "lines": len(data.get("lines") or []), "groups": rows,
            "unnamed_lines": sum(r["lines"] for r in rows if not r["name"]),
            "dub_tracks": tracks}


def summary_text(info: dict, samples: int = 3) -> list[str]:
    out = []
    for row in info["groups"]:
        pitch = f"{row['pitch_hz']} Hz" if row["pitch_hz"] else "-"
        out.append(f"{row['group']}  {row['name'] or '(unnamed)':14s} {row['lines']:3d} lines  "
                   f"median pitch {pitch}")
        out.extend(f"      {s['index']:4d}  {s['text'][:80]}" for s in row["samples"][:samples])
    return out


def cmd_analyze(ctx: Ctx) -> int:
    a = ctx.args
    info = summary(analyze(ctx, a.file, to=a.to or default_target(ctx.config),
                           source=a.source, force=a.force))
    ctx.result(info, [f"{info['lines']} lines in {len(info['groups'])} voice groups, "
                      f"{info['unnamed_lines']} lines unnamed"
                      + (f"; series {info['series']}" if info["series"] else "")]
               + summary_text(info, 2))
    return 0


# -- voices ------------------------------------------------------------------------


def cmd_voices(ctx: Ctx) -> int:
    a = ctx.args
    data = analysis(ctx, a.file)
    lines = data.get("lines") or []
    if not lines:
        raise ApiError("no analysis for this file yet; run `doblarr analyze FILE` first")
    names = dict(data.get("names") or {})
    changed = False
    if a.name:
        for label, name in pairs(a.name, "--name"):
            if name in ("", "-"):
                names.pop(label, None)
            else:
                names[label] = name
        ctx.api.put("/api/analysis/names", {"path": a.file, "names": names})
        changed = True
    by_index = {line["index"]: line for line in lines}
    for spec, character in pairs(a.move, "--move"):
        indexes = [int(x) for x in spec.replace(" ", "").split(",") if x]
        missing = [i for i in indexes if i not in by_index]
        if missing:
            raise ApiError(f"no line(s) {missing} in this episode")
        cues = [by_index[i]["cue"] for i in indexes]
        moved = ctx.api.put("/api/analysis/lines",
                            {"path": a.file, "cues": cues, "character": character})
        ctx.say(f"moved {len(cues)} line(s) to {character or 'a new voice'} -> "
                f"{moved['speaker']}")
        changed = True
    if changed:
        data = analysis(ctx, a.file)
        lines = data.get("lines") or []
        names = dict(data.get("names") or {})
    if a.lines:
        rows = [ln for ln in lines if not a.group or ln["speaker"] == a.group]
        ctx.result(rows, [
            f"{ln['index']:4d} {ln['start']:7.1f} {ln['speaker']} "
            f"{(names.get(ln['speaker']) or '?')[:12]:12s} "
            f"{(str(round(ln['pitch_hz'])) + 'Hz') if ln.get('pitch_hz') else '-':>6s}  "
            f"{ln['text'][:70]}  | {(ln.get('original_text') or '')[:30]}" for ln in rows])
        return 0
    info = summary(data)
    ctx.result(info, summary_text(info, a.samples))
    return 0


# -- dubref ------------------------------------------------------------------------


def episode_files(config, video: str, locale: str):
    stem = Path(video).stem
    media = Path(config.work_dir) / "media"
    scripts = sorted(media.glob(f"*/{locale}/{stem}.script.json"),
                     key=lambda p: p.stat().st_mtime, reverse=True)
    if not scripts:
        raise ApiError(f"no {locale} script for {stem}; analyse or dub the episode first")
    script = scripts[0]
    folder = script.parent.parent
    evidence = {}
    speakers = folder / f"{stem}.speakers.json"
    if speakers.is_file():
        data = json.loads(speakers.read_text(encoding="utf-8"))
        evidence = {t["stream"]: t for t in data.get("track_evidence") or []}
    return script, folder, stem, evidence


def cmd_dubref(ctx: Ctx) -> int:
    from .. import dub_reference

    a = ctx.args
    script, folder, stem, evidence = episode_files(ctx.config, a.file, a.locale)
    language = a.locale.split("-")[0]
    track = evidence.get(a.track) if a.track is not None else dub_reference.published_track(
        list(evidence.values()), language)
    if track is None:
        raise ApiError("no verified dub track in this language was found next to the "
                       "episode; pass --track N")
    stream = int(track["stream"])
    audio = folder / f"{stem}.audio{stream}.16k.wav"
    if not audio.is_file():
        raise ApiError(f"{audio.name} is not extracted yet; analyse the episode first")
    out = dub_reference.sidecar(script, stream)
    if out.is_file() and not a.refresh:
        ref = json.loads(out.read_text(encoding="utf-8"))
    else:
        ctx.say(f"transcribing track {stream} ({track.get('title', '?')}) with {a.model} ...")
        ref = dub_reference.build(script, audio, stream, language,
                                  float(track.get("offset") or 0.0),
                                  float(track.get("rate") or 1.0), a.model,
                                  resolve_device("transcribe", {"device": a.device}))
    lines = ref["lines"]
    heard = [ln for ln in lines if ln["text"]]
    target = dub_reference.reference_file(script, stream)
    target.write_text(json.dumps(dub_reference.reference(ref), ensure_ascii=False, indent=1),
                      encoding="utf-8")
    result: dict = {"track": stream, "title": track.get("title"), "lines": len(lines),
                    "heard": len(heard), "reference_file": str(target),
                    "names": dub_reference.frequent_names(lines)[:40]}
    text = [f"{track.get('title', 'track ' + str(stream))}: {len(heard)}/{len(lines)} lines "
            f"heard -> {out.name}",
            "Names and terms the dub repeats: "
            + ", ".join(f"{w} {n}" for w, n in result["names"])]
    ours: dict = {}
    if a.compare:
        job = find_job(ctx.api, a.compare)
        version = json.loads(Path(job["version_file"]).read_text(encoding="utf-8"))
        ours = {s["index"]: s for s in version["script"]["segments"]}
        cues = {c["index"]: c for c in version.get("cues") or []}
        longer = shorter = 0
        for ln in heard:
            renders = {r["role"]: r for r in (cues.get(ln["index"], {}).get("audio") or {})
                       .get("renders") or []}
            timed = (renders.get("fitted") or {}).get("duration")
            if ln["index"] in ours and timed and ln["seconds"]:
                longer += timed > ln["seconds"] * 1.25
                shorter += timed < ln["seconds"] * 0.8
        result.update(longer=longer, shorter=shorter)
        text.append(f"Against our dub: {longer} lines run 25% longer than the published one, "
                    f"{shorter} 20% shorter")
    if a.lines:
        result["pairs"] = [{"index": ln["index"], "dub": ln["text"],
                            "ours": (ours.get(ln["index"]) or {}).get("text")} for ln in lines]
        for ln in lines:
            text.append(f"{ln['index']:4d}  dub: {ln['text'][:90]}")
            if ln["index"] in ours:
                text.append(f"      us:  {ours[ln['index']]['text'][:90]}")
    ctx.result(result, text)
    return 0


def add_parsers(sub, common) -> None:
    an = sub.add_parser("analyze", parents=[common],
                        help="separate, transcribe and group the voices of a file (once)")
    an.add_argument("file")
    an.add_argument("--to", default=None, help="target locale (default: from settings)")
    an.add_argument("--from", dest="source", default="auto")
    an.add_argument("--force", action="store_true", help="analyse again")

    v = sub.add_parser("voices", parents=[common],
                       help="voice groups of an analysed file; name groups and move lines")
    v.add_argument("file", help="the video file, as the library knows it")
    v.add_argument("--lines", action="store_true", help="every line with its group")
    v.add_argument("--group", default=None, help="with --lines: only this group")
    v.add_argument("--samples", type=int, default=3, help="sample lines per group")
    v.add_argument("--name", action="append", default=[], metavar="SPEAKER_NN=NAME",
                   help="name a group (repeatable); NAME '-' clears it")
    v.add_argument("--move", action="append", default=[], metavar="I,J=NAME",
                   help="move lines (by index) to a character's group (repeatable)")

    d = sub.add_parser("dubref", parents=[common],
                       help="transcribe the file's own dub track and line it up with ours")
    d.add_argument("file")
    d.add_argument("--locale", default="es-419")
    d.add_argument("--track", type=int, default=None, help="stream index (default: Latino)")
    d.add_argument("--model", default="large-v3")
    d.add_argument("--device", default="auto", help="auto, cpu, cuda or cuda:N")
    d.add_argument("--refresh", action="store_true", help="transcribe again")
    d.add_argument("--compare", default=None, metavar="JOB_ID",
                   help="put a finished dub's lines next to the published ones")
    d.add_argument("--lines", action="store_true", help="print every line")


COMMANDS = {"analyze": cmd_analyze, "voices": cmd_voices, "dubref": cmd_dubref}
