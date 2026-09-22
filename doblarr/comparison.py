"""Render a bounded real-speech before/after from material this project already has.

The tone fixtures in `benchmarks.py` prove numbers. They cannot prove that a
dub sounds better, and no amount of them ever will: a 330 Hz sine has no
consonants, no breath and no acting. This module is the other half — it takes a
real source episode, the real translated script, and the real generated takes
from a completed run, cuts a short scene out of all of them, and renders that
scene through the **actual pipeline** once per settings variant.

Three properties it is built to guarantee, because without them a comparison is
worse than no comparison:

- **Same takes.** Every imported clip is registered as a take whose selection
  reason is `restored`, which is the existing mechanism `synthesize` already
  honours. The voicebox client handed to the run raises on any generation
  attempt, so "this was a processing change, not a new performance" is enforced
  rather than asserted. A variant that tried to generate speech fails loudly.
- **Same scene, same cast, same window.** Every variant reads one frozen
  excerpt of one source file, with one script and one set of speakers. The
  manifest records all of it, including what could not be reconstructed.
- **The features were actually on.** After each variant renders, the run's own
  records are read back — which timing owner ran, which level mode, which
  treatment preset reached `applied`, what coverage decided. "No difference"
  and "the feature fell back on this scene" are different findings and the
  session must be able to tell them apart.

What it deliberately does not do: it does not generate new speech, does not
call a paid service, does not render a library, does not touch the original
media, and does not decide whether the result is better. That last one is the
listener's, and the results sheet it writes is empty on purpose.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from .artifacts import digest, media_work, record, stamp
from .config import Config
from .cues import (
    RAW,
    SOURCE,
    Artifact,
    Selection,
    Span,
    Take,
    adopt_legacy,
    now,
    script_ref,
)
from .ffmpeg import FFmpegError, run_ffmpeg, run_ffprobe
from .languages import resolve_target_locale
from .levels import analyze as analyze_levels
from .models import DubJob, Segment, Speaker
from .pipeline import run_job
from .services import Services
from .stages import extract
from .stages.common import save_script
from .telemetry import write_json

log = logging.getLogger("doblarr.comparison")

MANIFEST_VERSION = 2
# Context kept either side of the first and last cue in a scene, so a line does
# not start abruptly and a tail is not cut at the boundary.
PAD_SECONDS = 1.0
# A scene longer than this is not an excerpt any more. The protocol asks for a
# few minutes in total across several short scenes, not an episode.
MAX_SCENE_SECONDS = 120.0
# At most this many of the source's own audio tracks are cut as references. A
# release can carry commentary and descriptive tracks nobody is comparing against.
MAX_REFERENCES = 4


class ComparisonError(RuntimeError):
    """A comparison could not be built, with the exact missing input named."""


class RefusesToGenerate:
    """A voicebox stand-in that makes a TTS request a crash rather than a cost.

    This is the enforcement behind "same-take processing comparison". If a
    variant reaches for the engine, something in the import is wrong — the
    selection did not survive, or an edit invalidated a take — and the right
    outcome is a failed comparison that says so, not a quietly re-acted line
    that makes a processing change look like an improvement.
    """

    def __init__(self, voices: list[str]):
        self.voices = list(voices)
        self.attempts: list[dict] = []
        self.observer = None

    def list_voices(self) -> list[dict]:
        return [{"id": v, "name": v} for v in self.voices]

    def create_profile(self, name: str, language: str) -> str:  # pragma: no cover
        raise ComparisonError("a comparison must not create a voice profile")

    def add_sample(self, profile_id, path, text) -> None:  # pragma: no cover
        raise ComparisonError("a comparison must not add a clone sample")

    def transcribe(self, path, language: str = "") -> dict:
        # Recognition is a read, not a generation. It is still refused here:
        # nothing in a bounded processing comparison needs it, and a silently
        # available recognizer would make one variant slower than another for
        # reasons that have nothing to do with what is being compared.
        raise ComparisonError("a comparison runs with recognition off")

    def synthesize_to_file(self, profile_id, text, language, dest, **kwargs):
        self.attempts.append({"profile": profile_id, "text": text})
        raise ComparisonError(
            f"a variant asked the engine to speak {text[:60]!r}. A comparison "
            f"reuses the takes it imported; generating here would make a "
            f"processing change look like a new performance.")


@dataclass
class Scene:
    """One bounded window of the original, and the cues inside it."""

    index: int
    title: str
    start: float          # SOURCE seconds in the original media
    end: float
    cues: list = field(default_factory=list)   # rows from the imported script

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    def as_dict(self) -> dict:
        return {
            "index": self.index, "title": self.title,
            "window": Span(self.start, self.end, SOURCE).as_dict(),
            "duration": round(self.duration, 3),
            "lines": len(self.cues),
            "speakers": sorted({row["speaker"] for row in self.cues}),
            "text": [{"index": row["index"], "speaker": row["speaker"],
                      "start": round(row["start"] - self.start, 3),
                      "source": row["text_src"], "dub": row["text_translated"],
                      "script": row.get("script_text", ""),
                      "stale_script": bool(row.get("stale_script"))}
                     for row in self.cues],
            "stale_script_lines": sum(1 for row in self.cues
                                      if row.get("stale_script")),
        }


# --------------------------------------------------------------------------
# Importing a completed run
# --------------------------------------------------------------------------

def read_script(path: Path) -> dict:
    """Load a saved script cache from a completed run, of any schema age."""
    path = Path(path)
    if not path.is_file():
        raise ComparisonError(f"no script cache at {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ComparisonError(f"{path} is not a readable script cache: {exc}") from exc
    if not payload.get("segments"):
        raise ComparisonError(f"{path} holds no segments")
    return payload


def find_clip(clips: Path, index: int) -> Path | None:
    """The generated WAV for one line, as the synthesis stage names them."""
    candidate = Path(clips) / f"line_{index:04d}.wav"
    return candidate if candidate.is_file() else None


def spoken_text(clip: Path) -> str:
    """What was actually asked of the engine for this clip, from its own receipt.

    The authority on what a take says is the receipt `synthesize` wrote beside
    it, not the script cache. A reviewed run rewrites lines and regenerates
    them, and the base script can be several edits behind the audio sitting
    next to it — so reading the script would caption a take with words it does
    not speak, and would hand a recognizer the wrong thing to expect.
    """
    receipt = Path(clip).with_suffix(".json")
    if not receipt.is_file():
        return ""
    try:
        payload = json.loads(receipt.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    return str((payload.get("request") or {}).get("text") or "")


def inventory(script: dict, clips: Path) -> list[dict]:
    """Every line in a completed run, with whether its audio is still on disk."""
    rows = []
    for raw in script["segments"]:
        index = int(raw["index"])
        clip = find_clip(clips, index)
        script_text = raw.get("text_translated") or ""
        spoken = spoken_text(clip) if clip else ""
        rows.append({
            "index": index,
            "start": float(raw["start"]),
            "end": float(raw["end"]),
            "speaker": str(raw.get("speaker") or "SPEAKER_00"),
            "text_src": str(raw.get("text_src") or ""),
            # What the take actually says wins over what the script remembers.
            "text_translated": spoken or script_text,
            "script_text": script_text,
            "spoken_text": spoken,
            # A run whose script is behind its audio is worth saying out loud:
            # it means the cached script is not what was reviewed.
            "stale_script": bool(spoken and script_text
                                 and spoken.strip() != script_text.strip()),
            "delivery": str(raw.get("delivery") or ""),
            "clip": str(clip) if clip else "",
        })
    return rows


def scenes_from(rows: list[dict], windows, titles=None) -> list[Scene]:
    """Turn requested `start:end` windows into scenes with their cues.

    A window with no playable line in it is an error rather than an empty
    scene: rendering silence and calling it a comparison would waste the
    listener's time before it wasted anyone else's.
    """
    found = []
    for position, (start, end) in enumerate(windows):
        if end <= start:
            raise ComparisonError(f"scene {position + 1} has a non-positive window")
        if end - start > MAX_SCENE_SECONDS:
            raise ComparisonError(
                f"scene {position + 1} is {end - start:.0f}s; this is a bounded "
                f"excerpt runner and {MAX_SCENE_SECONDS:.0f}s is its limit")
        inside = [row for row in rows if row["start"] >= start and row["end"] <= end]
        playable = [row for row in inside if row["clip"]]
        if not playable:
            raise ComparisonError(
                f"scene {position + 1} ({start:g}-{end:g}s) has no line with "
                f"generated audio on disk; {len(inside)} line(s) fall in it")
        title = (titles or {}).get(position) or f"{start:g}-{end:g}s"
        found.append(Scene(index=position, title=title, start=start, end=end,
                           cues=playable))
    return found


def suggest(rows: list[dict], count: int = 4, seconds: float = 30.0) -> list[dict]:
    """Candidate windows: runs of consecutive lines that have audio.

    A helper for choosing, not a scene detector. It groups lines by the gaps
    between them and says what each group contains, so a person picking the
    excerpts can see how many speakers and how much dialogue they would get.
    """
    playable = sorted((r for r in rows if r["clip"]), key=lambda r: r["start"])
    groups: list[list[dict]] = []
    for row in playable:
        if groups and row["start"] - groups[-1][-1]["end"] <= 2.5 and \
                row["end"] - groups[-1][0]["start"] <= seconds:
            groups[-1].append(row)
        else:
            groups.append([row])
    scored = []
    for group in groups:
        span = group[-1]["end"] - group[0]["start"]
        speakers = {row["speaker"] for row in group}
        scored.append({
            "start": round(max(0.0, group[0]["start"] - PAD_SECONDS), 2),
            "end": round(group[-1]["end"] + PAD_SECONDS, 2),
            "lines": len(group), "speakers": sorted(speakers),
            "seconds": round(span, 2),
            # More speakers and more lines in a short window is a better
            # listening test than one long monologue.
            "score": len(speakers) * 10 + len(group),
        })
    scored.sort(key=lambda row: (-row["score"], row["start"]))
    return scored[:count]


# --------------------------------------------------------------------------
# Cutting the bounded media
# --------------------------------------------------------------------------

def cut_media(source: Path, scene: Scene, dest: Path, cancel=None) -> Path:
    """Cut one scene out of the original, keeping the picture and every track.

    The video is re-encoded rather than stream-copied: a copy snaps to the
    nearest keyframe, which would move the cut by up to a couple of seconds and
    silently put every cue at the wrong time. An excerpt whose timeline is
    wrong is not a smaller version of the test, it is a different test.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    request = {"source": stamp(source), "start": round(scene.start, 3),
               "end": round(scene.end, 3), "version": 3}
    receipt = dest.with_suffix(".cut.json")
    if dest.is_file() and _read(receipt) == request:
        return dest
    temp = dest.with_name(dest.stem + ".partial" + dest.suffix)
    try:
        # Video and audio only, both re-encoded. Three deliberate choices:
        #
        # - Re-encoded rather than stream-copied, because a copy snaps to the
        #   nearest keyframe and would move the cut by seconds. An excerpt
        #   whose timeline is wrong is not a smaller test, it is a different
        #   one, and every cue in it would be at the wrong time.
        # - Subtitles and attachments are dropped. A subtitle event that starts
        #   inside the window and ends outside it keeps the container alive
        #   past `-t`, so the excerpt ends up longer than the window it claims
        #   to be — which is exactly what the export check then reports as a
        #   truncated dub. Neither stream is part of a listening test.
        # - `-map 0:v:0` takes one video stream: a cover-art or thumbnail
        #   stream mapped as video would be encoded as a still for the whole
        #   excerpt.
        run_ffmpeg(["-y", "-ss", f"{scene.start:g}", "-i", str(source),
                    "-t", f"{scene.duration:g}", "-map", "0:v:0", "-map", "0:a",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                    "-c:a", "aac", "-b:a", "192k",
                    "-avoid_negative_ts", "make_zero",
                    str(temp)], cancel=cancel)
    except FFmpegError as exc:
        temp.unlink(missing_ok=True)
        raise ComparisonError(f"the source scene could not be cut: {exc}") from exc
    temp.replace(dest)
    write_json(receipt, request)
    return dest


