"""Versioned, settings-only dub recipes. Never load media or remote references."""

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator

from .config_schema import ConfigModel
from .languages import base_language
from .languages import parse as parse_language_tag

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
    "translate.locale",
    "translate.adaptation",
    "translate.adapt_region",
    "translate.direction",
    "translate.character_notes",
    "translate.chars_per_second",
    "voicebox.seed",
    "quality.enabled",
    "quality.normalize",
    "quality.dialogue_lufs",
    "quality.asr",
    "quality.max_retries",
    "quality.request_budget",
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


KNOWLEDGE_STATUS = Literal["proposed", "reviewed", "needs-retest", "retired"]


class RecipeRealization(StrictModel):
    """A pronunciation realization pinned by id/revision (schema v2 overlay)."""

    id: str = Field(min_length=1, max_length=100)
    revision: int = Field(ge=1)
    engine: str = Field(min_length=1, max_length=100)
    model: str | None = Field(default=None, max_length=200)
    voice: str | None = Field(default=None, max_length=300)
    replacement: str = Field(min_length=1, max_length=300)
    evidence: str = Field(default="", max_length=2000)
    status: KNOWLEDGE_STATUS = "proposed"


class RecipeEntry(StrictModel):
    """One show/episode knowledge rule in a schema v2 overlay.

    scope_ref is deliberately absent: episode/movie rules bind to the importing
    install's title key and show rules to the recipe's stable series id.
    """

    id: str = Field(min_length=1, max_length=100)
    revision: int = Field(ge=1)
    kind: Literal["pronunciation", "term"]
    locale: str = Field(min_length=2, max_length=20)
    coverage: list[str] = Field(default_factory=list, max_length=20)
    source_lang: str | None = None
    source_form: str = Field(default="", max_length=300)
    phrase: str = Field(min_length=1, max_length=300)
    sense: str = Field(default="", max_length=300)
    usage: str = Field(default="", max_length=2000)
    examples: list[str] = Field(default_factory=list, max_length=20)
    pronunciation: str = Field(default="", max_length=2000)
    ipa: str | None = None
    scope: Literal["episode", "movie", "show"]
    status: KNOWLEDGE_STATUS = "proposed"
    license: str = Field(default="", max_length=300)
    contributor: str = Field(default="", max_length=300)
    review_history: list[dict] = Field(default_factory=list, max_length=100)
    realizations: list[RecipeRealization] = Field(default_factory=list, max_length=50)

    @field_validator("locale")
    @classmethod
    def _canonical_locale(cls, value: str) -> str:
        parsed = parse_language_tag(value)
        if parsed is None:
            raise ValueError("locale must be a language tag such as es, es-MX or es-419")
        return parsed


class RecipeKnowledge(StrictModel):
    entries: list[RecipeEntry] = Field(default_factory=list, max_length=200)
    pack_dependencies: dict[str, str | int] = Field(default_factory=dict, max_length=50)


class RecipeShow(StrictModel):
    """Show-level knowledge scope: a stable series id, never episode-local speaker ids."""

    series_id: str = Field(min_length=1, max_length=300)
    title: str = Field(default="", max_length=300)


class DubRecipe(StrictModel):
    format: Literal["doblarr-recipe"]
    schema_version: Literal[1, 2]
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
    # Schema v2 only (rejected on v1 documents, so a v1 reader never silently
    # drops them): the regional target, the show overlay scope, and the
    # portable knowledge overlay itself.
    target_locale: str = Field(default="", max_length=20)
    show: RecipeShow | None = None
    knowledge: RecipeKnowledge | None = None

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
            "quality.request_budget": (0, 10000),
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
    def versioned_fields(self):
        if self.schema_version == 1:
            if self.target_locale or self.show is not None or self.knowledge is not None:
                raise ValueError("schema v1 recipes cannot carry target_locale, show or knowledge")
        elif self.target_locale:
            parsed = parse_language_tag(self.target_locale)
            if parsed is None:
                raise ValueError("target_locale must be a language tag")
            if base_language(parsed) != self.target_language:
                raise ValueError("target_locale must share the target_language base")
        return self

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
