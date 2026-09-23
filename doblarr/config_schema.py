"""Pydantic schema for config.yaml — validates known fields at load time.

Validation is advisory: unknown keys are allowed (older/newer configs keep
working) and wrong types log a warning instead of aborting the load. The
runtime still accesses config as plain dicts via `doblarr.config.Config`.
"""

from __future__ import annotations

import logging
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from .languages import parse as parse_language_tag

log = logging.getLogger("doblarr.config")


class _Section(BaseModel):
    model_config = ConfigDict(extra="allow")


class PathsModel(_Section):
    work_dir: str = "./work"
    output_dir: str = "./output"
    db: str | None = None


class GeneralModel(_Section):
    target_languages: list[str] = ["en", "es"]
    log_file: str | None = None


class WebModel(_Section):
    host: str = "127.0.0.1"
    port: int = 6363
    api_key: str = ""


class ConnectModel(_Section):
    radarr_url: str | None = None
    radarr_api_key: str | None = None
    sonarr_url: str | None = None
    sonarr_api_key: str | None = None
    plex_url: str | None = None
    plex_token: str | None = None


class DiscoveryModel(_Section):
    only_original_foreign: bool = True
    treat_undefined_as: str = "original"
    rescan_interval: str = "6h"
    auto_scan: bool = False
    cache_ttl: int = 300
    webhook_debounce: int = 30


class FilteringModel(_Section):
    tag_missing_dub: str = "needs-dub"
    hidden_collection_name: str = "Not in your language"
    kometa_handoff: bool = True
    kometa_file: str | None = None
    auto_label: bool = False


class PlexModel(_Section):
    auto_refresh: bool = True


class VoiceboxModel(_Section):
    base_url: str = "http://127.0.0.1:17493"
    timeout_seconds: int = 1800  # first CPU generation includes the model load
    default_engine: str = "chatterbox"
    model_size: str | None = None
    concurrency: int = 1
    seed: int | None = None
    preview_engine: str = "kokoro"


class TranslateModel(_Section):
    provider: str = "claude"
    model: str = "claude-sonnet-5"
    endpoint: str | None = None  # prompture driver URL override (local LLMs)
    batch_size: int = 12
    chars_per_second: float = 14
    glossary: dict[str, str] = {}
    locale: Literal["auto", "es-419", "es-MX", "es-ES"] = "auto"
    adaptation: Literal["natural", "faithful", "localized"] = "natural"
    adapt_region: bool = False
    reuse_memory: bool = False
    direction: str = ""
    character_notes: dict[str, str] = {}


class TranscribeModel(_Section):
    source: str = "subtitles"
    whisper_model: str = "large-v3"
    diarize: bool = True
    clean_cues: bool = True
    # A cue that is only a reaction written as a word ("Tsk!", "Heh heh") becomes
    # a reaction event instead of a line for the engine to act.
    interjections_as_reactions: bool = True
    align_subtitles: bool = False
    batch_size: int = 8
    device: str = "auto"
    compute_type: str = "auto"
    keep_models_loaded: bool = False


class SeparateModel(_Section):
    model: str = "htdemucs_ft"


class DubModel(_Section):
    version_name: str = ""
    preserve_versions: bool = True
    narrator_voice: str = ""
    narrator_delivery: str = ""
    preset: str = "custom"
    target_locale: str = ""  # canonical regional target (es-MX); "" derives from the language
    voice_mode: str = "clone"
    dry_run: bool = True
    duration_match: bool = True
    max_fit_attempts: int = 2
    ducking_ratio: str = "4:1"
    background_volume: float = 1.0
    fallback_volume: float = 0.2
    duck_threshold: float = 0.05
    duck_attack_ms: float = 30
    duck_release_ms: float = 350
    output_codec: str = "aac"
    output_bitrate: str = "192k"
    pronunciations: dict[str, str] = {}
    line_edits: dict[str, dict] = {}
    cast_group: str = ""
    character_map: dict[str, str] = {}
    audition_lines: int = 8
    # Acting direction and alternate takes (Plan 03). `candidates` maps a cue
    # id to how many extra takes to generate for it; a reviewer sets it and it
    # is spent once, under the shared request budget.
    locale_direction: str = ""       # "" derives accent guidance from the target locale
    candidates: dict[str, int] = {}
    candidate_limit: int = 4
    # A restrained cleanup of the clone reference sample. Off by default: it
    # changes the voice the clone learns, and the original sample is always
    # kept so the choice is reversible.
    clone_cleanup: bool = False
    track_name_template: str = "{language_name} AI"
    preset_voices: list[str] = []
    teaser_minutes: int = 10
    segment_limit: int | None = None