def cut_stem(source: Path, scene: Scene, dest: Path, cancel=None,
             stream: int | None = None) -> Path:
    """Cut one audio stream for the scene, as 48 kHz stereo PCM.

    `stream` is an audio-relative index and is not optional in spirit: a file
    with an English dub before the Japanese original hands ffmpeg's default
    pick the wrong one, and a comparison whose "original" button plays another
    dub is worse than having no button at all. It is `None` only for a stem
    file that has exactly one stream.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    temp = dest.with_suffix(".partial.wav")
    args = ["-y", "-ss", f"{scene.start:g}", "-i", str(source)]
    if stream is not None:
        args += ["-map", f"0:a:{stream}"]
    args += ["-t", f"{scene.duration:g}", "-vn", "-ac", "2", "-ar", "48000",
             "-c:a", "pcm_s16le", str(temp)]
    run_ffmpeg(args, cancel=cancel)
    temp.replace(dest)
    return dest


def cut_references(media: Path, scene: Scene, scene_dir: Path, chosen: dict,
                   cancel=None) -> list[dict]:
    """Cut every audio track the source carries for this scene, each labelled.

    The distinction the labels carry is the whole reason this returns more than
    one file. The stream the pipeline dubbed from is the **original
    performance** and is the reference for whether a line was translated and
    acted faithfully. Any other track on the disc is **another dub** — useful
    for hearing what a professional localisation did with the same scene, and
    no evidence at all about the original. Collapsing the two, which is what
    playing whichever stream ffmpeg defaults to amounts to, is how a listener
    ends up comparing one dub against another and calling it the source.
    """
    rows = []
    for entry in chosen.get("available", [])[:MAX_REFERENCES]:
        index = entry["audio_index"]
        is_original = index == chosen.get("audio_index")
        language = entry.get("language") or "und"
        name = "source" if is_original else f"reference-{language}-{index}"
        path = cut_stem(media, scene, scene_dir / f"{name}.wav", cancel, stream=index)
        rows.append({
            "path": str(path),
            "audio_index": index,
            "language": language,
            "title": entry.get("title", ""),
            "role": "original" if is_original else "reference dub",
            "label": (f"Original scene ({language})" if is_original
                      else f"Reference dub ({language})"),
            "note": ("the track this dub was made from; the reference for whether a "
                     "line was translated and acted faithfully" if is_original else
                     "another localisation of the same scene. Useful for comparison, "
                     "and not evidence about the original performance"),
            "measured": measure(path),
        })
    return rows


def source_stream(media: Path, source_lang: str, cancel=None) -> dict:
    """Which audio stream of `media` is the original performance.

    Reuses the pipeline's own selection rather than a second rule, and returns
    what it chose *and* what else was there, because "we used the Japanese
    track and this file also carries an English dub" is exactly the sentence a
    listening test has to be able to say.
    """
    from .stages.extract import _select_audio_stream

    probe = DubJob(input_file=Path(media), source_lang=source_lang, target_lang="und")
    absolute = _select_audio_stream(probe, cancel)
    layout = json.loads(run_ffprobe(
        ["-v", "error", "-select_streams", "a", "-show_entries",
         "stream=index:stream_tags=language,title", "-of", "json", str(media)],
        cancel=cancel))
    streams = layout.get("streams", [])
    chosen = next((i for i, row in enumerate(streams)
                   if row.get("index") == absolute), 0)
    return {
        "audio_index": chosen,
        "container_index": absolute,
        "language": (streams[chosen].get("tags") or {}).get("language", "und")
        if chosen < len(streams) else "und",
        "available": [{"audio_index": i,
                       "language": (row.get("tags") or {}).get("language", "und"),
                       "title": (row.get("tags") or {}).get("title", "")}
                      for i, row in enumerate(streams)],
    }


def seed_separation(job: DubJob, shared_work: Path, scene: Scene, stems: dict,
                    model: str, cancel=None) -> dict:
    """Pre-place this project's real separated stems for the excerpt.

    Demucs is a heavy optional dependency and re-running it on every excerpt
    would be both slow and a different separation from the one the reviewed dub
    was built on. The stems that run produced are cut to the window and
    registered through the normal artifact receipt, so `separate` finds its own
    cache rather than being bypassed.

    Without stems the run falls back to the original mix, which leaves the
    original performance under the dub. That is a real difference and it is
    reported, never hidden.
    """
    from .stages.common import work_stem

    stem = work_stem(job)
    vocals = shared_work / f"{stem}.vocals.wav"
    background = shared_work / f"{stem}.background.wav"
    if not stems.get("vocals") or not stems.get("background"):
        return {"separated": False,
                "note": ("no separated stems were supplied, so the bed under the "
                         "dub is the original mix at a reduced level and the "
                         "original dialogue is still in it")}
    shared_work.mkdir(parents=True, exist_ok=True)
    cut_stem(Path(stems["vocals"]), scene, vocals, cancel)
    cut_stem(Path(stems["background"]), scene, background, cancel)
    record([vocals, background], {"source": stamp(job.source_audio), "model": model})
    return {"separated": True, "vocals": str(vocals), "background": str(background),
            "note": ("this project's own Demucs output, cut to the scene; it is an "
                     "estimate of the music and effects, not a studio M&E stem")}


# --------------------------------------------------------------------------
# Building the job whose takes are already rendered
# --------------------------------------------------------------------------

def import_takes(job: DubJob, scene: Scene) -> list[dict]:
    """Register each imported clip as the selected take for its cue.

    `Selection(reason="restored")` is the existing contract `synthesize`
    already reads: a selection a person or a resume put there is a decision,
    not a cache entry, and the stage keeps it instead of generating over it.
    Using it here is what makes the whole comparison cost zero requests.
    """
    imported = []
    for seg, row in zip(job.segments, scene.cues, strict=True):
        clip = Path(row["clip"])
        # The take's fingerprint names where the audio came from, not a request
        # that was never made on this machine. An imported take is honestly an
        # import: its origin says so and nothing claims it was generated here.
        fingerprint = digest({"import": str(clip.resolve()), "size": clip.stat().st_size,
                              "cue": seg.cue_id})[:16]
        identifier = f"imported-{fingerprint[:12]}"
        take = Take(
            take_id=identifier, fingerprint=fingerprint, engine="imported",
            profile=row["speaker"], text=row["text_translated"] or row["text_src"],
            direction=row["delivery"], origin="import", state="reused",
            raw=Artifact(role=RAW, path=str(clip), fingerprint=fingerprint,
                         bytes=clip.stat().st_size),
        )
        seg.audio.takes = [take]
        seg.audio.selection = Selection(
            take_id=identifier, reason="restored", actor="comparison-import",
            at=now())
        seg.audio_clip = clip
        imported.append({"cue": seg.cue_id, "line": seg.index, "take": identifier,
                         "clip": str(clip), "bytes": clip.stat().st_size})
    return imported


def build_job(config: Config, excerpt: Path, scene: Scene, *, source_lang: str,
              target_lang: str, target_locale: str, speakers: list[str]) -> DubJob:
    """A job for one excerpt whose cue times are the excerpt's own timeline."""
    job = DubJob(input_file=excerpt, source_lang=source_lang,
                 target_lang=target_lang,
                 target_locale=target_locale or resolve_target_locale(
                     config.as_dict(), target_lang))
    job.script_is_target = True   # the imported text is already the dub's words
    job.script_lang = target_lang
    job.script_ref = script_ref(excerpt, source_lang)
    job.speakers = {label: Speaker(label=label) for label in speakers}
    for position, row in enumerate(scene.cues):
        seg = Segment(
            index=position,
            start=round(max(0.0, row["start"] - scene.start), 4),
            end=round(row["end"] - scene.start, 4),
            text_src=row["text_src"],
            speaker=row["speaker"],
            text_translated=row["text_translated"] or row["text_src"],
            delivery=row["delivery"],
        )
        adopt_legacy(seg, job.script_ref)
        # SOURCE time is *this job's* source medium, and this job's source
        # medium is the excerpt. Writing the episode's own timestamps here
        # would be the unlabeled-time mistake one level up: source measurement
        # reads `DubJob.source_track`, which is the excerpt, and a span at
        # 221 s would send it past the end of a 32-second file and come back
        # with nothing to measure. Where the scene sits in the episode is
        # recorded in the comparison manifest, which is where a fact about the
        # episode belongs.
        seg.source.spans = [Span(round(max(0.0, row["start"] - scene.start), 4),
                                 round(row["end"] - scene.start, 4), SOURCE)]
        seg.source.speaker = row["speaker"]
        seg.source_start = seg.source.spans[0].start
        job.segments.append(seg)
    return job


