"""Pydantic schema for config.yaml — validates known fields at load time.

Validation is advisory: unknown keys are allowed (older/newer configs keep
working) and wrong types log a warning instead of aborting the load. The
runtime still accesses config as plain dicts via `doblarr.config.Config`.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, ConfigDict, ValidationError

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


class TranscribeModel(_Section):
    source: str = "subtitles"
    whisper_model: str = "large-v3"
    diarize: bool = True
    clean_cues: bool = True
    align_subtitles: bool = False
    batch_size: int = 8
    device: str = "auto"
    compute_type: str = "auto"
    keep_models_loaded: bool = False


class SeparateModel(_Section):
    model: str = "htdemucs_ft"


class DubModel(_Section):
    preset: str = "custom"
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
    track_name_template: str = "{language_name} AI"
    preset_voices: list[str] = []
    teaser_minutes: int = 10
    segment_limit: int | None = None


class QualityModel(_Section):
    enabled: bool = True
    normalize: bool = True
    dialogue_lufs: float = -18
    asr: str = "off"
    max_retries: int = 1


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
    quality: QualityModel = QualityModel()


def validate_config(data: dict) -> None:
    """Log a clear warning for every schema violation in the merged config."""
    try:
        ConfigModel.model_validate(data)
    except ValidationError as exc:
        for err in exc.errors():
            loc = ".".join(str(p) for p in err["loc"])
            log.warning("config: invalid value at %s: %s", loc, err["msg"])
