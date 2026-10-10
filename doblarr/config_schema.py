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
    # Plex fills in what Sonarr and Radarr do not list (see doblarr.plex_library).
    plex: bool = True
    plex_unmatched: bool = False    # also titles Plex could not match to TMDB/TVDB


class FilteringModel(_Section):
    tag_missing_dub: str = "needs-dub"
    hidden_collection_name: str = "Not in your language"
    kometa_handoff: bool = True
    kometa_file: str | None = None
    auto_label: bool = False


class PlexModel(_Section):
    auto_refresh: bool = True


class SpeechModel(_Section):
    # Which speech service clones voices and generates lines. The engine,
    # model size, seed and concurrency in `voicebox` apply to either.
    backend: Literal["voicebox", "voicestudio"] = "voicebox"


class VoiceStudioModel(_Section):
    base_url: str = "http://127.0.0.1:3900"
    api_key: str | None = None   # only for a non-loopback VoiceStudio
    timeout_seconds: int = 1800
    # Engines heard following a free-text delivery direction. VoiceStudio
    # does not declare this, so nothing is directable until listed.
    directable_engines: list[str] = []


class VoiceboxModel(_Section):
    base_url: str = "http://127.0.0.1:17493"
    timeout_seconds: int = 1800  # first CPU generation includes the model load
    default_engine: str = "chatterbox"
    model_size: str | None = None
    concurrency: int = 1
    seed: int | None = None
    preview_engine: str = "kokoro"
    # Chatterbox sampling (needs the jhd3197/voicebox fork). None keeps the
    # engine default (0.5 / 0.5 / 0.8); a cast entry can override each one.
    # cfg_weight near 0 stops a Japanese reference's accent leaking into Spanish.
    exaggeration: float | None = None  # 0.25-2.0
    cfg_weight: float | None = None  # 0-1
    temperature: float | None = None  # 0.05-5.0


class TranslateModel(_Section):
    provider: str = "claude"
    model: str = "claude-sonnet-5"
    endpoint: str | None = None  # prompture driver URL override (local LLMs)
    batch_size: int = 12
    chars_per_second: float = 14
    glossary: dict[str, str] = {}
    locale: Literal["auto", "es-419", "es-MX", "es-VE", "es-AR", "es-CO", "es-CL", "es-ES"] = "auto"
    adaptation: Literal["natural", "faithful", "localized"] = "natural"
    adapt_region: bool = False
    # Regional slang in the dub. Off keeps wording understandable across the
    # region; on lets characters talk the way people there do.
    slang: bool = False
    reuse_memory: bool = False
    direction: str = ""
    character_notes: dict[str, str] = {}
    # One bounded read of the whole script before translating: a synopsis for
    # the translator, and with summary_terms, name/term candidates for review.
    # Costs provider calls, so off by default (see doblarr.prepass).
    prepass: Literal["off", "summary", "summary_terms"] = "off"
    # How another localisation may inform the writing (see doblarr.studio).
    # original_only never sends a reference; reference_suggestions lets the
    # original decide facts and borrows phrasing only where it keeps them;
    # follow_edition targets a chosen adaptation and records departures.
    reference_policy: Literal["original_only", "reference_suggestions",
                              "follow_edition"] = "original_only"
    reference_file: str = ""       # studio-built aligned reference for this episode
    # When the episode file carries a published dub in the target language,
    # transcribe it and translate along it (doblarr.dub_reference): follow its
    # names and phrasing, only borrow phrasing (suggest), or ignore it (off).
    published_dub: Literal["follow", "suggest", "off"] = "follow"
    holdout_files: list[str] = []  # evaluation-only text no request may carry


class TranscribeModel(_Section):
    source: str = "subtitles"
    whisper_model: str = "large-v3"
    diarize: bool = True
    # auto: pyannote when HF_TOKEN is set, else local voice grouping (no account)
    diarizer: Literal["auto", "pyannote", "local"] = "auto"
    clean_cues: bool = True
    # A cue that is only a reaction written as a word ("Tsk!", "Heh heh") becomes
    # a reaction event instead of a line for the engine to act.
    interjections_as_reactions: bool = True
    align_subtitles: bool = False
    batch_size: int = 8
    device: str = "auto"
    compute_type: str = "auto"
    keep_models_loaded: bool = False