def prepare(config: Config, excerpt: Path, scene: Scene, *, source_lang: str,
            target_lang: str, target_locale: str, stems: dict, cancel=None) -> dict:
    """Build the excerpt's job, import its takes and seed its script cache.

    `config` must be the same config the variant will run with, work directory
    included: the script cache the pipeline restores is keyed on the extracted
    audio's stamp and on the transcription options, so a cache written under
    different settings is correctly judged stale and thrown away.
    """
    speakers = sorted({row["speaker"] for row in scene.cues})
    job = build_job(config, excerpt, scene, source_lang=source_lang,
                    target_lang=target_lang, target_locale=target_locale,
                    speakers=speakers)
    shared_work = media_work(config.work_dir, job)
    locale_ns = job.target_locale or job.target_lang
    work = shared_work / locale_ns
    # `extract` is run here, not reimplemented: the script cache the pipeline
    # restores is keyed on the extracted audio's stamp, so the real stage has
    # to have produced it before the cache can match.
    extract.run(job, shared_work, cancel=cancel)
    separation = seed_separation(job, shared_work, scene,
                                 stems, config["separate"]["model"], cancel)
    # Mirrors `transcribe.run`: the options it will compute must already be on
    # the payload or the cache will be judged stale and the stage will try to
    # transcribe an excerpt nobody asked it to transcribe.
    job.transcription_options = extract_transcription_options(config)
    # Mirrors `run_job`, for the same reason: a script cache whose translation
    # options differ from the run's is treated as a different translation and
    # its text is dropped. Here the text is the whole point of the import.
    job.translation_options = dict(config["translate"])
    job.translation_options["target_locale"] = job.target_locale
    imported = import_takes(job, scene)
    save_script(job, work)
    return {
        "excerpt": str(excerpt),
        "work": str(work),
        "shared_work": str(shared_work),
        "source_audio": str(job.source_audio) if job.source_audio else None,
        "separation": separation,
        "speakers": speakers,
        "imported": imported,
        "script_ref": job.script_ref,
        # Both timelines, named. `excerpt` and `source` are this job's own
        # domains; `episode` is where the line sits in the original media, kept
        # here rather than inside a cue record so nothing downstream can mistake
        # it for a time it may seek to.
        "cues": [{"cue": s.cue_id, "line": s.index,
                  "excerpt": Span(s.start, s.end, "target").as_dict(),
                  "source": [sp.as_dict() for sp in s.source.spans],
                  "episode": [round(s.source.spans[0].start + scene.start, 3),
                              round(s.source.spans[0].end + scene.start, 3)],
                  "speaker": s.speaker, "text": s.text_translated}
                 for s in job.segments],
        "episode_offset": round(scene.start, 3),
    }


