"""Versioned, settings-only dub recipes. Never load media or remote references."""

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator

from .config_schema import ConfigModel

# Deliberately excludes paths, endpoints, credentials, dialogue, local voice IDs,
# cast groups (cross-title local state), and machine/queue preferences.
SETTING_KEYS = (
    "dub.preset",
    "dub.duration_match",
    "dub.max_fit_attempts",
    "dub.ducking_ratio",
    "dub.background_volume",
    "dub.fallback_volume",
    "dub.duck_threshold",
    "dub.duck_attack_ms",
    "dub.duck_release_ms",
    "dub.pronunciations",
    "transcribe.source",
    "transcribe.diarize",
    "transcribe.clean_cues",
    "transcribe.align_subtitles",
    "translate.glossary",
    "translate.chars_per_second",
    "voicebox.seed",
    "quality.enabled",
    "quality.normalize",
    "quality.dialogue_lufs",
    "quality.asr",
    "quality.max_retries",
)
ENGINE = Literal[
    "chatterbox", "chatterbox_turbo", "qwen", "qwen_custom_voice", "kokoro", "luxtts", "tada"
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class RecipeMedia(StrictModel):
    kind: Literal["movie", "episode"]
    title: str = Field(min_length=1, max_length=300)
    tmdb_id: int | None = Field(default=None, gt=0)
    tvdb_id: int | None = Field(default=None, gt=0)
    season: int | None = Field(default=None, ge=0)
    episode: int | None = Field(default=None, gt=0)
    runtime_seconds: float | None = Field(default=None, gt=0, le=86400)

    @model_validator(mode="after")
    def episode_identity(self):
        if self.kind == "episode" and (
            not self.tvdb_id or self.season is None or self.episode is None
        ):
            raise ValueError("Episodes need TVDB ID, season, and episode number")
        return self


class RecipeVoice(StrictModel):
    name: str = Field(default="", max_length=200)
    engine: ENGINE = "chatterbox"
    delivery: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def direction_engine(self):
        if self.delivery and self.engine not in ("qwen", "qwen_custom_voice"):
            raise ValueError("Delivery directions require a Qwen engine")
        return self


class RecipeCharacter(StrictModel):
    speaker_id: str = Field(min_length=1, max_length=100)
    label: str = Field(min_length=1, max_length=100)
    category: Literal[
        "speaker",
        "narrator",
        "child_f",
        "child_m",
        "young_f",
        "young_m",
        "adult_f",
        "adult_m",
        "elderly_f",
        "elderly_m",
    ]
    voice: RecipeVoice


class DubRecipe(StrictModel):
    format: Literal["doblarr-recipe"]
    schema_version: Literal[1]
    mode: Literal["recipe-only"]
    creator: str = Field(default="", max_length=100)
    revision: str = Field(default="1", max_length=40)
    notes: str = Field(default="", max_length=2000)
    media: RecipeMedia
    source_language: str = Field(default="", pattern=r"^[a-z]{0,3}$")
    target_language: str = Field(pattern=r"^[a-z]{2,3}$")
    settings: dict = Field(default_factory=dict, max_length=len(SETTING_KEYS))
    narrator: RecipeVoice
    characters: list[RecipeCharacter] = Field(default_factory=list, max_length=100)

    @field_validator("settings")
    @classmethod
    def safe_settings(cls, settings):
        if len(json.dumps(settings)) > 32000:
            raise ValueError("Recipe settings exceed 32 KB")
        for key, value in settings.items():
            if key not in SETTING_KEYS:
                raise ValueError(f"Setting is not portable: {key}")
            section, field = key.split(".")
            model = ConfigModel.model_fields[section].annotation
            TypeAdapter(
                model.model_fields[field].annotation, config=ConfigDict(allow_inf_nan=False)
            ).validate_python(value, strict=True)
        choices = {
            "dub.preset": {"custom"},
            "transcribe.source": {"subtitles", "whisper"},
            "quality.asr": {"off", "suspicious", "all"},
        }
        ranges = {
            "dub.max_fit_attempts": (0, 10),
            "quality.max_retries": (0, 10),
            "dub.background_volume": (0, 4),
            "dub.fallback_volume": (0, 4),
            "dub.duck_threshold": (0, 1),
            "dub.duck_attack_ms": (0, 10000),
            "dub.duck_release_ms": (0, 10000),
            "translate.chars_per_second": (1, 100),
            "quality.dialogue_lufs": (-70, 0),
        }
        for key, value in settings.items():
            if key in choices and value not in choices[key]:
                raise ValueError(f"Unsupported recipe setting: {key}")
            if key in ranges and not ranges[key][0] <= value <= ranges[key][1]:
                raise ValueError(f"Recipe setting outside supported range: {key}")
        return settings

    @model_validator(mode="after")
    def unique_speakers(self):
        ids = [c.speaker_id for c in self.characters]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate speaker IDs")
        return self


def media_matches(source: RecipeMedia, destination: RecipeMedia) -> bool:
    if source.kind != destination.kind:
        return False
    if source.kind == "episode":
        return (source.tvdb_id, source.season, source.episode) == (
            destination.tvdb_id,
            destination.season,
            destination.episode,
        )
    if source.tmdb_id and destination.tmdb_id:
        return source.tmdb_id == destination.tmdb_id
    return source.title.casefold().strip() == destination.title.casefold().strip()
