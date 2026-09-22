"""Core data structures passed between pipeline stages."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .cues import (
    CueAudio,
    CueLineage,
    Finding,
    Placement,
    SourceReference,
    SourceSpans,
    SpeechPreparation,
)


@dataclass
class Segment:
    """One line of dialogue with timing, through every stage of the pipeline.

    The first block of fields is the long-standing compatibility view: `index`
    addresses a line in legacy snapshots and routes, `audio_clip` is a
    projection of whichever artifact would be mixed right now, and `issues` is
    the derived string view of `findings`.

    The typed records below own the real data (see `doblarr.cues`): `cue_id`
    plus `lineage` are the identity that survives wording, voice, gain and
    placement edits; `source` keeps the original intervals that a target-timing
    edit must never move; `placement` holds target-only extras; `audio` holds
    every take and processed derivative separately; `preparation` records what
    boundary analysis found in the selected raw take and what it did about it.
    """

    index: int
    start: float                     # seconds, target timeline
    end: float                       # seconds, target timeline
    text_src: str                    # original-language text
    speaker: str = "SPEAKER_00"      # diarization label
    text_translated: str | None = None
    audio_clip: Path | None = None   # current render for this line (projection)
    words: list[dict] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    delivery: str = ""
    voice: str | None = None
    revision: int = 0
    source_start: float | None = None
    tts_text: str | None = None          # resolved spoken form (knowledge applied)
    applied_rules: list[dict] = field(default_factory=list)  # rule ids/revisions used
    translation_provenance: dict = field(default_factory=dict)
    memory_context: dict = field(default_factory=dict)

    # Typed cue records (Plan 01). Empty values are valid: "unknown" is a real
    # state and no stage is required to fill every field on first import.
    cue_id: str = ""
    lineage: CueLineage = field(default_factory=CueLineage)
    source: SourceSpans = field(default_factory=SourceSpans)
    placement: Placement = field(default_factory=Placement)
    audio: CueAudio = field(default_factory=CueAudio)
    preparation: SpeechPreparation = field(default_factory=SpeechPreparation)
    findings: list[Finding] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    def use(self, path: Path | None) -> Path | None:
        """Point the compatibility clip projection at `path`."""
        self.audio_clip = path
        return path


@dataclass
class Speaker:
    """A distinct voice in the film, mapped to a voicebox profile once cloned."""

    label: str                       # e.g. "SPEAKER_00"
    reference_clip: Path | None = None   # clean sample pulled from the original
    voicebox_profile_id: str | None = None


@dataclass
class DubJob:
    """Everything one dubbing run needs and accumulates as it progresses."""

    input_file: Path
    source_lang: str                 # e.g. "ko"
    target_lang: str                 # e.g. "es"
    target_locale: str = ""          # canonical regional target, e.g. "es-MX" ("" = base)
    subtitle_file: Path | None = None
    kind: str = "full"               # full | tease (first-minutes audition clip)

    # Populated as stages run:
    source_audio: Path | None = None       # extracted original audio
    vocals: Path | None = None             # separated dialogue stem
    background: Path | None = None         # separated music + FX (M&E)
    segments: list[Segment] = field(default_factory=list)
    speakers: dict[str, Speaker] = field(default_factory=dict)
    dubbed_track: Path | None = None       # mixed dialogue + background
    output_file: Path | None = None        # final remuxed video
    script_is_target: bool = False         # segments already in the target language
    script_lang: str | None = None         # subtitle language can differ from audio
    transcription_options: dict = field(default_factory=dict)
    metrics: dict = field(default_factory=dict)
    report_file: Path | None = None
    review_file: Path | None = None
    artifacts_dir: Path | None = None
    translation_options: dict = field(default_factory=dict)
    version_id: str | None = None
    translation_id: str | None = None
    version_name: str = ""
    version_file: Path | None = None
    knowledge_snapshot: dict | None = None   # frozen entry/revision pins for this run
    show_ref: str = ""                     # stable series id for show-scope rules

    # Plan 01 identity. `source_reference` records which media/stream the source
    # evidence came from; `cue_lineage` maps a retired cue id to the cues that
    # replaced it, so an old edit cannot silently land on a different line.
    source_reference: SourceReference | None = None
    cue_lineage: dict[str, list[str]] = field(default_factory=dict)
    script_ref: str = ""

    # Non-spoken cues dropped before TTS, kept as evidence for reaction coverage
    # (Plan 08). Excluding a cue from synthesis is not the same as not knowing
    # that something was heard there.
    nonverbal: list[dict] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"{self.input_file.name}: {self.source_lang} -> {self.target_lang} | "
            f"{len(self.segments)} segments, {len(self.speakers)} speakers"
        )