def extract_transcription_options(config: Config) -> dict:
    """The `transcription_options` `transcribe.run` will compute for a subtitle run.

    Kept next to the caller that must match it. `transcribe` builds this from
    the same two pieces — the non-default source/limit keys, then the whole
    transcribe section — and a cache whose options differ is treated as stale.
    """
    return dict(config["transcribe"])


# --------------------------------------------------------------------------
# Rendering the variants
# --------------------------------------------------------------------------

def variant_config(config: Config, scene_dir: Path, name: str, speakers: list[str],
                   overrides: dict) -> Config:
    """The config one variant runs under, isolated from every other variant.

    Each variant gets its own work and output directory so it is a genuine
    cold render rather than a resume of the variant before it. Sharing one work
    directory would still be *correct* — every stage reads its declared
    upstream artifact — but it would make the second variant a warm run, and
    then the cost it reports would be the cost of not being first.
    """
    return config.with_overrides({
        "paths.work_dir": str(scene_dir / "work" / name),
        "paths.output_dir": str(scene_dir / "out" / name),
        "dub.dry_run": False,
        "dub.voice_mode": "preset",
        "dub.preset_voices": speakers,
        "dub.preserve_versions": False,
        "transcribe.diarize": False,
        # The words are fixed. They came from a completed, reviewed run and
        # they are the same in every variant by construction — re-translating
        # would change what is being said between the two files and make the
        # comparison meaningless. `passthrough` keeps the imported text and
        # needs no provider, so the runner works with no network at all.
        "translate.provider": "passthrough",
        "translate.adapt_region": False,
        "translate.reuse_memory": False,
        # Recognition is off in every variant. It costs requests, it is not
        # what is being compared, and letting one variant run it would make the
        # two runs differ in something other than the thing under test.
        "quality.asr": "off",
        **overrides,
    })


def render_variant(config: Config, prepared: dict, scene: Scene, name: str,
                   scene_dir: Path, *, source_lang: str, target_lang: str,
                   target_locale: str, cancel=None) -> dict:
    """Run the real pipeline once for one settings variant, generating nothing."""
    excerpt = Path(prepared["excerpt"])
    speakers = prepared["speakers"]
    job = DubJob(input_file=excerpt, source_lang=source_lang,
                 target_lang=target_lang, target_locale=target_locale)
    engine = RefusesToGenerate(speakers)
    services = Services(config)
    services._cache["voicebox"] = engine   # the documented injection point
    started = time.perf_counter()
    run_job(job, config, services=services, cancel_event=cancel)
    elapsed = round(time.perf_counter() - started, 2)
    if engine.attempts:  # pragma: no cover - run_job raises first
        raise ComparisonError("a variant generated speech; the comparison is void")
    mixed = scene_dir / f"{name}.wav"
    exported = scene_dir / f"{name}{excerpt.suffix}"
    if job.dubbed_track and Path(job.dubbed_track).is_file():
        shutil.copy2(job.dubbed_track, mixed)
    if job.output_file and Path(job.output_file).is_file():
        shutil.copy2(job.output_file, exported)
    return {
        "name": name,
        "settings": dict(prepared.get("overrides") or {}),
        "mixed": str(mixed) if mixed.is_file() else None,
        "mixed_sha256": _sha(mixed),
        "exported": str(exported) if exported.is_file() else None,
        "exported_sha256": _sha(exported),
        "tts_requests": len(engine.attempts),
        "seconds": elapsed,
        "work_bytes": _tree_bytes(Path(config.work_dir)),
        "active": activation(job),
        "delivery": job.delivery,
        "metrics": {k: v for k, v in sorted(job.metrics.items())
                    if k not in ("mix_fingerprint", "timing_translation_usage")},
        "lines": [_line_row(seg) for seg in job.segments],
    }