class SpeakersModel(_Section):
    """Which voice models tell the speakers apart (see doblarr.voice_models).

    One model or several joined; `custom` registers any sherpa-onnx speaker
    embedding ONNX as {id, name, url or path, threshold}.
    """

    models: list[str] = ["wespeaker-resnet34", "3dspeaker-eres2netv2"]
    threshold: float | None = None     # grouping distance; blank uses the models' own
    models_dir: str = ""               # blank: <work_dir>/models/speakers
    custom: list[dict] = []
    # Also hear the video's other audio tracks (the dubs): "all", a list of
    # languages or stream numbers, or [] for the original dialogue alone.
    tracks: str | list[str] = "all"
    # A dub kept as an evaluation reference (to compare a Doblarr dub with)
    # may still help tell the voices apart: hearing who speaks is not
    # learning how to perform. false keeps such tracks out of grouping too.
    evaluation_tracks: bool = True


class ComputeModel(_Section):
    """Where the local model stages run (Demucs, pyannote, whisper).

    `auto` picks the first CUDA device, else Apple MPS, else the CPU, and never
    fails. An explicit device that is not there fails the job before the stage
    starts instead of silently running an hour on the CPU. `inherit` on a
    stage means "use `device`"; the older `transcribe.device` still applies to
    transcription while its own override is `inherit`.
    """

    device: str = "auto"               # auto | cpu | cuda | cuda:N | mps
    separate_device: str = "inherit"   # inherit | auto | cpu | cuda | cuda:N | mps
    diarize_device: str = "inherit"
    transcribe_device: str = "inherit"
    release_after_stage: bool = True   # free VRAM after each local model stage
    log_memory: bool = True            # log allocated/reserved VRAM around them


class SeparateModel(_Section):
    model: str = "htdemucs_ft"
    # A source longer than this is separated in overlapping windows, so a
    # feature film needs the memory of one window. 0 separates in one piece.
    chunk_seconds: float = 600
    overlap_seconds: float = 10


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
    # Keep the original singing in the opening, ending and insert songs (found
    # from the subtitle track's lyric styles); separation removes it otherwise.
    keep_songs: bool = True
    # A voice with no clean sample to clone (a few short lines) borrows the
    # clone of the voice it sounds most like instead of failing the dub.
    borrow_voice: bool = True
    background_volume: float = 1.0
    fallback_volume: float = 0.2
    duck_threshold: float = 0.05
    duck_attack_ms: float = 100     # the bed eases down under a new line instead of snapping
    duck_release_ms: float = 350
    output_codec: str = "aac"
    output_bitrate: str = "192k"
    pronunciations: dict[str, str] = {}
    # Japanese source, Spanish target: respell the glossary's names for the
    # engine the way Latin American dubs say them (Jiro -> Yiro).
    romaji_names: bool = False
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
    # Compare every take with its speaker's reference clip and retry one that
    # no longer sounds like that voice (doblarr.voice_check). With retries the
    # best attempt is kept, not the last.
    voice_check: bool = False
    voice_min_similarity: float = 0.25
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
    # Voices ease in and out where a clip would otherwise start or end
    # mid-sound; an edge that is already silent is never touched.
    edge_fade_in_ms: float = 12     # short: never blunt a consonant (max 50)
    edge_fade_out_ms: float = 40    # longer: let a vowel or breath die away (max 150)
    edge_fade_curve: Literal["hsin", "qsin", "tri"] = "hsin"  # hsin = S-shaped, tri = linear
    edge_fade_ms: float = 0         # older single linear fade; nonzero overrides the three above
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
    # Move each subtitle cue's start to the speech onset in the dialogue stem
    # (fansub timing often trails the voice by a few hundred milliseconds).
    snap_onsets: bool = False
    max_stretch: float = 1.3
    min_stretch: float = 1.0        # 1.0 = never slow speech down
    # How a line is time-compressed: ffmpeg's atempo, or the Rubber Band
    # phase vocoder, which stays smoother on voiced speech past ~1.15x.
    stretcher: Literal["atempo", "rubberband"] = "atempo"
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
    # Pace per character (doblarr.pacing). A group is one speaker's lines,
    # split where they fall silent for `pace_scene_gap`; a line heard more
    # than `pace_tolerance` off its group's median pace is flagged.
    # `speaker` keeps a group's lines near one pace in whole-clip mode;
    # `off` fits each line alone, exactly as earlier releases did.
    pacing: Literal["off", "speaker"] = "speaker"
    pace_tolerance: float = 0.18
    pace_local_range: float = 0.10  # how far a line's factor may sit from its group's
    pace_max_speedup: float = 1.15  # most a slow take is sped toward its group
    pace_scene_gap: float = 8.0
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
    # Voice in the separated dialogue stem that no line covers (an untagged
    # laugh, a grunt) becomes an event; auto_retain keeps each unreviewed
    # vocal event's original sound where its window is clean and short.
    detect: bool = False
    auto_retain: bool = False
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


