"""Core data structures passed between pipeline stages."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Segment:
    """One line of dialogue with timing, through every stage of the pipeline."""

    index: int
    start: float                     # seconds
    end: float                       # seconds
    text_src: str                    # original-language text
    speaker: str = "SPEAKER_00"      # diarization label
    text_translated: str | None = None
    audio_clip: Path | None = None   # generated dub clip for this line

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


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

    def summary(self) -> str:
        return (
            f"{self.input_file.name}: {self.source_lang} -> {self.target_lang} | "
            f"{len(self.segments)} segments, {len(self.speakers)} speakers"
        )