def activation(job: DubJob) -> dict:
    """Which features actually did something in this run, read back from it.

    This is the answer to the ambiguity the early sample left behind. "The
    dub sounds the same" and "the feature was off, fell back, or was
    unsupported on this scene" are different findings, and only one of them is
    about the audio.
    """
    segments = job.segments
    return {
        "timing": {
            "modes": sorted({s.phrasing.mode for s in segments}),
            "states": sorted({s.phrasing.state for s in segments}),
            "planned": sum(1 for s in segments if s.phrasing.planned),
            "fallbacks": job.metrics.get("phrase_fallbacks", 0),
            "stretched": job.metrics.get("stretched_lines", 0),
        },
        "levels": {
            "mode": job.metrics.get("levels_mode", "legacy"),
            "applied": job.metrics.get("levels_applied", 0),
            "clamped": job.metrics.get("levels_clamped", 0),
            "outcomes": sorted({s.level.outcome for s in segments}),
        },
        "boundaries": {
            "decisions": sorted({s.preparation.decision for s in segments}),
            "edge_fades": job.metrics.get("edge_fades", 0),
        },
        "treatments": job.metrics.get("treatments", {}),
        "coverage": job.metrics.get("coverage", {}),
        "conversation": job.metrics.get("conversation", {}),
        "requests": job.metrics.get("request_budget", {}),
    }


def _line_row(seg) -> dict:
    current = seg.audio.current()
    return {
        "cue": seg.cue_id, "line": seg.index, "speaker": seg.speaker,
        "window": [seg.start, seg.end],
        "role": current.role if current else None,
        "duration": current.duration if current else None,
        "timing": {"mode": seg.phrasing.mode, "state": seg.phrasing.state,
                   "reason": seg.phrasing.reason,
                   "planned": seg.phrasing.planned_duration,
                   "actual": seg.phrasing.actual_duration},
        "level": {"mode": seg.level.mode, "outcome": seg.level.outcome,
                  "applied_db": seg.level.applied_db},
        "treatment": {"preset": seg.treatment.preset, "outcome": seg.treatment.outcome,
                      "tail": seg.treatment.tail, "makeup": seg.treatment.makeup,
                      "capability": seg.treatment.capability},
        "findings": sorted((f.code, f.severity) for f in seg.findings
                           if f.disposition != "obsolete"),
    }


# --------------------------------------------------------------------------
# Objective checks and level-matched playback
# --------------------------------------------------------------------------

def check_takes(scene: Scene, vb, language: str) -> list[dict]:
    """Listen to the imported takes once and say what a recognizer hears.

    Run against the *shared* takes rather than per variant, because that is
    what they are: every variant plays the same audio, so a wrong word or a
    runaway vocalisation is a property of the material being compared and not
    of either side of the comparison. Forcing recognition off inside the
    variants keeps them identical; leaving it off everywhere would hand a
    listener a defect and no way to know it was already measurable.

    A missing or failing recognizer is recorded as unchecked. It is never a
    reason to fail the comparison — the takes are still the takes.
    """
    from . import verify as content

    rows = []
    for position, row in enumerate(scene.cues):
        asked = row.get("spoken_text") or row["text_translated"] or row["text_src"]
        entry = {"line": position, "speaker": row["speaker"], "asked": asked,
                 "state": "skipped", "heard": "", "reason": "",
                 "from_receipt": bool(row.get("spoken_text"))}
        if vb is None:
            entry["reason"] = "no recognizer is configured"
            rows.append(entry)
            continue
        try:
            heard = (vb.transcribe(Path(row["clip"]), language=language) or {}).get("text", "")
        except Exception as exc:  # noqa: BLE001 - any failure is the same answer
            entry.update(state="failed", reason=str(exc)[:200])
            rows.append(entry)
            continue
        result = content.compare(asked, heard, language)
        entry.update(state=result.get("state", "unknown"), heard=heard,
                     reason=result.get("reason", ""))
        rows.append(entry)
    return rows


def take_findings(scene_rows: list[dict]) -> list[str]:
    """The takes a recognizer disagreed with, as sentences for the results sheet."""
    found = []
    for scene in scene_rows:
        for row in scene.get("takes") or []:
            if row["state"] != "mismatch":
                continue
            found.append(
                f"scene {scene['scene']['index']} line {row['line']} "
                f"({row['speaker']}): asked “{row['asked']}”, heard "
                f"“{row['heard'][:70]}{'…' if len(row['heard']) > 70 else ''}” — "
                f"{row['reason']}")
    return found


def measure(path: Path) -> dict:
    """Speech-active level, peak and duration of one rendered scene."""
    try:
        stats = analyze_levels(Path(path))
    except Exception as exc:  # noqa: BLE001 - any read failure is the same answer here
        return {"state": "unavailable", "reason": str(exc)}
    return {"state": "measured", "speech_db": stats["speech_db"],
            "peak": round(stats["peak"], 4), "duration": round(stats["duration"], 3),
            "units": "dBFS-rms-speech", "method": "speech-rms/1"}


def level_match(rows: list[dict], destination: Path, cancel=None) -> list[dict]:
    """Make a separately labelled, level-matched copy of each variant.

    Applied to the *playback copy* and never to production audio: matching the
    per-line levels would erase exactly the whisper-to-shout contrast the level
    work exists to create. A louder variant is not a better variant, and this
    is the control that stops the listening test from concluding that it is.
    """
    usable = [row for row in rows
              if row.get("mixed") and (row.get("measured") or {}).get("speech_db") is not None]
    if len(usable) < 2:
        return rows
    reference = max(r["measured"]["speech_db"] for r in usable)
    for row in usable:
        gain = round(reference - row["measured"]["speech_db"], 3)
        source = Path(row["mixed"])
        dest = destination / f"{row['name']}-matched.wav"
        try:
            run_ffmpeg(["-y", "-i", str(source), "-af", f"volume={gain:.3f}dB",
                        "-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le",
                        str(dest)], cancel=cancel)
        except FFmpegError as exc:
            row["matched"] = None
            row["matched_note"] = f"level-matched copy failed: {exc}"
            continue
        row["matched"] = str(dest)
        row["matched_gain_db"] = gain
        row["matched_note"] = (
            f"playback copy only, {gain:+.2f} dB, so timbre and artefacts can be "
            f"judged without the louder variant winning by being louder")
    return rows