class KnowledgeModel(_Section):
    pack_releases: dict[str, str] = {}
    pack_distribution_url: str = ""   # official pack distribution endpoint; "" = unset
    auto_install_starter: bool = True  # install the bundled starter pack on first run


class QualityModel(_Section):
    enabled: bool = True
    normalize: bool = True
    dialogue_lufs: float = -18
    asr: str = "off"
    # Fraction of otherwise-unsuspicious lines to verify anyway under the
    # `suspicious` policy. Deterministic per cue, so a rerun checks the same
    # lines; 0 keeps the historical behavior of checking none of them.
    asr_sample: float = 0.0
    max_retries: int = 1
    # Extra provider requests one job may spend across quality retries, timing
    # repairs and later candidate takes. 0 = counted but never capped, which is
    # exactly the behavior before the budget existed.
    request_budget: int = 0


class BoundariesModel(_Section):
    """Speech-boundary preparation and protected clip edges (Plan 02).

    Defaults are off. Both operations change audio, and the roadmap only allows
    a new DSP default once audio and regression evidence back the rollout.
    """

    trim: bool = False              # remove generator padding before fitting
    handle_ms: float = 60           # protective margin kept each side of speech
    max_trim_seconds: float = 2.0   # never remove more than this per side
    min_trim_ms: float = 30         # below this there is nothing worth doing
    threshold_db: float = 12        # dB above the noise floor that counts as speech
    min_separation_db: float = 10   # below this the boundary is not knowable
    edge_fade_ms: float = 0         # 0 = no edge fade; 8 is a good starting value
    edge_threshold_db: float = -40  # an edge quieter than this is already smooth


class LevelsModel(_Section):
    """Source-relative dynamics and the one post-fit level owner (Plan 03).

    `mode` defaults to `legacy`, which keeps the pre-fit `quality.normalize`
    loudness pass exactly as it was. The other modes move loudness ownership
    after timing; only one of the two ever runs.
    """

    mode: Literal["legacy", "off", "consistent", "follow_source", "manual"] = "legacy"
    target_db: float = -20.0        # baseline speech-active RMS target, dBFS
    strength: float = 0.7           # how much of the source contrast to follow, 0..1
    max_boost_db: float = 4.0       # never push a loud line further than this
    max_cut_db: float = 8.0         # never bury a quiet line further than this
    min_seconds: float = 0.30       # shorter source evidence is not a measurement
    min_separation_db: float = 8.0  # speech this close to its bed is not measurable
    peak_ceiling: float = 0.89      # hard peak the level pass will not cross
    measure_source: bool = False    # measure the original even outside follow_source
    gains: dict[str, float] = {}    # per-cue manual gain in dB, keyed by cue id


class TimingModel(_Section):
    """Phrase timing and conversation checks (Plan 04).

    `mode` defaults to `whole`, which is whole-clip fitting exactly as earlier
    releases ran it. `phrase` hands timing to the phrase owner; only one of the
    two ever runs, because two stretches of one line compound.
    """

    mode: Literal["whole", "phrase"] = "whole"
    max_stretch: float = 1.3
    min_stretch: float = 1.0        # 1.0 = never slow speech down
    handle_ms: float = 40           # margin kept each side of a phrase
    min_pause: float = 0.12         # floor for a redistributable gap
    protect_pause: float = 0.45     # a gap at least this long is performance
    tail_handle_ms: float = 60
    anchor_tolerance: float = 0.12
    min_phrase_seconds: float = 0.15
    threshold_db: float = 12
    min_separation_db: float = 10
    collision_gap: float = 0.0
    repair: bool = True
    phrases: dict[str, dict] = {}   # per-cue anchors/pauses set in review
    overlaps: dict[str, dict] = {}  # per-cue accepted intentional overlaps


