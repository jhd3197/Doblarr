"""Which track or text plays which role, and what we actually know about it.

A release can carry the original, several dubs and several subtitle tracks. A
track's language tag or title says what somebody *labelled* it; it is not
evidence of what it contains. So a reference assignment records identity
(media, stream, hashes, time base), provenance (how its text was obtained) and
an explicit set of roles, and nothing is fed to a model by default.

Roles:

- ``meaning``     the original dialogue: facts, relationships, intent.
- ``adaptation``  another localisation's writing (e.g. the English dub transcript),
                  which may contribute phrasing or wordplay under an explicit policy.
- ``performance`` a track chosen for delivery context.
- ``voice``       a sample or profile chosen for voice identity.
- ``evaluation``  held out from every generation path and revealed separately.

An evaluation-only reference cannot hold any other role. A dub transcript is
not a subtitle: subtitles usually translate the original, a dub transcript is
what a different cast actually said, and the two are kept apart by `text.kind`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..artifacts import digest
from ..languages import parse as parse_language

ROLES = ("meaning", "adaptation", "performance", "voice", "evaluation")
GENERATION_ROLES = ("meaning", "adaptation", "performance", "voice")
TEXT_KINDS = ("original_transcript", "dub_transcript", "subtitle_translation",
              "subtitle_original", "manual", "none")


class TrackIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    media_path: str = Field(default="", max_length=2000)
    media_key: str = Field(default="", max_length=64)
    stream_index: int | None = Field(default=None, ge=0, le=512)   # container stream
    audio_index: int | None = Field(default=None, ge=0, le=64)     # nth audio stream
    stream_title: str = Field(default="", max_length=300)
    tag_language: str = Field(default="", max_length=16)
    duration: float | None = Field(default=None, ge=0)


class TextIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["original_transcript", "dub_transcript", "subtitle_translation",
                  "subtitle_original", "manual", "none"] = "none"
    provenance: str = Field(default="", max_length=300)   # asr:<engine> | subtitle:<stream> | file
    artifact: str = Field(default="", max_length=2000)   # utterance file on disk
    sha256: str = Field(default="", max_length=64)
    utterances: int = Field(default=0, ge=0)
    uncertain: bool = False   # transcription quality unknown / unreviewed


class SampleWindow(BaseModel):
    """Where on its track a voice sample sits (seconds, the track's own timeline)."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    start: float = Field(ge=0)
    end: float = Field(gt=0)
    speaker: str = Field(default="", max_length=80)
    text: str = Field(default="", max_length=600)


class ReferenceIn(BaseModel):
    """A reference assignment as a person (or the importer) declares it."""

    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1, max_length=120)
    language: str = Field(min_length=2, max_length=16)
    edition: str = Field(default="", max_length=200)
    roles: list[str] = Field(default_factory=list, max_length=5)
    track: TrackIdentity | None = None
    text: TextIdentity = Field(default_factory=TextIdentity)
    sample: SampleWindow | None = None
    notes: str = Field(default="", max_length=2000)

    @field_validator("language")
    @classmethod
    def known_language(cls, value):
        parsed = parse_language(value)
        if not parsed or parsed == "und":
            raise ValueError("a reference needs a known language")
        return parsed

    @field_validator("roles")
    @classmethod
    def known_roles(cls, value):
        cleaned = sorted(dict.fromkeys(str(r) for r in value))
        unknown = [r for r in cleaned if r not in ROLES]
        if unknown:
            raise ValueError(f"unknown role(s) {', '.join(unknown)}")
        return cleaned

    @model_validator(mode="after")
    def coherent(self):
        roles = set(self.roles)
        if "evaluation" in roles and roles - {"evaluation"}:
            raise ValueError("an evaluation-only reference cannot also be a writing, "
                             "performance or voice reference")
        if roles & {"meaning", "adaptation"} and self.text.kind == "none":
            raise ValueError("a meaning or adaptation reference needs text to read")
        if roles & {"performance", "voice"} and self.track is None:
            raise ValueError("a performance or voice reference needs an audio track")
        return self


def reference_id(session: str, data: ReferenceIn) -> str:
    """Stable identity of one reference in one studio session."""
    track = data.track.model_dump() if data.track else None
    return "ref-" + digest([session, data.label, data.language,
                            track and [track["media_key"], track["stream_index"]]])[:12]


def is_generation_source(reference: dict) -> bool:
    roles = set(reference.get("roles") or [])
    return bool(roles & set(GENERATION_ROLES)) and "evaluation" not in roles


def is_evaluation(reference: dict) -> bool:
    return "evaluation" in set(reference.get("roles") or [])


class Utterance(BaseModel):
    """One timed line on a reference's own timeline (seconds)."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    utt_id: str = Field(min_length=1, max_length=64)
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    text: str = Field(default="", max_length=4000)
    speaker: str = Field(default="", max_length=80)

    @model_validator(mode="after")
    def ordered(self):
        if self.end <= self.start:
            raise ValueError("an utterance must end after it starts")
        return self


MAX_UTTERANCES = 20000


def write_utterances(path: Path, rows: list[dict]) -> dict:
    """Validate and store a reference's utterances; returns their text identity."""
    if len(rows) > MAX_UTTERANCES:
        raise ValueError("too many utterances for one reference")
    checked = [Utterance.model_validate(r).model_dump() for r in rows]
    ids = [r["utt_id"] for r in checked]
    if len(set(ids)) != len(ids):
        raise ValueError("utterance ids must be unique")
    checked.sort(key=lambda r: (r["start"], r["utt_id"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps({"version": 1, "utterances": checked}, ensure_ascii=False, indent=1)
    temp = path.with_suffix(".partial")
    temp.write_text(body, encoding="utf-8")
    temp.replace(path)
    return {"artifact": str(path), "sha256": digest(checked), "utterances": len(checked)}


def read_utterances(reference: dict) -> list[dict]:
    """The utterances a reference's text identity points at, verified by hash."""
    text = reference.get("text") or {}
    artifact = text.get("artifact")
    if not artifact:
        return []
    try:
        payload = json.loads(Path(artifact).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"reference text for {reference.get('label')} is unreadable") from exc
    rows = payload.get("utterances") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise ValueError("reference text has no utterances")
    if text.get("sha256") and digest(rows) != text["sha256"]:
        raise ValueError(f"reference text for {reference.get('label')} changed on disk since "
                         "it was assigned; reassign it so every use reads the same words")
    return rows


def from_subtitles(path: Path, prefix: str = "sub") -> list[dict]:
    """Utterances from a subtitle file (SRT/ASS/VTT), on the file's own timeline."""
    import pysubs2

    subs = pysubs2.load(str(path))
    rows = []
    for n, line in enumerate(subs):
        text = line.plaintext.replace("\n", " ").strip()
        if not text or line.end <= line.start:
            continue
        rows.append({"utt_id": f"{prefix}-{n:05d}", "start": line.start / 1000.0,
                     "end": line.end / 1000.0, "text": text, "speaker": line.name or ""})
    return rows