class DecisionsModel(_Section):
    """Typed decisions about the script (doblarr.decisions).

    Rules read what the text says; the decision model is asked only where
    they are silent. A model answer at `apply_confidence` or above is
    applied, one at `suggest_confidence` or above is a review suggestion.
    Without the model (not installed, or failing) the rules run alone.
    """

    enabled: bool = True
    model: str = "laya/router"      # any Prompture decision model: laya/*, kev/*, typesafe/*
    apply_confidence: float = 0.9
    suggest_confidence: float = 0.6
    cutoffs: bool = True            # a line cut off or trailing away keeps that ending
    delivery: bool = True           # whisper / shout / thought from the line's own words
    title_cards: bool = True        # an on-screen caption is not spoken
    reactions: bool = True          # a cue that is only a reaction sound becomes an event
    sound_tags: bool = True         # name an unrecognized [sound] tag
    rewrite_check: bool = True      # reject a timing rewrite that loses names or meaning
    treatments: bool = True         # suggest phone / radio / distant from the text
    review_order: bool = True       # the lines most likely wrong come first in review


class AnalysisModel(_Section):
    """What an episode analysis measures beyond its lines (docs/media-knowledge.md).

    Nothing here translates, clones or generates speech. Visual analysis is
    optional and off by default: it needs extra libraries and model files, and
    audio analysis never waits for it or fails because of it.
    """

    features: bool = True             # energy curves, pauses and peaks per line
    visual: bool = False              # shots, faces, tracks and active speaker
    stages: list[str] = []            # rerun only these stages; [] runs everything enabled
    # Narrative extraction reads the script with a language model (provider
    # calls, charged to the request budget). Off unless asked for; the model
    # is a Prompture model string such as ollama/qwen3:8b.
    knowledge: bool = False
    knowledge_model: str = ""
    knowledge_endpoint: str | None = None
    # Let the extraction read a reference script found online (title research)
    # next to the lines, aligned by their words. Off until it has proven itself.
    use_reference_scripts: bool = False
    # How each line is said (doblarr.emotion): a still per line read by a
    # model that sees, plus the voice. Off unless asked; local by default.
    emotion: bool = False
    emotion_model: str = "ollama/qwen3-vl:8b"
    # Who speaks each line, read from the script by a strong model
    # (doblarr.dialogue_reader). Empty: off. A cloud model sends the subtitle
    # text out of this machine; local 8-12B models were not good enough.
    reader_model: str = ""
    # Transcribe the official dub in the target language, when the file has
    # one, so the page can offer its wording for the show's names and terms.
    # Local speech recognition, about two minutes of GPU per episode.
    dub_text: bool = True
    # A dub track only helps tell voices apart when it is the same cut: its
    # speech has to line up with the original before it is used as evidence.
    verify_tracks: bool = True
    min_track_correlation: float = 0.45
    max_track_offset: float = 2.0     # seconds; a larger offset is another edit


class VisionModel(_Section):
    """Shots, faces and visible tracks (doblarr.vision). Optional dependencies."""

    backend: Literal["auto", "opencv", "off"] = "auto"
    domain: Literal["auto", "anime", "live_action"] = "auto"
    frame_height: int = 360
    frames_per_shot: int = 3
    max_frames: int = 4000            # bound on frames sampled from one title
    asd_fps: float = 8.0              # frames per second read inside a line for mouth motion
    models_dir: str = ""              # blank: <work_dir>/models/vision
    match_threshold: float = 0.55     # face-to-reference similarity to propose a name