class CoverageModel(_Section):
    """Reaction and background coverage (Plan 04).

    `off` keeps the event ledger and changes no audio. Nothing is ever inserted
    without an explicit decision, and an unsupported engine capability is
    recorded as asked-for rather than applied.
    """

    mode: Literal["off", "review", "retain"] = "off"
    gain_db: float = 0.0
    handle_ms: float = 80
    fade_ms: float = 25
    max_seconds: float = 4.0
    leakage_check: bool = False
    generate: bool = False
    events: dict[str, dict] = {}    # per-event decisions set in review
    assets: dict[str, str] = {}     # machine-local replacement sounds
    extra: list[dict] = []          # events a person added by hand


class TreatmentsModel(_Section):
    """Scene space and device voice treatments (Plan 05).

    `mode` defaults to `off`, which renders dry dialogue exactly as earlier
    releases did. A treatment is a place or a device, never a performance: it
    never changes the acting direction and its effect on the level is measured
    and corrected rather than left to accumulate.
    """

    mode: Literal["off", "on"] = "off"
    default: Literal["dry", "room", "distant", "phone", "radio"] = "dry"
    intensity: float = 1.0          # 0..1, interpolates the preset's parameters
    max_tail: float = 1.5           # hard bound on effect ring-out, seconds
    scenes: list[dict] = []         # [{start, end, preset, intensity, note}]
    lines: dict[str, dict] = {}     # per-cue {preset, intensity, bypass}


class DeliveryModel(_Section):
    """Checks run on the actual exported track (Plan 05).

    `measure` reports everything and blocks nothing. `enforce` lets a
    structural failure withhold the saved version and the library refresh.
    A loudness or true-peak target left unset yields a measurement, never a
    fabricated pass against an unspecified standard.
    """

    mode: Literal["off", "measure", "enforce"] = "measure"
    profile: str = "local"
    sample_rate: int = 0            # 0 accepts whatever the encoder produced
    channels: int = 0               # 0 accepts any layout
    duration_tolerance: float = 1.0
    start_tolerance: float = 0.10   # encoder-delay allowance on stream start
    target_lufs: float | None = None
    lufs_tolerance: float = 2.0
    true_peak_db: float | None = None
    placement_samples: int = 3      # head/middle/tail cue windows to decode
    silence_db: float = -50.0
    check_original_streams: bool = True


class ConfigModel(_Section):
    paths: PathsModel = PathsModel()
    general: GeneralModel = GeneralModel()
    web: WebModel = WebModel()
    connect: ConnectModel = ConnectModel()
    discovery: DiscoveryModel = DiscoveryModel()
    filtering: FilteringModel = FilteringModel()
    plex: PlexModel = PlexModel()
    voicebox: VoiceboxModel = VoiceboxModel()
    translate: TranslateModel = TranslateModel()
    transcribe: TranscribeModel = TranscribeModel()
    separate: SeparateModel = SeparateModel()
    dub: DubModel = DubModel()
    knowledge: KnowledgeModel = KnowledgeModel()
    quality: QualityModel = QualityModel()
    boundaries: BoundariesModel = BoundariesModel()
    levels: LevelsModel = LevelsModel()
    timing: TimingModel = TimingModel()
    coverage: CoverageModel = CoverageModel()
    treatments: TreatmentsModel = TreatmentsModel()
    delivery: DeliveryModel = DeliveryModel()


def validate_config(data: dict) -> None:
    """Log a clear warning for every schema violation in the merged config."""
    try:
        ConfigModel.model_validate(data)
    except ValidationError as exc:
        for err in exc.errors():
            loc = ".".join(str(p) for p in err["loc"])
            log.warning("config: invalid value at %s: %s", loc, err["msg"])
    locale = str((data.get("dub") or {}).get("target_locale") or "")
    if locale and not parse_language_tag(locale):
        log.warning("config: dub.target_locale %r is not a valid language tag", locale)
