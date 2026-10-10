"""Who speaks each line, without an account: voice embeddings and clustering.

pyannote's diarization pipeline is gated behind a HuggingFace token. The voice
embedding model it uses inside (WeSpeaker ResNet34, trained on VoxCeleb) is
not, and it is all a dub needs once the lines are already cut: a dub's lines
come from subtitles or from recognition, so the question is only which lines
share a voice. Each line is embedded on the separated dialogue with the chosen
voice model(s) (doblarr.voice_models) and the lines are grouped by cosine
distance.

What the grouping is tuned for, measured on a hand-labelled anime episode:
a group almost never mixes two people (purity 0.90-1.00), and one person is
sometimes split in two (the lead shouting and the lead calm). That is the cheap
error: giving both groups the same name joins them, where a mixed group would have
to be pulled apart line by line.

The other audio tracks of the same video (the dubs) can be heard too: each
dub has its own cast, so a voice two Japanese actors share is rarely shared
again in Spanish. On that episode, adding its two Spanish dubs to the
separated Japanese dialogue found 14 voices instead of 16, F1 0.88 against
0.85. A dub is used as it is in the file (no separation; its music is the same
in every line, so it moves every vector alike).

Every line's embedding is kept beside the script (`<stem>.speakers.json`), so a
named character can be found again in the next episode without re-listening.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path

from . import voice_models
from .fileio import write_json

log = logging.getLogger("doblarr.speakers")


MODEL_ID = voice_models.BUILT_IN[voice_models.DEFAULT].source
DETECTOR = "voice-clusters/2"
RATE = voice_models.RATE
THRESHOLD = 0.7          # cosine distance at which two groups stay apart
MIN_EMBED = 0.8          # shorter lines are too little voice to group on
MIN_HEARD = 0.3          # below this a line takes its neighbour's speaker
MIN_LINES = 3            # a group of fewer lines joins its nearest voice...
MIN_LINES_FROM = 30      # ...once there are this many lines to group


def _audio(path: Path):
    import numpy as np

    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-ac", "1", "-ar",
                          str(RATE), "-f", "s16le", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def audio_tracks(source: Path) -> list[dict]:
    """The audio streams of a video: [{stream, lang (two letters), title}]."""
    from .discovery import ISO3_TO_ISO2

    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
         "stream=index:stream_tags=language,title", "-of", "json", str(source)],
        capture_output=True, check=False, text=True).stdout
    streams = []
    for row in json.loads(out or "{}").get("streams") or []:
        tags = row.get("tags") or {}
        code = str(tags.get("language") or "und").lower()
        streams.append({"stream": int(row["index"]), "lang": ISO3_TO_ISO2.get(code, code),
                        "title": str(tags.get("title") or "")})
    return streams


def other_tracks(source: Path, original: str, folder: Path, stem: str,
                 wanted: str | list = "all") -> list[tuple[int, Path]]:
    """The dub tracks to listen to as well, as 16 kHz mono files (made once).

    `wanted` is "all", or a list of languages or stream numbers; the track in
    the original language is never one of them (its separated dialogue is the
    main evidence already).
    """
    if not wanted or not Path(source).is_file():
        return []
    picks = {str(w).lower() for w in wanted} if isinstance(wanted, list) else None
    out = []
    for track in audio_tracks(Path(source)):
        if track["lang"] == (original or "").lower():
            continue
        if picks is not None and str(track["stream"]) not in picks and track["lang"] not in picks:
            continue
        wav = folder / f"{stem}.audio{track['stream']}.16k.wav"
        if not wav.is_file():
            folder.mkdir(parents=True, exist_ok=True)
            temp = wav.with_suffix(".part.wav")
            result = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(source), "-map",
                                     f"0:{track['stream']}", "-ac", "1", "-ar", str(RATE),
                                     str(temp)], capture_output=True, check=False)
            if result.returncode:
                temp.unlink(missing_ok=True)
                log.warning("speakers: could not read audio track %s of %s", track["stream"],
                            Path(source).name)
                continue
            os.replace(temp, wav)
        out.append((track["stream"], wav))
    return out


def embed_with(audio_path: Path, extra: list[Path], spans: list[tuple[float, float]],
               device: str = "cpu", models: list[voice_models.VoiceModel] | None = None,
               models_dir: Path | None = None,
               maps: list[dict | None] | None = None) -> tuple[list, list]:
    """`embed` on the main dialogue, and the same joined with the lines in
    other tracks (a line silent in a dub keeps its place with zeros there).

    `maps` gives, per extra track, the verified alignment of that track to the
    source timeline (``{"offset", "rate"}``, see doblarr.track_alignment): a
    line is read on a dub where the dub says it, not at the original's
    timestamps. A track with no verified alignment should not be passed at all.

    The joined vectors group the lines; the main ones are what is kept, so a
    voice tag means the same thing in every episode whichever dubs it has.
    """
    import numpy as np

    vectors = embed(audio_path, spans, device, models, models_dir)
    if not extra:
        return vectors, vectors
    others = []
    for n, path in enumerate(extra):
        mapping = (maps or [None] * len(extra))[n] or {}
        offset, rate = float(mapping.get("offset", 0.0)), float(mapping.get("rate", 1.0))
        mapped = [((s - offset) / rate, (e - offset) / rate) for s, e in spans]
        others.append(embed(path, [(max(0.0, s), max(0.0, e)) for s, e in mapped], device,
                            models, models_dir))
    joined: list = []
    for i, main in enumerate(vectors):
        if main is None:
            joined.append(None)
            continue
        parts = [main] + [o[i] if o[i] is not None else np.zeros_like(main) for o in others]
        v = np.concatenate(parts)
        joined.append(v / float(np.linalg.norm(v)))
    return joined, vectors


def embed(audio_path: Path, spans: list[tuple[float, float]], device: str = "cpu",
          models: list[voice_models.VoiceModel] | None = None,
          models_dir: Path | None = None) -> list:
    """One normalised voice vector per (start, end) span; None where too short.

    With several models each line's vectors are normalised and joined, so the
    cosine of two joined vectors is the mean of the models' cosines.
    """
    import numpy as np

    models = models or voice_models.resolve(voice_models.DEFAULT)
    folder = models_dir or voice_models.folder()
    samples = _audio(audio_path)
    chunks = []
    for start, end in spans:
        chunk = samples[int(start * RATE):int(end * RATE)]
        chunks.append(chunk if end - start >= MIN_HEARD and len(chunk) >= int(MIN_HEARD * RATE)
                      else None)
    parts: list[list] = [[] for _ in spans]
    for model in models:
        run = voice_models.extractor(model, folder, device)
        for i, chunk in enumerate(chunks):
            if chunk is None:
                continue
            vector = np.asarray(run(chunk), dtype=np.float32).ravel()
            norm = float(np.linalg.norm(vector))
            parts[i].append(vector / norm if norm else None)
    vectors: list = []
    for found in parts:
        if not found or any(v is None for v in found):
            vectors.append(None)
            continue
        joined = np.concatenate(found)
        vectors.append(joined / float(np.linalg.norm(joined)))
    return vectors


def cluster(vectors: list, durations: list[float], threshold: float = THRESHOLD,
            min_lines: int = MIN_LINES) -> list[str]:
    """A speaker label per line: SPEAKER_00 is the one heard longest.

    See `cluster_detailed` for how; this returns the labels alone.
    """
    return cluster_detailed(vectors, durations, threshold, min_lines)[0]


DIAGNOSTICS = "voice-diagnostics/1"


def cluster_detailed(vectors: list, durations: list[float], threshold: float = THRESHOLD,
                     min_lines: int = MIN_LINES) -> tuple[list[str], dict]:
    """Speaker labels and why each line got its label.

    Lines long enough to group on are clustered; shorter ones join the nearest
    group; lines with no voice at all take the previous line's speaker. In a
    full episode a group of one or two lines is nearly always a piece of a
    character found elsewhere (in the measured episode, three main
    characters shouting), so it joins its nearest larger voice instead of
    standing alone.

    Each of those steps can hide a brief character or join two similar voices,
    so the decision is kept rather than thrown away: per line, the method
    (``clustered``, ``merged_small``, ``nearest``, ``inherited``), the closest
    groups with their cosine similarity and the margin between the first two.
    A line decided without its own voice evidence is never presented as if it
    had been heard.
    """
    import numpy as np

    anchors = [i for i, v in enumerate(vectors)
               if v is not None and durations[i] >= MIN_EMBED]
    labels: list[int | None] = [None] * len(vectors)
    methods: list[str] = ["inherited"] * len(vectors)
    merged_from: dict[int, int] = {}
    if len(anchors) == 1:
        labels[anchors[0]] = 0
    elif anchors:
        from sklearn.cluster import AgglomerativeClustering

        found = AgglomerativeClustering(
            n_clusters=None, metric="cosine", linkage="average",
            distance_threshold=threshold).fit_predict(np.array([vectors[i] for i in anchors]))
        for i, label in zip(anchors, found, strict=True):
            labels[i] = int(label)
    for i in anchors:
        methods[i] = "clustered"
    groups = sorted({label for label in labels if label is not None})
    merges: list[dict] = []
    if len(anchors) >= MIN_LINES_FROM and min_lines > 1:
        size = {g: sum(1 for label in labels if label == g) for g in groups}
        kept = [g for g in groups if size[g] >= min_lines]
        if kept and len(kept) < len(groups):
            stays = {g: _centroid([vectors[i] for i, label in enumerate(labels) if label == g])
                     for g in kept}
            for g in groups:
                if g in kept:
                    continue
                members = [i for i in anchors if labels[i] == g]
                centre = _centroid([vectors[i] for i in members])
                into = max(kept, key=lambda k: float(np.dot(centre, stays[k])))
                merges.append({"group": g, "lines": len(members), "into": into,
                               "similarity": round(float(np.dot(centre, stays[into])), 3),
                               "seconds": round(sum(durations[i] for i in members), 2)})
            for i in anchors:
                if labels[i] not in kept:
                    merged_from[i] = int(labels[i] or 0)
                    labels[i] = max(kept, key=lambda g: float(np.dot(vectors[i], stays[g])))
                    methods[i] = "merged_small"
            groups = kept
    centroids = {}
    if groups:
        centroids = {g: _centroid([vectors[i] for i, label in enumerate(labels) if label == g])
                     for g in groups}
        for i, vector in enumerate(vectors):
            if labels[i] is None and vector is not None:
                labels[i] = max(groups, key=lambda g: float(np.dot(vector, centroids[g])))
                methods[i] = "nearest"
    for i in range(len(labels)):
        if labels[i] is None:
            labels[i] = labels[i - 1] if i and labels[i - 1] is not None else (
                groups[0] if groups else 0)
    final = [int(label or 0) for label in labels]
    heard: dict[int, float] = {}
    for label, seconds in zip(final, durations, strict=True):
        heard[label] = heard.get(label, 0.0) + seconds
    order = sorted(heard, key=lambda g: -heard[g])
    names = {g: f"SPEAKER_{n:02d}" for n, g in enumerate(order)}
    lines = []
    for i, vector in enumerate(vectors):
        row: dict = {"method": methods[i], "seconds": round(durations[i], 2)}
        if vector is not None and centroids:
            ranked = sorted(((float(np.dot(vector, c)), g) for g, c in centroids.items()),
                            reverse=True)[:3]
            row["candidates"] = [{"label": names.get(g, f"SPEAKER_{g}"),
                                  "similarity": round(s, 3)} for s, g in ranked]
            row["margin"] = round(ranked[0][0] - ranked[1][0], 3) if len(ranked) > 1 else None
        else:
            row["candidates"], row["margin"] = [], None
        if i in merged_from:
            row["merged_from"] = f"group-{merged_from[i]}"
        lines.append(row)
    diagnostics = {
        "version": DIAGNOSTICS, "threshold": threshold, "min_lines": min_lines,
        "anchors": len(anchors), "lines": lines,
        "merges": [{**m, "group": f"group-{m['group']}", "into": names.get(m["into"])}
                   for m in merges],
        "methods": {m: methods.count(m) for m in sorted(set(methods))},
    }
    return [names[label] for label in final], diagnostics


def _centroid(vectors: list):
    import numpy as np

    mean = np.mean(vectors, axis=0)
    norm = float(np.linalg.norm(mean))
    return mean / norm if norm else mean


def assign(job, audio_path: Path, sidecar: Path | None = None, device: str = "cpu",
           threshold: float | None = None, models: list[voice_models.VoiceModel] | None = None,
           models_dir: Path | None = None,
           tracks: list[tuple[int, Path]] | None = None,
           track_evidence: list[dict] | None = None) -> list[str]:
    """Label every line of `job` and keep the evidence beside the script.

    `tracks` are the extra audio tracks already checked as usable (permitted
    and aligned); `track_evidence` is the record of every track considered,
    used or not, so a reader can see which track supplied which evidence.
    """
    from .models import Speaker

    models = models or voice_models.resolve(voice_models.DEFAULT)
    threshold = voice_models.threshold(models, threshold)
    spans = [(seg.start, seg.end) for seg in job.segments]
    tracks = tracks or []
    maps = _maps_for(tracks, track_evidence)
    joined, vectors = embed_with(audio_path, [p for _, p in tracks], spans, device, models,
                                 models_dir, maps)
    durations = [max(0.0, end - start) for start, end in spans]
    labels, diagnostics = cluster_detailed(joined, durations, threshold)
    for seg, label, vector, seconds in zip(job.segments, labels, vectors, durations,
                                           strict=True):
        seg.speaker = label
        if vector is None or seconds < MIN_EMBED:
            seg.issues.append("speaker_uncertain")
    job.speakers = {label: Speaker(label=label) for label in sorted(set(labels))}
    if sidecar is not None:
        _write_sidecar(sidecar, models, threshold,
                       [(seg.cue_id, seg.start, seg.end, seg.speaker) for seg in job.segments],
                       vectors, [stream for stream, _ in tracks], diagnostics,
                       track_evidence)
    log.info("speakers: %d line(s) grouped into %d voice(s) with %s",
             len(labels), len(job.speakers),
             voice_model_key(models, [stream for stream, _ in tracks]))
    return labels


def _maps_for(tracks: list[tuple[int, Path]], evidence: list[dict] | None) -> list[dict | None]:
    by_stream = {int(e["stream"]): e for e in evidence or [] if e.get("stream") is not None}
    return [by_stream.get(int(stream)) for stream, _ in tracks]


def _write_sidecar(path: Path, models, threshold: float, rows: list[tuple], vectors: list,
                   tracks: list[int] | None = None, diagnostics: dict | None = None,
                   track_evidence: list[dict] | None = None):
    per_line = (diagnostics or {}).get("lines") or [None] * len(rows)
    write_json(path, {
        "detector": DETECTOR, "model": voice_models.combined_id(models), "threshold": threshold,
        "tracks": tracks or [], "track_evidence": track_evidence or [],
        "diagnostics": {k: v for k, v in (diagnostics or {}).items() if k != "lines"},
        "lines": [{"cue": cue, "start": start, "end": end, "speaker": speaker,
                   "vector": None if v is None else [round(float(x), 5) for x in v],
                   **({"why": why} if why else {})}
                  for (cue, start, end, speaker), v, why in zip(rows, vectors, per_line,
                                                                strict=True)],
    }, indent=None)


def move_lines(script: Path, sidecar: Path | None, moves: dict[str, str]) -> int:
    """Give single lines to another voice group, in the script and its
    evidence: a person who watched the line knows better than the grouping."""
    if not moves:
        return 0
    data = json.loads(script.read_text(encoding="utf-8"))
    moved = 0
    for seg in data.get("segments") or []:
        label = moves.get(str((seg.get("cue") or {}).get("cue_id")))
        if label and seg.get("speaker") != label:
            seg["speaker"] = label
            seg["issues"] = [i for i in seg.get("issues") or [] if i != "speaker_uncertain"]
            source = (seg.get("cue") or {}).get("source")
            if isinstance(source, dict) and "speaker" in source:
                source["speaker"] = label
            moved += 1
    data["speakers"] = sorted({str(seg.get("speaker")) for seg in data.get("segments") or []})
    write_json(script, data, indent=1)
    if sidecar is not None and sidecar.is_file():
        evidence = json.loads(sidecar.read_text(encoding="utf-8"))
        for line in evidence.get("lines") or []:
            if str(line.get("cue")) in moves:
                line["speaker"] = moves[str(line["cue"])]
                # A person decided this line; the grouping's reasons no longer apply.
                line["why"] = {"method": "manual", "candidates": [], "margin": None}
        write_json(sidecar, evidence, indent=None)
    return moved


def voice_model_key(models, tracks: list[int] | None = None) -> str:
    """How a grouping was made, for the log: models, then any dub streams."""
    key = voice_models.combined_id(models)
    return key + (f" + audio {','.join(str(t) for t in tracks)}" if tracks else "")


def regroup(script: Path, audio: Path, sidecar: Path, models: list[voice_models.VoiceModel],
            models_dir: Path | None = None, threshold: float | None = None,
            device: str = "cpu", tracks: list[tuple[int, Path]] | None = None,
            track_evidence: list[dict] | None = None,
            ) -> tuple[list[str], list[str], list[float]]:
    """Group an analysed episode's lines again, with other voice models.

    Only the speakers change: the cached dialogue is re-read, the lines keep
    their text, timing and raw measurements. Returns the labels before and
    after and each line's length, so names can follow the lines they were
    given to. Speaker-relative baselines depend on who is in each group, so the
    caller refreshes them (doblarr.speaker_memory.refresh_baselines).
    """
    data = json.loads(script.read_text(encoding="utf-8"))
    segments = data.get("segments") or []
    spans = [(float(seg["start"]), float(seg["end"])) for seg in segments]
    durations = [max(0.0, end - start) for start, end in spans]
    tracks = tracks or []
    joined, vectors = embed_with(audio, [p for _, p in tracks], spans, device, models,
                                 models_dir, _maps_for(tracks, track_evidence))
    labels, diagnostics = cluster_detailed(joined, durations,
                                           voice_models.threshold(models, threshold))
    before = [str(seg.get("speaker") or "") for seg in segments]
    for seg, label, vector, seconds in zip(segments, labels, vectors, durations, strict=True):
        seg["speaker"] = label
        issues = [i for i in seg.get("issues") or [] if i != "speaker_uncertain"]
        if vector is None or seconds < MIN_EMBED:
            issues.append("speaker_uncertain")
        seg["issues"] = issues
        source = (seg.get("cue") or {}).get("source")
        if isinstance(source, dict) and "speaker" in source:
            source["speaker"] = label
    data["speakers"] = sorted(set(labels))
    write_json(script, data, indent=1)
    _write_sidecar(sidecar, models, voice_models.threshold(models, threshold),
                   [((seg.get("cue") or {}).get("cue_id"), seg["start"], seg["end"],
                     seg["speaker"]) for seg in segments], vectors,
                   [stream for stream, _ in tracks], diagnostics, track_evidence)
    log.info("speakers: regrouped %s with %s -> %d voice(s)", script.name,
             voice_model_key(models, [stream for stream, _ in tracks]), len(set(labels)))
    return before, labels, durations