class AdaptiveModel(_Section):
    """Voice envelopes, background policies, retrieval and the judge (docs/adaptive-audio.md).

    `mode: off` renders exactly what earlier releases rendered. `suggest`
    computes recommendations for review without changing audio; `apply` renders
    the selected envelope through the one post-fit level owner.
    """

    mode: Literal["off", "suggest", "apply"] = "off"
    # off | retrieval | kev/<model> | laya/<model> | llm:<prompture model>
    judge: str = "retrieval"
    judge_endpoint: str | None = None
    judge_budget: int = 400           # model calls per run; then the retrieval fallback
    candidates: int = 4               # template candidates per line (preserve is extra)
    envelope_strength: float = 1.0    # global scale on every selected envelope
    max_envelope_db: float = 6.0      # no envelope moves a line further than this
    background_policy: str = ""       # a background template id; "" keeps sidechain ducking
    max_bed_attenuation_db: float = 18.0
    lines: dict[str, dict] = {}       # per-cue manual selection {template, strength, locked}


class ResearchModel(_Section):
    """Title research from public sources (docs/title-research.md).

    Off by default. A search sends only a title or a question out of this
    machine, and only when a person asks; whatever it finds lands for review
    and is never applied on its own.
    """

    enabled: bool = False
    model: str = ""                   # a Prompture model; blank uses analysis.knowledge_model
    depth: Literal["quick", "standard", "deep"] = "quick"
    max_cost_usd: float = 1.0         # hard cap on model spend per research run
    cache_days: int = 30              # how long a catalogue answer is reused
    opensubtitles_api_key: str = ""   # optional; without it OpenSubtitles is skipped
    tmdb_api_key: str = ""            # optional; without it TMDB is skipped
    kitsunekko_mirror: str = ""       # a local clone of a Japanese subtitle mirror


class ConfigModel(_Section):
    paths: PathsModel = PathsModel()
    general: GeneralModel = GeneralModel()
    web: WebModel = WebModel()
    connect: ConnectModel = ConnectModel()
    discovery: DiscoveryModel = DiscoveryModel()
    filtering: FilteringModel = FilteringModel()
    plex: PlexModel = PlexModel()
    speech: SpeechModel = SpeechModel()
    voicebox: VoiceboxModel = VoiceboxModel()
    voicestudio: VoiceStudioModel = VoiceStudioModel()
    translate: TranslateModel = TranslateModel()
    transcribe: TranscribeModel = TranscribeModel()
    speakers: SpeakersModel = SpeakersModel()
    separate: SeparateModel = SeparateModel()
    compute: ComputeModel = ComputeModel()
    dub: DubModel = DubModel()
    knowledge: KnowledgeModel = KnowledgeModel()
    quality: QualityModel = QualityModel()
    boundaries: BoundariesModel = BoundariesModel()
    levels: LevelsModel = LevelsModel()
    timing: TimingModel = TimingModel()
    coverage: CoverageModel = CoverageModel()
    treatments: TreatmentsModel = TreatmentsModel()
    delivery: DeliveryModel = DeliveryModel()
    decisions: DecisionsModel = DecisionsModel()
    analysis: AnalysisModel = AnalysisModel()
    vision: VisionModel = VisionModel()
    adaptive: AdaptiveModel = AdaptiveModel()
    research: ResearchModel = ResearchModel()


def config_problems(data: dict) -> list[str]:
    """Every schema violation in a merged config, one readable line each."""
    problems = []
    try:
        ConfigModel.model_validate(data)
    except ValidationError as exc:
        for err in exc.errors():
            loc = ".".join(str(p) for p in err["loc"])
            problems.append(f"invalid value at {loc}: {err['msg']}")
    locale = str((data.get("dub") or {}).get("target_locale") or "")
    if locale and not parse_language_tag(locale):
        problems.append(f"dub.target_locale {locale!r} is not a valid language tag")
    return problems


def validate_config(data: dict) -> None:
    """Log a clear warning for every schema violation in the merged config."""
    for problem in config_problems(data):
        log.warning("config: %s", problem)