def objective(scene_rows: list[dict]) -> dict:
    """Every check that has to pass before a person is asked to listen.

    Deliberately blunt: a comparison whose variants have different durations,
    or whose two settings both resolved to the same thing, is not a test and
    should not be presented as one.
    """
    problems: list[str] = []
    notes: list[str] = []
    for scene in scene_rows:
        variants = scene["variants"]
        playable = [v for v in variants if v.get("mixed")]
        if len(playable) < 2:
            problems.append(f"scene {scene['scene']['index']}: fewer than two "
                            f"variants produced playable audio")
            continue
        lengths = [(v.get("measured") or {}).get("duration") for v in playable]
        known = [length for length in lengths if length]
        if known and max(known) - min(known) > 0.25:
            problems.append(
                f"scene {scene['scene']['index']}: variants differ in length by "
                f"{max(known) - min(known):.2f}s, so they are not the same window")
        digests = {v.get("mixed_sha256") for v in playable}
        if len(digests) == 1:
            notes.append(
                f"scene {scene['scene']['index']}: every variant rendered the same "
                f"bytes. The settings changed nothing here — that is a real result "
                f"and it is not a listening result.")
        generated = sum(v.get("tts_requests", 0) for v in variants)
        if generated:
            problems.append(f"scene {scene['scene']['index']}: {generated} speech "
                            f"request(s) were made; this is not a same-take comparison")
        for variant in variants:
            report = variant.get("delivery") or {}
            failures = [f["code"] for f in report.get("findings") or []
                        if f["severity"] == "failure"]
            if failures:
                problems.append(f"scene {scene['scene']['index']} / {variant['name']}: "
                                f"export validation failed ({', '.join(failures)})")
    return {"ready": not problems, "problems": problems, "notes": notes}


def blind_labels(names: list[str], seed: str) -> dict:
    """Neutral A/B labels with a mapping that is always revealable.

    Randomised order so the first file is not always the baseline, and
    deterministic from the comparison id so the same test relabels the same way
    if it is rebuilt. The mapping lives in the manifest in plain text: blind
    labelling is a fairness aid, not a secret, and a listener who wants to know
    which is which is entitled to find out.
    """
    order = sorted(names, key=lambda name: digest({"seed": seed, "name": name}))
    letters = [chr(ord("A") + i) for i in range(len(order))]
    return dict(zip(letters, order, strict=True))


# --------------------------------------------------------------------------
# The deliverable
# --------------------------------------------------------------------------

def run(config: Config, *, comparison_id: str, source: Path, script: Path,
        clips: Path, windows, variants: dict, root: Path, titles=None,
        stems: dict | None = None, source_lang: str = "ja",
        target_lang: str = "es", target_locale: str = "", note: str = "",
        check_content: bool = True, cancel=None) -> dict:
    """Build the whole comparison and return its manifest."""
    root = Path(root) / comparison_id
    if root.exists() and any(root.iterdir()):
        raise ComparisonError(
            f"{root} already holds a comparison. Give this run its own id rather "
            f"than overwriting evidence somebody may already have listened to.")
    root.mkdir(parents=True, exist_ok=True)
    original = source_stream(Path(source), source_lang, cancel)
    if len(original["available"]) > 1:
        log.info("comparison: the original is audio stream %d (%s); this file also "
                 "carries %s", original["audio_index"], original["language"],
                 ", ".join(row["language"] for row in original["available"]
                           if row["audio_index"] != original["audio_index"]))
    payload = read_script(Path(script))
    rows = inventory(payload, Path(clips))
    scenes = scenes_from(rows, windows, titles)
    recognizer = None
    if check_content:
        # Best effort. A comparison must still build with nothing listening.
        try:
            recognizer = Services(config).voicebox
        except Exception as exc:  # noqa: BLE001 - unconfigured is not an error here
            log.info("comparison: no recognizer available (%s); the imported takes "
                     "will not be checked", exc)
    scene_rows = []
    for scene in scenes:
        log.info("comparison: scene %d (%s), %d line(s)", scene.index, scene.title,
                 len(scene.cues))
        scene_dir = root / f"scene-{scene.index:02d}"
        scene_dir.mkdir(parents=True, exist_ok=True)
        # One excerpt per scene, cut once and frozen. Every variant reads this
        # same file, which is what makes "the same window, the same cast, the
        # same cut" a fact about the comparison rather than an intention.
        excerpt = cut_media(Path(source), scene, scene_dir / "excerpt.mkv", cancel)
        references = cut_references(Path(source), scene, scene_dir, original, cancel)
        source_excerpt = Path(next(r["path"] for r in references
                                   if r["role"] == "original"))
        speakers = sorted({row["speaker"] for row in scene.cues})
        rendered = []
        preparations = {}
        for name, overrides in variants.items():
            log.info("comparison:   variant %s", name)
            settings = variant_config(config, scene_dir, name, speakers, overrides)
            prepared = prepare(settings, excerpt, scene, source_lang=source_lang,
                               target_lang=target_lang, target_locale=target_locale,
                               stems=dict(stems or {}), cancel=cancel)
            prepared["overrides"] = dict(overrides)
            preparations[name] = prepared
            row = render_variant(settings, prepared, scene, name, scene_dir,
                                 source_lang=source_lang, target_lang=target_lang,
                                 target_locale=target_locale, cancel=cancel)
            row["measured"] = measure(Path(row["mixed"])) if row["mixed"] else {}
            rendered.append(row)
        rendered = level_match(rendered, scene_dir, cancel)
        scene_rows.append({
            "scene": scene.as_dict(),
            "excerpt": str(excerpt),
            "prepared": preparations,
            "source_excerpt": str(source_excerpt),
            "source_measured": measure(source_excerpt),
            "references": references,
            "variants": rendered,
            # Once, on the shared takes — not per variant, because it is the
            # same audio in all of them.
            "takes": check_takes(scene, recognizer, target_lang),
        })
    manifest = {
        "manifest_version": MANIFEST_VERSION,
        "comparison_id": comparison_id,
        "note": note,
        "source": {"media": str(Path(source).resolve()), "stamp": stamp(Path(source)),
                   "stream": original,
                   "script": str(Path(script).resolve()), "clips": str(Path(clips).resolve()),
                   "source_language": source_lang, "target_language": target_lang,
                   "target_locale": target_locale or target_lang,
                   "stems": {k: str(v) for k, v in (stems or {}).items()}},
        "variants": {name: dict(overrides) for name, overrides in variants.items()},
        "labels": blind_labels(list(variants), comparison_id),
        "scenes": scene_rows,
        "objective": objective(scene_rows),
        "take_findings": take_findings(scene_rows),
        "honesty": [
            "Every variant reused the takes imported from a completed run. No "
            "speech was generated for this comparison and the engine handed to "
            "each run refuses to generate at all.",
            "This is a bounded excerpt. It says nothing about an episode nobody "
            "has listened to.",
            "A passing objective check means the files are comparable. Whether "
            "the dub is better is not established by anything in this manifest.",
        ],
    }
    write_json(root / "manifest.json", manifest)
    (root / "results.md").write_text(results_sheet(manifest), encoding="utf-8")
    (root / "index.html").write_text(page(manifest, root), encoding="utf-8")
    return manifest


def results_sheet(manifest: dict) -> str:
    """An empty results sheet. It is empty because nobody has listened yet."""
    lines = [
        f"# Listening results — {manifest['comparison_id']}",
        "",
        "Fill in what you actually heard. `same` and `worse` are real answers and",
        "are as useful as `better`; leaving a row blank is better than guessing.",
        "",
        "Neutral labels and what they are:",
        "",
        "| Label | Variant |",
        "| --- | --- |",
    ]
    for label, name in manifest["labels"].items():
        lines.append(f"| {label} | `{name}` |")
    flagged = manifest.get("take_findings") or []
    if flagged:
        lines += ["", "## Before you listen: the shared takes have known defects", "",
                  "A recognizer was run once over the imported takes — the same audio",
                  "every variant plays — and disagreed with these. They are defects in",
                  "the generated speech itself, present identically in every variant,",
                  "and no processing setting on either side can fix them:", ""]
        lines += [f"- {row}" for row in flagged]
        lines += ["",
                  "Recognition proves a wrong word, never a right one: a line it agrees",
                  "with may still be badly acted, and a name it misreads may be fine.",
                  ""]
    lines += ["", "## What was actually active", "",
              "Read this before listening. A feature that fell back on this material",
              "and a feature that made no audible difference are different findings,",
              "and only one of them is about the audio.", ""]
    lines += activation_report(manifest)
    lines += ["", "## Per scene", ""]
    for row in manifest["scenes"]:
        scene = row["scene"]
        lines += [
            f"### Scene {scene['index']} — {scene['title']} "
            f"({scene['duration']:.1f}s, {scene['lines']} line(s), "
            f"{', '.join(scene['speakers'])})",
            "",
            "| Dimension | better / same / worse / uncertain | What stood out |",
            "| --- | --- | --- |",
            "| Words (right words, no missing or wrong ones) | | |",
            "| Timing (rhythm, pauses, rushed endings, overlap) | | |",
            "| Performance (energy, believability, identity) | | |",
            "| Level contrast (whisper vs shout, audible over the bed) | | |",
            "| Reactions (missing, doubled, plausible) | | |",
            "| Space (room, distance, device; tails and joins) | | |",
            "| Overall preference | | |",
            "",
        ]
    lines += [
        "## Overall",
        "",
        "- Which version would you choose, and why?",
        "- Anything wrong or missing at an approximate timestamp?",
        "- Devices used (headphones / speaker / phone):",
        "",
        "## What this test cannot tell us",
        "",
        "- Whether an unreviewed episode is acceptable. This is a bounded excerpt.",
        "- Whether the translation means the right thing. That needs a fluent reader,",
        "  and no recognizer agreement substitutes for it.",
        "",
    ]
    return "\n".join(lines)


def activation_report(manifest: dict) -> list[str]:
    """Per variant, what each feature actually did across every scene.

    Counted from the rendered runs' own records rather than from the settings
    that were requested. `timing.mode = phrase` that fell back to the bounded
    whole-line path on every line is *on* and *not doing anything*, and a
    listener told only that it was enabled would draw the wrong conclusion
    from hearing no difference.
    """
    rows: list[str] = []
    for name in manifest["variants"]:
        timing: dict[str, int] = {}
        levels: dict[str, int] = {}
        space: dict[str, int] = {}
        gains: list[float] = []
        events = stretched = 0
        reasons: dict[str, int] = {}
        for scene in manifest["scenes"]:
            variant = next((v for v in scene["variants"] if v["name"] == name), None)
            if variant is None:
                continue
            stretched += variant["active"]["timing"].get("stretched", 0)
            events += int((variant["active"]["coverage"] or {}).get("events", 0))
            for line in variant["lines"]:
                timing[line["timing"]["state"]] = timing.get(line["timing"]["state"], 0) + 1
                levels[line["level"]["outcome"]] = levels.get(line["level"]["outcome"], 0) + 1
                space[line["treatment"]["outcome"]] = space.get(
                    line["treatment"]["outcome"], 0) + 1
                if line["level"]["outcome"] in ("applied", "clamped"):
                    gains.append(float(line["level"]["applied_db"] or 0.0))
                reason = (line["timing"]["reason"] or "").split(";")[0].strip()
                if reason:
                    reasons[reason] = reasons.get(reason, 0) + 1
        total = sum(timing.values()) or 1
        rows.append(f"### `{name}`")
        rows.append("")
        rows.append(f"- Timing: {_tally(timing)} across {total} lines; "
                    f"{stretched} line(s) time-compressed.")
        if reasons:
            common = max(reasons.items(), key=lambda item: item[1])
            rows.append(f"  Most common reason: “{common[0]}” ({common[1]} of {total}).")
        rows.append(f"- Levels: {_tally(levels)}."
                    + (f" Source-relative gain ranged {min(gains):+.1f} to "
                       f"{max(gains):+.1f} dB." if gains else
                       " No source-relative gain was applied."))
        rows.append(f"- Space: {_tally(space)}.")
        rows.append(f"- Coverage: {events} nonverbal event(s) were known in these scenes."
                    + (" With none, a coverage setting had nothing to do and changed "
                       "no audio." if not events else ""))
        rows.append("")
    return rows


def _tally(counts: dict) -> str:
    if not counts:
        return "nothing recorded"
    return ", ".join(f"{value} {key}" for key, value in sorted(counts.items()))


def page(manifest: dict, root: Path) -> str:
    """A small local page with working playback for every scene and variant.

    One audio element, deliberately: two would happily play over each other the
    moment a switch races a load, and a comparison where both versions are
    audible at once is not a comparison.
    """
    def rel(path) -> str:
        try:
            return Path(path).resolve().relative_to(Path(root).resolve()).as_posix()
        except (ValueError, TypeError):
            return ""

    reveal = ", ".join(f"{label} = {name}" for label, name in manifest["labels"].items())
    blocks = []
    for row in manifest["scenes"]:
        scene = row["scene"]
        references = row.get("references") or [
            {"path": row["source_excerpt"], "label": "Original scene",
             "note": "", "role": "original"}]
        buttons = [
            f'<button data-src="{rel(ref["path"])}" class="ref '
            f'{"is-original" if ref["role"] == "original" else "is-dub"}" '
            f'title="{_esc(ref["note"])}">{_esc(ref["label"])}</button>'
            for ref in sorted(references, key=lambda r: r["role"] != "original")]
        for label, name in manifest["labels"].items():
            variant = next(v for v in row["variants"] if v["name"] == name)
            if variant.get("mixed"):
                buttons.append(f'<button data-src="{rel(variant["mixed"])}">'
                               f'{label} — actual level</button>')
            if variant.get("matched"):
                buttons.append(f'<button data-src="{rel(variant["matched"])}" '
                               f'class="matched">{label} — level-matched '
                               f'({variant["matched_gain_db"]:+.1f} dB)</button>')
        script_lines = "".join(
            f"<tr><td>{line['start']:.1f}s</td><td>{_esc(line['speaker'])}</td>"
            f"<td>{_esc(line['source'])}</td><td>{_esc(line['dub'] or '')}"
            + (f"<br><span class='was'>cached script said: "
               f"{_esc(line.get('script') or '')}</span>"
               if line.get("stale_script") else "")
            + "</td></tr>"
            for line in scene["text"])
        blocks.append(f"""
  <section>
    <h2>Scene {scene['index']} — {_esc(scene['title'])}</h2>
    <p class="meta">{scene['duration']:.1f}s · {scene['lines']} line(s) ·
       {_esc(', '.join(scene['speakers']))} ·
       source window {scene['window']['start']:.1f}–{scene['window']['end']:.1f}s</p>
    <div class="row">{''.join(buttons)}</div>
    <table><thead><tr><th>At</th><th>Speaker</th><th>Original</th><th>Dub</th></tr></thead>
    <tbody>{script_lines}</tbody></table>
  </section>""")

    problems = manifest["objective"]["problems"]
    notes = manifest["objective"]["notes"]
    status = ("<p class='ok'>Objective checks passed: the variants are comparable.</p>"
              if not problems else
              "<p class='bad'>Objective checks found problems:</p><ul>"
              + "".join(f"<li>{_esc(p)}</li>" for p in problems) + "</ul>")
    if notes:
        status += "<ul class='note'>" + "".join(f"<li>{_esc(n)}</li>" for n in notes) + "</ul>"
    return f"""<!doctype html>
<meta charset="utf-8">
<title>Doblarr comparison — {_esc(manifest['comparison_id'])}</title>
<style>
 body {{ font: 15px/1.5 system-ui, sans-serif; max-width: 62rem; margin: 2rem auto;
         padding: 0 1rem; color: #16181d; background: #fbfbfc; }}
 h1 {{ margin-bottom: .2rem; }} h2 {{ margin-top: 2rem; }}
 .meta {{ color: #5a6069; margin-top: .2rem; }}
 .row {{ display: flex; flex-wrap: wrap; gap: .5rem; margin: .8rem 0; }}
 button {{ font: inherit; padding: .45rem .8rem; border: 1px solid #c7ccd4;
           background: #fff; border-radius: .4rem; cursor: pointer; }}
 button.playing {{ background: #16181d; color: #fff; border-color: #16181d; }}
 button.matched {{ border-style: dashed; }}
 button.is-original {{ border-color: #1a6b39; }}
 button.is-dub {{ border-color: #8a6d3b; font-style: italic; }}
 table {{ border-collapse: collapse; width: 100%; font-size: .9em; margin-top: .6rem; }}
 td, th {{ border-bottom: 1px solid #e4e7ec; padding: .3rem .5rem; text-align: left;
           vertical-align: top; }}
 .ok {{ color: #1a6b39; }} .bad {{ color: #9b1c1c; }}
 .note li {{ color: #7a5200; }}
 .was {{ color: #9a6b00; font-size: .85em; }}
 footer {{ margin-top: 3rem; color: #5a6069; font-size: .9em; }}
 #reveal {{ margin-left: .5rem; }}
</style>
<h1>Doblarr comparison — {_esc(manifest['comparison_id'])}</h1>
<p class="meta">{_esc(manifest.get('note') or '')}</p>
{status}
<p>Labels are neutral and the mapping is not a secret:
   <button id="reveal">Reveal which is which</button>
   <span id="mapping" hidden>{_esc(reveal)}</span></p>
<p class="meta">A green-edged button is the <strong>original performance</strong>
   this dub was made from. An italic one is <strong>another dub</strong> of the
   same scene — worth hearing, but not evidence about the original.</p>
<audio id="player" controls style="width:100%"></audio>
<label><input type="checkbox" id="loop"> Loop the excerpt</label>
{''.join(blocks)}
<footer>
<p>Every variant reused the same imported takes. No speech was generated for this
comparison.</p>
<p>This is a bounded excerpt and says nothing about an episode nobody has heard.
A passing objective check means the files are comparable; whether the dub is
better is not established by anything on this page.</p>
</footer>
<script>
 const player = document.getElementById('player');
 document.getElementById('loop').addEventListener('change', e =>
   player.loop = e.target.checked);
 document.getElementById('reveal').addEventListener('click', () => {{
   document.getElementById('mapping').hidden = false;
 }});
 // One element for everything, so switching never leaves two files playing.
 document.querySelectorAll('button[data-src]').forEach(button => {{
   button.addEventListener('click', () => {{
     const wasPlaying = !player.paused && !player.ended;
     const at = player.currentTime;
     document.querySelectorAll('button[data-src]').forEach(b =>
       b.classList.remove('playing'));
     button.classList.add('playing');
     player.src = button.dataset.src;
     player.load();
     player.addEventListener('loadedmetadata', () => {{
       // Keep the listener's place in the scene across a switch: comparing two
       // versions is only useful if the same moment is being compared.
       if (at && isFinite(player.duration)) {{
         player.currentTime = Math.min(at, Math.max(0, player.duration - 0.05));
       }}
       if (wasPlaying) player.play().catch(() => {{}});
     }}, {{ once: true }});
   }});
 }});
</script>
"""


def _esc(value) -> str:
    return (str(value).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _sha(path: Path) -> str | None:
    path = Path(path)
    if not path.is_file():
        return None
    found = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            found.update(chunk)
    return found.hexdigest()


def _tree_bytes(root: Path) -> int:
    """Bytes one variant's work directory holds, so the cost is a number."""
    total = 0
    for path in Path(root).rglob("*"):
        try:
            if path.is_file():
                total += path.stat().st_size
        except OSError:
            continue
    return total


def _read(path: Path) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
