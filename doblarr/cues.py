"""Typed cue records: identity, source evidence, placement, takes and artifacts.

`Segment` stays the compatibility-facing cue (`index`, `audio_clip`, `issues`
keep working). The records here are the authoritative owners of cue identity,
the untouched original source ranges, generated takes, processed derivatives and
structured findings. One versioned codec (`cue_payload` / `apply_cue_payload`)
serializes them into the script cache, review snapshots and version manifests.

Invariants enforced here rather than at each call site:

- Times are seconds on an explicitly named timeline (`Span.domain`). Source
  words, target placement and montage positions never share an unlabeled domain.
- Intervals are half-open ``[start, end)`` with finite, non-negative, increasing
  bounds.
- Cue IDs derive from the *original script identity* plus an immutable ordinal,
  never from editable words or a live list position. Split/merge mints new IDs
  with parent lineage and retires the old ones, so an edit addressed to a
  retired cue raises a conflict instead of landing on a different line.
- Raw generated audio and each processed derivative are separate artifacts.
  Migrated audio whose role cannot be proven is recorded as ``unknown``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .artifacts import digest
from .errors import DoblarrError

# Bump only for an incompatible change to the records below. A payload written
# by a newer Doblarr is rejected, never guessed at.
#
# 2 (Plan 03): the `leveled` artifact role, plus the performance intent, source
# measurement, level decision and content-verification records. A version-1
# payload is read unchanged — the new records simply stay empty, which is their
# honest state for a run that never produced them.
#
# 3 (Plan 04): the `phrased` artifact role and the phrase timing plan, plus
# typed nonverbal events. Older payloads load unchanged: their timing plan is
# empty (that run fitted whole clips) and their nonverbal rows are migrated
# from the plain dictionaries Plan 01 wrote.
CUE_SCHEMA_VERSION = 3

# Time domains. Never mix them in one number.
SOURCE = "source"    # the original media timeline
TARGET = "target"    # the dubbed timeline the mix places clips on
CLIP = "clip"        # offsets inside one generated clip
MONTAGE = "montage"  # an audition montage's own concatenated timeline
DOMAINS = (SOURCE, TARGET, CLIP, MONTAGE)

# Artifact roles in processing order. "unknown" is a real state, used when a
# migrated file's provenance cannot be proven.
RAW = "raw"
TRIMMED = "trimmed"        # reversible boundary preparation (Plan 02)
NORMALIZED = "normalized"  # legacy pre-fit loudness pass (levels.mode="legacy")
PHRASED = "phrased"        # per-phrase timing recipe (timing.mode="phrase", Plan 04)
FITTED = "fitted"          # whole-clip timing (timing.mode="whole")
LEVELED = "leveled"        # post-fit baseline level + performance gain (Plan 03)
EDGED = "edged"            # protected final edges (Plan 02)
UNKNOWN = "unknown"
ROLES = (RAW, TRIMMED, NORMALIZED, PHRASED, FITTED, LEVELED, EDGED, UNKNOWN)
# Later plans append their own role here; order defines "most processed last".
# `normalized` stays where Plan 02 put it so a legacy run keeps reproducing
# exactly what it rendered before; Plan 03's level owner sits after fitting,
# because a loudness decision made before time-stretching is a decision about
# audio that no longer exists. Only one of the two ever runs.
#
# `phrased` and `fitted` are the same kind of pair for *timing*: whole-clip
# fitting stretches the line evenly, phrase fitting moves its internal silence
# and stretches only where it has to, and exactly one of them owns a run. The
# phrase role sits first so that, in phrase mode, everything downstream reads
# it through `upstream_of` without knowing which owner produced the timing.
RENDER_ORDER = (RAW, TRIMMED, NORMALIZED, PHRASED, FITTED, LEVELED, EDGED)

ORIGINS = ("import", "legacy", "split", "merge", "manual")
DISPOSITIONS = ("open", "accepted", "fixed", "obsolete")
FINDING_KINDS = ("technical", "content", "timing", "performance", "delivery")

# What boundary preparation decided to do with a take. "kept" and
# "uncertain" are not failures: leaving audio alone is the safe answer.
TRIM_DECISIONS = ("trimmed", "kept", "uncertain", "empty", "bypassed", "unknown")

# How a line's loudness was decided. "legacy" is the pre-Plan-03 path: one
# loudness pass before fitting, owned by the quality stage.
LEVEL_MODES = ("legacy", "consistent", "follow_source", "manual", "off")
# Why a level decision ended where it did. A clamp and a fallback are different
# facts: one bounded a trusted request, the other had nothing to trust.
LEVEL_OUTCOMES = ("applied", "clamped", "fallback", "bypassed", "unavailable", "unknown")
# How much a source measurement can be trusted. `unknown` is never a zero.
MEASUREMENT_STATES = ("measured", "insufficient", "contaminated", "missing", "unknown")
# What a spoken-content check concluded. Only `mismatch` is evidence of wrong
# words; every other non-match state says the check could not establish one.
VERIFY_STATES = ("match", "mismatch", "uncertain", "empty", "unsupported",
                 "failed", "skipped", "unknown")
# How a line is meant to be performed. A mode is not a character and never
# mandates a fixed dB change on its own.
SPEECH_MODES = ("normal", "thought", "whisper", "shout", "call", "broadcast", "unknown")

# Which owner produced a line's timing, and how the plan ended up.
TIMING_MODES = ("whole", "phrase", "bypassed", "unknown")
# `planned` means a recipe exists; `applied` means it was rendered and the
# output measured. `fallback` is the honest state for a line whose phrase
# evidence was not good enough and was fitted as one bounded whole.
TIMING_STATES = ("applied", "planned", "fallback", "infeasible", "bypassed",
                 "unavailable", "unknown")
# How a phrase boundary was established. `whole` is the single-phrase fallback.
PHRASE_METHODS = ("acoustic", "aligned", "manual", "whole", "unknown")
# A reviewer's anchor is `hard`; anything Doblarr derived on its own is `soft`
# and may be missed without that being a failure.
ANCHOR_KINDS = ("hard", "soft")
ANCHOR_EDGES = ("start", "end")
# What a gap between two phrases is. Only `padding` may be redistributed.
PAUSE_KINDS = ("padding", "pause", "hesitation", "breath", "response", "unknown")

# Nonverbal events. `type` is what the evidence says was heard; `unknown` is a
# real and common answer, because a subtitle tag is not a detector.
EVENT_TYPES = ("laugh", "sigh", "gasp", "cry", "scream", "cough", "breath",
               "effort", "applause", "music", "footsteps", "door", "silence",
               "inaudible", "unknown")
# A vocal reaction belongs to a person; a background event belongs to the bed.
EVENT_CATEGORIES = ("vocal", "background", "unknown")
# What was decided about covering an event. `unresolved` is the default and is
# never silently upgraded: nothing is injected on a guess.
EVENT_DECISIONS = ("unresolved", "retain", "replace", "omit", "covered")
# What actually happened. `covered` means existing audio already carries it.
EVENT_COVERAGE = ("unresolved", "retained", "replaced", "omitted", "covered",
                  "unavailable", "unsupported")
# Where the evidence for an event came from.
EVENT_EVIDENCE = ("subtitle", "manual", "detector", "unknown")


class SchemaError(DoblarrError):
    """A persisted cue record is malformed, or was written by a newer Doblarr."""

    http_status = 422


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _finite(value: Any, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise SchemaError(f"{name} must be a number") from exc
    if not math.isfinite(number):
        raise SchemaError(f"{name} must be finite")
    return number


def _opt_float(value: Any, name: str) -> float | None:
    return None if value is None else _finite(value, name)


def _opt_int(value: Any, name: str) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise SchemaError(f"{name} must be a whole number") from exc


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _optional_text(value: Any) -> str | None:
    return None if value is None else str(value)


def _mapping(value: Any, name: str) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise SchemaError(f"{name} must be an object")
    return dict(value)


def _sequence(value: Any, name: str) -> list:
    if value is None:
        return []
    if not isinstance(value, list):
        raise SchemaError(f"{name} must be a list")
    return list(value)


@dataclass
class Span:
    """A half-open ``[start, end)`` interval on one explicitly named timeline."""

    start: float
    end: float
    domain: str = SOURCE

    def __post_init__(self) -> None:
        self.start = _finite(self.start, "span start")
        self.end = _finite(self.end, "span end")
        if self.start < 0:
            raise SchemaError("span start must not be negative")
        if self.end <= self.start:
            raise SchemaError("span end must be after its start")
        if self.domain not in DOMAINS:
            raise SchemaError(f"unknown time domain {self.domain!r}")

    @property
    def duration(self) -> float:
        return self.end - self.start

    def as_dict(self) -> dict:
        return {"start": self.start, "end": self.end, "domain": self.domain}

    @classmethod
    def from_dict(cls, data: Any) -> Span:
        if not isinstance(data, dict):
            raise SchemaError("a span must be an object")
        return cls(_finite(data.get("start"), "span start"),
                   _finite(data.get("end"), "span end"),
                   _text(data.get("domain")) or SOURCE)


def _spans(value: Any, name: str) -> list[Span]:
    return [Span.from_dict(item) for item in _sequence(value, name)]


@dataclass
class SourceReference:
    """Which media and audio stream the source evidence was taken from.

    Owned by the extraction stage. `cut` is the extracted window when only part
    of the media was pulled (a tease); `None` means the whole file.
    """

    media_key: str = ""          # stable identity of the input media
    media_path: str = ""
    stream_index: int | None = None
    language: str = ""
    channel_layout: str = ""
    channels: int | None = None
    extraction_revision: str = ""
    time_base: str = SOURCE
    cut: Span | None = None

    def as_dict(self) -> dict:
        return {
            "media_key": self.media_key,
            "media_path": self.media_path,
            "stream_index": self.stream_index,
            "language": self.language,
            "channel_layout": self.channel_layout,
            "channels": self.channels,
            "extraction_revision": self.extraction_revision,
            "time_base": self.time_base,
            "cut": self.cut.as_dict() if self.cut else None,
        }

    @classmethod
    def from_dict(cls, data: Any) -> SourceReference:
        data = _mapping(data, "source reference")
        cut = data.get("cut")
        return cls(
            media_key=_text(data.get("media_key")),
            media_path=_text(data.get("media_path")),
            stream_index=_opt_int(data.get("stream_index"), "stream index"),
            language=_text(data.get("language")),
            channel_layout=_text(data.get("channel_layout")),
            channels=_opt_int(data.get("channels"), "channel count"),
            extraction_revision=_text(data.get("extraction_revision")),
            time_base=_text(data.get("time_base")) or SOURCE,
            cut=Span.from_dict(cut) if cut else None,
        )


@dataclass
class CueLineage:
    """How a cue came to exist. IDs never encode editable text or list position."""

    origin: str = "import"
    script_ref: str = ""
    ordinal: int | None = None       # position in the ORIGINAL script document
    legacy_index: int | None = None  # the list index a legacy snapshot addressed
    parents: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "origin": self.origin,
            "script_ref": self.script_ref,
            "ordinal": self.ordinal,
            "legacy_index": self.legacy_index,
            "parents": list(self.parents),
        }

    @classmethod
    def from_dict(cls, data: Any) -> CueLineage:
        data = _mapping(data, "cue lineage")
        origin = _text(data.get("origin")) or "import"
        if origin not in ORIGINS:
            raise SchemaError(f"unknown cue origin {origin!r}")
        return cls(
            origin=origin,
            script_ref=_text(data.get("script_ref")),
            ordinal=_opt_int(data.get("ordinal"), "cue ordinal"),
            legacy_index=_opt_int(data.get("legacy_index"), "legacy index"),
            parents=[_text(p) for p in _sequence(data.get("parents"), "cue parents")],
        )


@dataclass
class SourceSpans:
    """Original source intervals for a cue. Target edits never move these."""

    spans: list[Span] = field(default_factory=list)
    speaker: str | None = None
    method: str = "unknown"           # subtitle | asr | aligned | diarized | unknown
    confidence: float | None = None   # None is "unknown", distinct from 0.0
    word_domain: str = SOURCE         # timeline the cue's word evidence lives on
    word_method: str = "unknown"
    word_version: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def start(self) -> float | None:
        return self.spans[0].start if self.spans else None

    @property
    def end(self) -> float | None:
        return self.spans[-1].end if self.spans else None

    def as_dict(self) -> dict:
        return {
            "spans": [s.as_dict() for s in self.spans],
            "speaker": self.speaker,
            "method": self.method,
            "confidence": self.confidence,
            "word_domain": self.word_domain,
            "word_method": self.word_method,
            "word_version": self.word_version,
            "notes": list(self.notes),
        }

    @classmethod
    def from_dict(cls, data: Any) -> SourceSpans:
        data = _mapping(data, "source spans")
        word_domain = _text(data.get("word_domain")) or SOURCE
        if word_domain not in DOMAINS:
            raise SchemaError(f"unknown word time domain {word_domain!r}")
        return cls(
            spans=_spans(data.get("spans"), "source spans"),
            speaker=_optional_text(data.get("speaker")),
            method=_text(data.get("method")) or "unknown",
            confidence=_opt_float(data.get("confidence"), "source confidence"),
            word_domain=word_domain,
            word_method=_text(data.get("word_method")) or "unknown",
            word_version=_text(data.get("word_version")),
            notes=[_text(n) for n in _sequence(data.get("notes"), "source notes")],
        )


@dataclass
class Placement:
    """Target-timeline extras. ``Segment.start``/``end`` stay the cue window."""

    onset: float | None = None   # intended speech onset, seconds after the cue start
    offset: float = 0.0          # manual shift already folded into start/end
    montage: Span | None = None  # this cue's window inside an audition montage

    def as_dict(self) -> dict:
        return {
            "onset": self.onset,
            "offset": self.offset,
            "montage": self.montage.as_dict() if self.montage else None,
        }

    @classmethod
    def from_dict(cls, data: Any) -> Placement:
        data = _mapping(data, "placement")
        montage = data.get("montage")
        onset = _opt_float(data.get("onset"), "placement onset")
        if onset is not None and onset < 0:
            raise SchemaError("placement onset must not be negative")
        return cls(
            onset=onset,
            offset=_finite(data.get("offset", 0.0), "placement offset"),
            montage=Span.from_dict(montage) if montage else None,
        )


@dataclass
class SpeechPreparation:
    """What boundary analysis found in a raw take, and what it did about it.

    All times are in the CLIP domain — offsets inside the take — because a
    take's own timeline is not the source timeline and not the target one.
    `lead`/`tail` are what was actually removed, so the trim is reversible by
    inspection: the raw file plus these offsets reconstructs the derivative.
    """

    take_id: str = ""
    detector: str = ""
    decision: str = "unknown"
    reason: str = ""
    active: Span | None = None          # detected speech-active bounds, CLIP domain
    handle: float = 0.0                 # protective margin kept on each side
    lead: float = 0.0                   # seconds removed from the head
    tail: float = 0.0                   # seconds removed from the tail
    onset: float | None = None          # intended audible onset re-inserted, if any
    raw_duration: float | None = None
    active_duration: float | None = None
    noise_db: float | None = None       # estimated noise floor
    speech_db: float | None = None      # level of the active region
    confidence: float | None = None     # None is unknown, never a silent 0.0
    clipped: bool = False
    silences: list[Span] = field(default_factory=list)  # suspected internal gaps

    def __post_init__(self) -> None:
        if self.decision not in TRIM_DECISIONS:
            raise SchemaError(f"unknown trim decision {self.decision!r}")

    @property
    def trimmed(self) -> float:
        return self.lead + self.tail

    def as_dict(self) -> dict:
        return {
            "take_id": self.take_id,
            "detector": self.detector,
            "decision": self.decision,
            "reason": self.reason,
            "active": self.active.as_dict() if self.active else None,
            "handle": self.handle,
            "lead": self.lead,
            "tail": self.tail,
            "onset": self.onset,
            "raw_duration": self.raw_duration,
            "active_duration": self.active_duration,
            "noise_db": self.noise_db,
            "speech_db": self.speech_db,
            "confidence": self.confidence,
            "clipped": self.clipped,
            "silences": [s.as_dict() for s in self.silences],
        }

    @classmethod
    def from_dict(cls, data: Any) -> SpeechPreparation:
        data = _mapping(data, "speech preparation")
        active = data.get("active")
        return cls(
            take_id=_text(data.get("take_id")),
            detector=_text(data.get("detector")),
            decision=_text(data.get("decision")) or "unknown",
            reason=_text(data.get("reason")),
            active=Span.from_dict(active) if active else None,
            handle=_finite(data.get("handle", 0.0), "trim handle"),
            lead=_finite(data.get("lead", 0.0), "trimmed lead"),
            tail=_finite(data.get("tail", 0.0), "trimmed tail"),
            onset=_opt_float(data.get("onset"), "prepared onset"),
            raw_duration=_opt_float(data.get("raw_duration"), "raw duration"),
            active_duration=_opt_float(data.get("active_duration"), "active duration"),
            noise_db=_opt_float(data.get("noise_db"), "noise floor"),
            speech_db=_opt_float(data.get("speech_db"), "speech level"),
            confidence=_opt_float(data.get("confidence"), "trim confidence"),
            clipped=bool(data.get("clipped", False)),
            silences=_spans(data.get("silences"), "internal silences"),
        )


@dataclass
class PerformanceIntent:
    """How a line is meant to be acted, and what the engine could actually do.

    `mode` and `traits` are structured intent; `direction` is the reviewer's own
    words. `effective` is what was really sent to the engine after composition,
    and `unsupported` names every instruction the adapter could not honor — an
    unsupported instruction is reported, never quietly counted as applied.
    """

    mode: str = "unknown"                 # SPEECH_MODES
    traits: list[str] = field(default_factory=list)   # e.g. ["urgent", "restrained"]
    direction: str = ""                   # freeform reviewer direction
    origin: str = "unknown"               # manual | cast | knowledge | suggestion | unknown
    revision: int = 0                     # accepted intent revision
    treatment: str = ""                   # acoustic treatment reference (Plan 05 owns the DSP)
    effective: str = ""                   # the composed instruction actually requested
    unsupported: list[str] = field(default_factory=list)
    capability: str = "unknown"           # supported | unsupported | unknown
    sources: list[dict] = field(default_factory=list)  # which layer contributed what
    at: str = ""

    def __post_init__(self) -> None:
        if self.mode not in SPEECH_MODES:
            raise SchemaError(f"unknown speech mode {self.mode!r}")

    @property
    def empty(self) -> bool:
        """True when nothing was ever asked for. Distinct from 'asked for normal'."""
        return (self.mode in ("", "unknown") and not self.traits and not self.direction
                and not self.treatment)

    def as_dict(self) -> dict:
        return {
            "mode": self.mode,
            "traits": list(self.traits),
            "direction": self.direction,
            "origin": self.origin,
            "revision": self.revision,
            "treatment": self.treatment,
            "effective": self.effective,
            "unsupported": list(self.unsupported),
            "capability": self.capability,
            "sources": [dict(s) for s in self.sources],
            "at": self.at,
        }

    @classmethod
    def from_dict(cls, data: Any) -> PerformanceIntent:
        data = _mapping(data, "performance intent")
        return cls(
            mode=_text(data.get("mode")) or "unknown",
            traits=[_text(t) for t in _sequence(data.get("traits"), "delivery traits")],
            direction=_text(data.get("direction")),
            origin=_text(data.get("origin")) or "unknown",
            revision=_opt_int(data.get("revision"), "intent revision") or 0,
            treatment=_text(data.get("treatment")),
            effective=_text(data.get("effective")),
            unsupported=[_text(u) for u in
                         _sequence(data.get("unsupported"), "unsupported instructions")],
            capability=_text(data.get("capability")) or "unknown",
            sources=[_mapping(x, "intent source")
                     for x in _sequence(data.get("sources"), "intent sources")],
            at=_text(data.get("at")),
        )


@dataclass
class SourceMeasurement:
    """How loud the original actor was, and how much that number can be trusted.

    Measured on the recorded source stream over this cue's source spans — never
    on a cleaned clone reference or an audition montage, which are conditioned
    audio and say nothing about performance dynamics.
    """

    state: str = "unknown"               # MEASUREMENT_STATES
    speech_db: float | None = None       # speech-active level of this cue
    baseline_db: float | None = None     # ordinary-dialogue reference it is compared to
    relative_db: float | None = None     # speech_db - baseline_db, when both are known
    units: str = ""                      # e.g. "dBFS-rms-speech"
    method: str = ""                     # detector name and version
    channels: str = ""                   # channel/downmix policy actually used
    measured_seconds: float | None = None
    confidence: float | None = None      # None is unknown, never a silent 0.0
    baseline_scope: str = ""             # global | speaker | fallback
    baseline_samples: int = 0
    overlapped: bool = False             # another speaker talks across this cue
    contaminated: bool = False           # music/FX or separation artifacts in the reference
    exclusions: list[str] = field(default_factory=list)
    source: str = ""                     # which stream the measurement came from
    inputs: str = ""                     # fingerprint of the exact measured inputs

    def __post_init__(self) -> None:
        if self.state not in MEASUREMENT_STATES:
            raise SchemaError(f"unknown measurement state {self.state!r}")

    @property
    def trusted(self) -> bool:
        """Only a clean, sufficient measurement may drive automatic gain."""
        return (self.state == "measured" and self.relative_db is not None
                and not self.overlapped and not self.contaminated)

    def as_dict(self) -> dict:
        return {
            "state": self.state,
            "speech_db": self.speech_db,
            "baseline_db": self.baseline_db,
            "relative_db": self.relative_db,
            "units": self.units,
            "method": self.method,
            "channels": self.channels,
            "measured_seconds": self.measured_seconds,
            "confidence": self.confidence,
            "baseline_scope": self.baseline_scope,
            "baseline_samples": self.baseline_samples,
            "overlapped": self.overlapped,
            "contaminated": self.contaminated,
            "exclusions": list(self.exclusions),
            "source": self.source,
            "inputs": self.inputs,
        }

    @classmethod
    def from_dict(cls, data: Any) -> SourceMeasurement:
        data = _mapping(data, "source measurement")
        return cls(
            state=_text(data.get("state")) or "unknown",
            speech_db=_opt_float(data.get("speech_db"), "source speech level"),
            baseline_db=_opt_float(data.get("baseline_db"), "dialogue baseline"),
            relative_db=_opt_float(data.get("relative_db"), "relative level"),
            units=_text(data.get("units")),
            method=_text(data.get("method")),
            channels=_text(data.get("channels")),
            measured_seconds=_opt_float(data.get("measured_seconds"), "measured seconds"),
            confidence=_opt_float(data.get("confidence"), "measurement confidence"),
            baseline_scope=_text(data.get("baseline_scope")),
            baseline_samples=_opt_int(data.get("baseline_samples"), "baseline samples") or 0,
            overlapped=bool(data.get("overlapped", False)),
            contaminated=bool(data.get("contaminated", False)),
            exclusions=[_text(x) for x in
                        _sequence(data.get("exclusions"), "measurement exclusions")],
            source=_text(data.get("source")),
            inputs=_text(data.get("inputs")),
        )


@dataclass
class LevelDecision:
    """What the one level owner did to this line, and why.

    Recorded whether or not any gain was applied, because "nothing was needed"
    and "we could not tell" are different answers a reviewer has to be able to
    tell apart. `requested_db` minus `applied_db` is exactly what the bounds
    took away.
    """

    mode: str = "off"                    # LEVEL_MODES
    outcome: str = "unknown"             # LEVEL_OUTCOMES
    target_db: float | None = None       # the baseline loudness target
    measured_db: float | None = None     # the generated line measured after fitting
    requested_db: float = 0.0            # performance gain asked for
    applied_db: float = 0.0              # performance gain actually applied
    baseline_db: float = 0.0             # gain used to reach the baseline target
    strength: float = 1.0                # how much of the source contrast was followed
    bound_db: float = 0.0                # the per-side bound in force
    peak: float | None = None            # measured peak of the result, 0..1
    peak_limited: bool = False
    reason: str = ""
    manual: bool = False                 # a reviewer set this gain by hand
    inputs: str = ""

    def __post_init__(self) -> None:
        if self.mode not in LEVEL_MODES:
            raise SchemaError(f"unknown level mode {self.mode!r}")
        if self.outcome not in LEVEL_OUTCOMES:
            raise SchemaError(f"unknown level outcome {self.outcome!r}")

    @property
    def clamped_db(self) -> float:
        return round(self.requested_db - self.applied_db, 3)

    def as_dict(self) -> dict:
        return {
            "mode": self.mode,
            "outcome": self.outcome,
            "target_db": self.target_db,
            "measured_db": self.measured_db,
            "requested_db": self.requested_db,
            "applied_db": self.applied_db,
            "baseline_db": self.baseline_db,
            "strength": self.strength,
            "bound_db": self.bound_db,
            "peak": self.peak,
            "peak_limited": self.peak_limited,
            "reason": self.reason,
            "manual": self.manual,
            "inputs": self.inputs,
        }

    @classmethod
    def from_dict(cls, data: Any) -> LevelDecision:
        data = _mapping(data, "level decision")
        return cls(
            mode=_text(data.get("mode")) or "off",
            outcome=_text(data.get("outcome")) or "unknown",
            target_db=_opt_float(data.get("target_db"), "level target"),
            measured_db=_opt_float(data.get("measured_db"), "measured level"),
            requested_db=_finite(data.get("requested_db", 0.0), "requested gain"),
            applied_db=_finite(data.get("applied_db", 0.0), "applied gain"),
            baseline_db=_finite(data.get("baseline_db", 0.0), "baseline gain"),
            strength=_finite(data.get("strength", 1.0), "matching strength"),
            bound_db=_finite(data.get("bound_db", 0.0), "gain bound"),
            peak=_opt_float(data.get("peak"), "output peak"),
            peak_limited=bool(data.get("peak_limited", False)),
            reason=_text(data.get("reason")),
            manual=bool(data.get("manual", False)),
            inputs=_text(data.get("inputs")),
        )


@dataclass
class Verification:
    """What Doblarr expected to hear against what recognition actually heard.

    `state` separates a real mismatch from every reason a check could not
    establish one. `differences` is the ordered alignment, so review can show
    where a word went missing instead of only a similarity score.
    """

    state: str = "unknown"               # VERIFY_STATES
    policy: str = "off"                  # off | suspicious | all
    reason: str = ""                     # why this line was or was not checked
    expected: str = ""                   # the effective spoken text that was requested
    heard: str = ""                      # raw recognition output, unedited
    language: str = ""
    tokenizer: str = ""                  # which tokenizer ran, or the fallback used
    similarity: float | None = None
    confidence: float | None = None      # recognizer confidence when it supplies one
    differences: list[dict] = field(default_factory=list)
    critical: list[dict] = field(default_factory=list)  # names/numbers/negation at risk
    recognizer: str = ""                 # engine/model/version when known
    checker: str = ""                    # checker name and version
    target: str = ""                     # take id or artifact role that was listened to
    inputs: str = ""                     # verification fingerprint of the exact inputs
    at: str = ""
    attempts: int = 0                    # bounded repairs already spent on this line

    def __post_init__(self) -> None:
        if self.state not in VERIFY_STATES:
            raise SchemaError(f"unknown verification state {self.state!r}")

    @property
    def checked(self) -> bool:
        """A line is only 'verified' when recognition actually produced a verdict."""
        return self.state in ("match", "mismatch", "uncertain")

    def as_dict(self) -> dict:
        return {
            "state": self.state,
            "policy": self.policy,
            "reason": self.reason,
            "expected": self.expected,
            "heard": self.heard,
            "language": self.language,
            "tokenizer": self.tokenizer,
            "similarity": self.similarity,
            "confidence": self.confidence,
            "differences": [dict(d) for d in self.differences],
            "critical": [dict(c) for c in self.critical],
            "recognizer": self.recognizer,
            "checker": self.checker,
            "target": self.target,
            "inputs": self.inputs,
            "at": self.at,
            "attempts": self.attempts,
        }

    @classmethod
    def from_dict(cls, data: Any) -> Verification:
        data = _mapping(data, "verification")
        return cls(
            state=_text(data.get("state")) or "unknown",
            policy=_text(data.get("policy")) or "off",
            reason=_text(data.get("reason")),
            expected=_text(data.get("expected")),
            heard=_text(data.get("heard")),
            language=_text(data.get("language")),
            tokenizer=_text(data.get("tokenizer")),
            similarity=_opt_float(data.get("similarity"), "similarity"),
            confidence=_opt_float(data.get("confidence"), "recognition confidence"),
            differences=[_mapping(d, "difference")
                         for d in _sequence(data.get("differences"), "differences")],
            critical=[_mapping(c, "critical term")
                      for c in _sequence(data.get("critical"), "critical terms")],
            recognizer=_text(data.get("recognizer")),
            checker=_text(data.get("checker")),
            target=_text(data.get("target")),
            inputs=_text(data.get("inputs")),
            at=_text(data.get("at")),
            attempts=_opt_int(data.get("attempts"), "repair attempts") or 0,
        )


@dataclass
class Artifact:
    """One audio file produced for a cue, with the role it can actually prove."""

    role: str = UNKNOWN
    path: str = ""
    fingerprint: str = ""
    derived_from: str = ""       # role of the immutable input it was produced from
    duration: float | None = None
    bytes: int | None = None
    sha256: str | None = None
    proven: bool = True          # False: migrated audio whose role is a best guess

    def __post_init__(self) -> None:
        if self.role not in ROLES:
            raise SchemaError(f"unknown artifact role {self.role!r}")

    def as_dict(self) -> dict:
        return {
            "role": self.role,
            "path": self.path,
            "fingerprint": self.fingerprint,
            "derived_from": self.derived_from,
            "duration": self.duration,
            "bytes": self.bytes,
            "sha256": self.sha256,
            "proven": self.proven,
        }

    @classmethod
    def from_dict(cls, data: Any) -> Artifact:
        data = _mapping(data, "artifact")
        return cls(
            role=_text(data.get("role")) or UNKNOWN,
            path=_text(data.get("path")),
            fingerprint=_text(data.get("fingerprint")),
            derived_from=_text(data.get("derived_from")),
            duration=_opt_float(data.get("duration"), "artifact duration"),
            bytes=_opt_int(data.get("bytes"), "artifact size"),
            sha256=_optional_text(data.get("sha256")),
            proven=bool(data.get("proven", True)),
        )

    def exists(self) -> bool:
        return bool(self.path) and Path(self.path).is_file()


@dataclass
class Phrase:
    """One spoken run inside a take, between two silences it did not invent.

    `clip` is where the phrase sits in the *prepared* take (CLIP domain), which
    is the only timeline a generated utterance actually has. `source` is the
    original interval or intervals this phrase is believed to correspond to —
    it may name several, and it is very often empty, because translated word
    order does not map onto the original one to one. An empty `source` is
    "unknown correspondence", never "no correspondence".

    A reviewer's own edits to a line's phrasing live on `Pause.origin` and
    `Anchor.origin`, not here: they protect a pause or anchor an edge, and
    drawing phrase boundaries by hand is explicitly outside this plan.
    """

    phrase_id: str = ""
    order: int = 0
    text: str = ""                 # the words believed to be spoken here, when known
    clip: Span | None = None       # CLIP domain, inside the prepared take
    source: list[Span] = field(default_factory=list)   # SOURCE domain, possibly empty
    method: str = "unknown"        # PHRASE_METHODS
    confidence: float | None = None

    def __post_init__(self) -> None:
        if self.method not in PHRASE_METHODS:
            raise SchemaError(f"unknown phrase method {self.method!r}")

    def as_dict(self) -> dict:
        return {
            "phrase_id": self.phrase_id,
            "order": self.order,
            "text": self.text,
            "clip": self.clip.as_dict() if self.clip else None,
            "source": [s.as_dict() for s in self.source],
            "method": self.method,
            "confidence": self.confidence,
        }

    @classmethod
    def from_dict(cls, data: Any) -> Phrase:
        data = _mapping(data, "phrase")
        clip = data.get("clip")
        return cls(
            phrase_id=_text(data.get("phrase_id")),
            order=_opt_int(data.get("order"), "phrase order") or 0,
            text=_text(data.get("text")),
            clip=Span.from_dict(clip) if clip else None,
            source=_spans(data.get("source"), "phrase source"),
            method=_text(data.get("method")) or "unknown",
            confidence=_opt_float(data.get("confidence"), "phrase confidence"),
        )


@dataclass
class Anchor:
    """A time a phrase edge is meant to land on, in the cue's own target window.

    `at` is seconds after the cue start, on the TARGET timeline. A `hard`
    anchor is a reviewer's instruction and a plan that cannot honour it is
    infeasible; a `soft` anchor is Doblarr's own suggestion and missing it is
    reported, not treated as a failure.
    """

    anchor_id: str = ""
    phrase_id: str = ""
    edge: str = "start"            # ANCHOR_EDGES
    at: float = 0.0                # seconds after the cue start, TARGET domain
    kind: str = "soft"             # ANCHOR_KINDS
    tolerance: float = 0.12
    origin: str = "auto"           # auto | source | review
    note: str = ""
    observed: float | None = None  # where the edge actually landed once rendered

    def __post_init__(self) -> None:
        if self.kind not in ANCHOR_KINDS:
            raise SchemaError(f"unknown anchor kind {self.kind!r}")
        if self.edge not in ANCHOR_EDGES:
            raise SchemaError(f"unknown anchor edge {self.edge!r}")
        self.at = _finite(self.at, "anchor time")
        if self.at < 0:
            raise SchemaError("an anchor must not sit before the cue starts")
        self.tolerance = abs(_finite(self.tolerance, "anchor tolerance"))

    @property
    def error(self) -> float | None:
        """How far the rendered edge missed this anchor, once it is known."""
        return None if self.observed is None else round(self.observed - self.at, 4)

    def as_dict(self) -> dict:
        return {"anchor_id": self.anchor_id, "phrase_id": self.phrase_id,
                "edge": self.edge, "at": self.at, "kind": self.kind,
                "tolerance": self.tolerance, "origin": self.origin,
                "note": self.note, "observed": self.observed}

    @classmethod
    def from_dict(cls, data: Any) -> Anchor:
        data = _mapping(data, "anchor")
        return cls(
            anchor_id=_text(data.get("anchor_id")),
            phrase_id=_text(data.get("phrase_id")),
            edge=_text(data.get("edge")) or "start",
            at=_finite(data.get("at", 0.0), "anchor time"),
            kind=_text(data.get("kind")) or "soft",
            tolerance=_finite(data.get("tolerance", 0.12), "anchor tolerance"),
            origin=_text(data.get("origin")) or "auto",
            note=_text(data.get("note")),
            observed=_opt_float(data.get("observed"), "observed anchor"),
        )


@dataclass
class Pause:
    """A gap between two phrases, and whether it may be shortened.

    Only `padding` is redistributable. A hesitation, a breath and the beat
    before a reply are performance: they keep their measured length unless a
    reviewer says otherwise, because removing them is exactly how a fitted line
    starts sounding rushed.
    """

    pause_id: str = ""
    after: str = ""                # phrase_id this gap follows
    clip: Span | None = None       # the measured gap in the prepared take (CLIP)
    kind: str = "padding"          # PAUSE_KINDS
    protected: bool = False
    origin: str = "auto"           # auto | review
    planned: float | None = None   # seconds this gap is given in the recipe

    def __post_init__(self) -> None:
        if self.kind not in PAUSE_KINDS:
            raise SchemaError(f"unknown pause kind {self.kind!r}")

    @property
    def measured(self) -> float:
        return self.clip.duration if self.clip else 0.0

    def as_dict(self) -> dict:
        return {"pause_id": self.pause_id, "after": self.after,
                "clip": self.clip.as_dict() if self.clip else None,
                "kind": self.kind, "protected": self.protected,
                "origin": self.origin, "planned": self.planned}

    @classmethod
    def from_dict(cls, data: Any) -> Pause:
        data = _mapping(data, "pause")
        clip = data.get("clip")
        return cls(
            pause_id=_text(data.get("pause_id")),
            after=_text(data.get("after")),
            clip=Span.from_dict(clip) if clip else None,
            kind=_text(data.get("kind")) or "padding",
            protected=bool(data.get("protected", False)),
            origin=_text(data.get("origin")) or "auto",
            planned=_opt_float(data.get("planned"), "planned pause"),
        )


@dataclass
class TimingPlan:
    """The recipe that turned one prepared take into its timed derivative.

    It is a *plan plus what happened*: `pieces` is the ordered instruction the
    renderer followed, and `actual_duration` is what came back out, measured
    rather than assumed — an atempo factor is a request, not a guarantee.

    `state` separates a rendered plan from one that could not be satisfied.
    `infeasible` is a real, reviewable outcome: the words do not fit the slot
    within the bounds, and the answer is a repair or a human decision, not a
    silently dropped clause.
    """

    mode: str = "unknown"                 # TIMING_MODES
    state: str = "unknown"                # TIMING_STATES
    reason: str = ""
    planner: str = ""                     # planner name and version
    phrases: list[Phrase] = field(default_factory=list)
    anchors: list[Anchor] = field(default_factory=list)
    pauses: list[Pause] = field(default_factory=list)
    pieces: list[dict] = field(default_factory=list)   # ordered render instruction
    onset: float | None = None            # intended audible onset inside the slot
    slot: float | None = None             # the target window this was planned for
    planned_duration: float | None = None
    actual_duration: float | None = None  # measured from the rendered output
    max_stretch: float = 1.0
    min_stretch: float = 1.0
    moved: float = 0.0                    # seconds of padding redistributed
    protected_kept: float = 0.0           # seconds of protected pause preserved
    speech_in: float = 0.0                # speech seconds entering the renderer
    speech_out: float = 0.0               # speech seconds leaving it
    conflicts: list[dict] = field(default_factory=list)
    inputs: str = ""                      # processing fingerprint of the exact inputs
    attempts: int = 0                     # bounded repairs already spent here
    bypassed: bool = False                # a reviewer asked for no timing edit
    at: str = ""

    def __post_init__(self) -> None:
        if self.mode not in TIMING_MODES:
            raise SchemaError(f"unknown timing mode {self.mode!r}")
        if self.state not in TIMING_STATES:
            raise SchemaError(f"unknown timing state {self.state!r}")

    @property
    def planned(self) -> bool:
        """Whether this plan describes real work, as opposed to a bypass."""
        return self.state in ("applied", "planned", "fallback")

    def phrase(self, phrase_id: str) -> Phrase | None:
        return next((p for p in self.phrases if p.phrase_id == phrase_id), None)

    def as_dict(self) -> dict:
        return {
            "mode": self.mode,
            "state": self.state,
            "reason": self.reason,
            "planner": self.planner,
            "phrases": [p.as_dict() for p in self.phrases],
            "anchors": [a.as_dict() for a in self.anchors],
            "pauses": [p.as_dict() for p in self.pauses],
            "pieces": [dict(piece) for piece in self.pieces],
            "onset": self.onset,
            "slot": self.slot,
            "planned_duration": self.planned_duration,
            "actual_duration": self.actual_duration,
            "max_stretch": self.max_stretch,
            "min_stretch": self.min_stretch,
            "moved": self.moved,
            "protected_kept": self.protected_kept,
            "speech_in": self.speech_in,
            "speech_out": self.speech_out,
            "conflicts": [dict(c) for c in self.conflicts],
            "inputs": self.inputs,
            "attempts": self.attempts,
            "bypassed": self.bypassed,
            "at": self.at,
        }

    @classmethod
    def from_dict(cls, data: Any) -> TimingPlan:
        data = _mapping(data, "timing plan")
        return cls(
            mode=_text(data.get("mode")) or "unknown",
            state=_text(data.get("state")) or "unknown",
            reason=_text(data.get("reason")),
            planner=_text(data.get("planner")),
            phrases=[Phrase.from_dict(p) for p in _sequence(data.get("phrases"), "phrases")],
            anchors=[Anchor.from_dict(a) for a in _sequence(data.get("anchors"), "anchors")],
            pauses=[Pause.from_dict(p) for p in _sequence(data.get("pauses"), "pauses")],
            pieces=[_mapping(piece, "timing piece")
                    for piece in _sequence(data.get("pieces"), "timing pieces")],
            onset=_opt_float(data.get("onset"), "timing onset"),
            slot=_opt_float(data.get("slot"), "timing slot"),
            planned_duration=_opt_float(data.get("planned_duration"), "planned duration"),
            actual_duration=_opt_float(data.get("actual_duration"), "actual duration"),
            max_stretch=_finite(data.get("max_stretch", 1.0), "max stretch"),
            min_stretch=_finite(data.get("min_stretch", 1.0), "min stretch"),
            moved=_finite(data.get("moved", 0.0), "redistributed silence"),
            protected_kept=_finite(data.get("protected_kept", 0.0), "protected pause"),
            speech_in=_finite(data.get("speech_in", 0.0), "speech in"),
            speech_out=_finite(data.get("speech_out", 0.0), "speech out"),
            conflicts=[_mapping(c, "timing conflict")
                       for c in _sequence(data.get("conflicts"), "timing conflicts")],
            inputs=_text(data.get("inputs")),
            attempts=_opt_int(data.get("attempts"), "timing attempts") or 0,
            bypassed=bool(data.get("bypassed", False)),
            at=_text(data.get("at")),
        )


@dataclass
class NonverbalEvent:
    """A laugh, a gasp, a door — something heard that nobody says.

    Dropping a cue from synthesis is not the same as knowing nothing was heard
    there, so every non-spoken cue becomes one of these. The record keeps the
    original cue text verbatim, because a parser that decided `[gasps]` is a
    gasp can be wrong and the words are the evidence.

    `decision` is what a person (or a configured policy) asked for; `coverage`
    is what actually happened. They are separate on purpose: asking for a
    replacement whose asset is missing leaves `decision="replace"` and
    `coverage="unavailable"`, which is a visible gap rather than a silent one.
    """

    event_id: str = ""
    cue_id: str = ""                # the cue this evidence came from, if any
    in_mix: bool = False            # whether this event's audio reaches the dub
    type: str = "unknown"           # EVENT_TYPES
    category: str = "unknown"       # EVENT_CATEGORIES
    speaker: str | None = None      # possible speaker; never a proven one
    text: str = ""                  # the original cue text, unedited
    source: list[Span] = field(default_factory=list)
    target: Span | None = None      # where it would be placed in the dub
    evidence: str = "subtitle"      # EVENT_EVIDENCE
    confidence: float | None = None
    decision: str = "unresolved"    # EVENT_DECISIONS
    coverage: str = "unresolved"    # EVENT_COVERAGE
    reason: str = ""
    artifact: Artifact | None = None      # the audio actually placed, if any
    asset: str = ""                 # a supplied local replacement/patch, if any
    gain_db: float = 0.0
    fade_in: float = 0.0
    fade_out: float = 0.0
    handle: float = 0.0
    checks: dict = field(default_factory=dict)   # contamination / duplication evidence
    findings: list[Finding] = field(default_factory=list)
    origin: str = "auto"            # auto | manual
    inputs: str = ""
    at: str = ""

    def __post_init__(self) -> None:
        if self.type not in EVENT_TYPES:
            raise SchemaError(f"unknown nonverbal event type {self.type!r}")
        if self.category not in EVENT_CATEGORIES:
            raise SchemaError(f"unknown nonverbal event category {self.category!r}")
        if self.decision not in EVENT_DECISIONS:
            raise SchemaError(f"unknown nonverbal decision {self.decision!r}")
        if self.coverage not in EVENT_COVERAGE:
            raise SchemaError(f"unknown nonverbal coverage {self.coverage!r}")
        if self.evidence not in EVENT_EVIDENCE:
            raise SchemaError(f"unknown nonverbal evidence {self.evidence!r}")

    @property
    def rendered(self) -> bool:
        """Whether this event has audio on disk that review can play."""
        return (self.coverage in ("retained", "replaced") and self.artifact is not None
                and self.artifact.exists())

    @property
    def placed(self) -> bool:
        """Whether this event contributes audio to the dub right now.

        Separate from `rendered` on purpose: `coverage.mode = review`
        prepares the sound so it can be auditioned next to the scene and
        leaves it out of the mix until somebody switches the run to
        `retain`. Hearing a candidate reaction and shipping it are
        different decisions.
        """
        return self.rendered and self.in_mix

    @property
    def span(self) -> Span | None:
        return self.source[0] if self.source else None

    def as_dict(self) -> dict:
        return {
            "event_id": self.event_id,
            "cue_id": self.cue_id,
            "in_mix": self.in_mix,
            "type": self.type,
            "category": self.category,
            "speaker": self.speaker,
            "text": self.text,
            "source": [s.as_dict() for s in self.source],
            "target": self.target.as_dict() if self.target else None,
            "evidence": self.evidence,
            "confidence": self.confidence,
            "decision": self.decision,
            "coverage": self.coverage,
            "reason": self.reason,
            "artifact": self.artifact.as_dict() if self.artifact else None,
            "asset": self.asset,
            "gain_db": self.gain_db,
            "fade_in": self.fade_in,
            "fade_out": self.fade_out,
            "handle": self.handle,
            "checks": dict(self.checks),
            "findings": [f.as_dict() for f in self.findings],
            "origin": self.origin,
            "inputs": self.inputs,
            "at": self.at,
        }

    @classmethod
    def from_dict(cls, data: Any) -> NonverbalEvent:
        """Read an event, migrating the plain dictionaries Plan 01 wrote.

        Those rows carried `cue_id`, `type`, `speaker`, `text`, `source` and
        `coverage="uncovered"`. The old `uncovered` becomes `unresolved`, which
        is the same claim in this record's vocabulary: nothing was decided, and
        nothing is known about whether the sound survived.
        """
        data = _mapping(data, "nonverbal event")
        target = data.get("target")
        artifact = data.get("artifact")
        kind = _text(data.get("type")) or "unknown"
        coverage = _text(data.get("coverage")) or "unresolved"
        cue = _text(data.get("cue_id"))
        return cls(
            # A Plan 01 row has no id at all. It gets the one the parser would
            # derive for the same cue, so the migrated event and a freshly
            # parsed one are the same event rather than two.
            event_id=_text(data.get("event_id")) or (event_id(cue, 0) if cue else ""),
            cue_id=cue,
            in_mix=bool(data.get("in_mix", False)),
            type=kind if kind in EVENT_TYPES else "unknown",
            category=_text(data.get("category")) or "unknown",
            speaker=_optional_text(data.get("speaker")),
            text=_text(data.get("text")),
            source=_spans(data.get("source"), "event source"),
            target=Span.from_dict(target) if target else None,
            evidence=_text(data.get("evidence")) or "subtitle",
            confidence=_opt_float(data.get("confidence"), "event confidence"),
            decision=_text(data.get("decision")) or "unresolved",
            coverage="unresolved" if coverage == "uncovered" else coverage,
            reason=_text(data.get("reason")),
            artifact=Artifact.from_dict(artifact) if artifact else None,
            asset=_text(data.get("asset")),
            gain_db=_finite(data.get("gain_db", 0.0), "event gain"),
            fade_in=_finite(data.get("fade_in", 0.0), "event fade in"),
            fade_out=_finite(data.get("fade_out", 0.0), "event fade out"),
            handle=_finite(data.get("handle", 0.0), "event handle"),
            checks=_mapping(data.get("checks"), "event checks"),
            findings=[Finding.from_dict(f)
                      for f in _sequence(data.get("findings"), "event findings")],
            origin=_text(data.get("origin")) or "auto",
            inputs=_text(data.get("inputs")),
            at=_text(data.get("at")),
        )


@dataclass
class Take:
    """One generation of a cue. Raw audio is never overwritten by processing."""

    take_id: str = ""
    fingerprint: str = ""
    engine: str = ""
    model: str | None = None
    profile: str | None = None
    voice_revision: str = ""
    text: str = ""               # the effective spoken text that was requested
    direction: str = ""          # the effective delivery direction
    seed: int | None = None
    line_revision: int = 0
    state: str = "unknown"       # generated | reused | failed | unknown
    remote_id: str | None = None
    raw: Artifact | None = None
    created_at: str = ""
    # Candidate provenance (Plan 03). `origin` separates the take the pipeline
    # made on its own from one a reviewer asked for; `checks` holds the
    # technical measurements a ranking may explain itself with. A number here
    # is never an acting judgement.
    origin: str = "auto"         # auto | candidate | repair | migrated
    attempt: int = 0
    intent: PerformanceIntent | None = None
    checks: dict = field(default_factory=dict)
    error: str = ""              # why a failed take failed, kept as evidence

    def as_dict(self) -> dict:
        return {
            "take_id": self.take_id,
            "fingerprint": self.fingerprint,
            "engine": self.engine,
            "model": self.model,
            "profile": self.profile,
            "voice_revision": self.voice_revision,
            "text": self.text,
            "direction": self.direction,
            "seed": self.seed,
            "line_revision": self.line_revision,
            "state": self.state,
            "remote_id": self.remote_id,
            "raw": self.raw.as_dict() if self.raw else None,
            "created_at": self.created_at,
            "origin": self.origin,
            "attempt": self.attempt,
            "intent": self.intent.as_dict() if self.intent else None,
            "checks": dict(self.checks),
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: Any) -> Take:
        data = _mapping(data, "take")
        raw = data.get("raw")
        identifier = _text(data.get("take_id"))
        if not identifier:
            raise SchemaError("a take needs a take_id")
        return cls(
            take_id=identifier,
            fingerprint=_text(data.get("fingerprint")),
            engine=_text(data.get("engine")),
            model=_optional_text(data.get("model")),
            profile=_optional_text(data.get("profile")),
            voice_revision=_text(data.get("voice_revision")),
            text=_text(data.get("text")),
            direction=_text(data.get("direction")),
            seed=_opt_int(data.get("seed"), "take seed"),
            line_revision=_opt_int(data.get("line_revision"), "line revision") or 0,
            state=_text(data.get("state")) or "unknown",
            remote_id=_optional_text(data.get("remote_id")),
            raw=Artifact.from_dict(raw) if raw else None,
            created_at=_text(data.get("created_at")),
            origin=_text(data.get("origin")) or "auto",
            attempt=_opt_int(data.get("attempt"), "take attempt") or 0,
            intent=(PerformanceIntent.from_dict(data["intent"])
                    if data.get("intent") else None),
            checks=_mapping(data.get("checks"), "take checks"),
            error=_text(data.get("error")),
        )


@dataclass
class Selection:
    """Which take is rendered, and why. Resume must not silently pick another."""

    take_id: str = ""
    reason: str = "auto"      # auto | review | candidate | restored | migrated
    actor: str = ""
    previous: str | None = None
    at: str = ""

    def as_dict(self) -> dict:
        return {"take_id": self.take_id, "reason": self.reason, "actor": self.actor,
                "previous": self.previous, "at": self.at}

    @classmethod
    def from_dict(cls, data: Any) -> Selection:
        data = _mapping(data, "selection")
        return cls(
            take_id=_text(data.get("take_id")),
            reason=_text(data.get("reason")) or "auto",
            actor=_text(data.get("actor")),
            previous=_optional_text(data.get("previous")),
            at=_text(data.get("at")),
        )


@dataclass
class Finding:
    """An observation about exact inputs. Severity is not confidence."""

    finding_id: str = ""
    code: str = ""
    kind: str = "technical"
    severity: str = "warning"     # info | warning | error
    confidence: float | None = None
    scope: str = "cue"            # cue | take | render | event
    target: str = ""              # take id or artifact role the finding is about
    span: Span | None = None
    detector: str = ""            # detector name and version
    evidence: dict = field(default_factory=dict)
    disposition: str = "open"
    inputs: str = ""              # fingerprint of the exact inputs checked
    history: list[dict] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.kind not in FINDING_KINDS:
            raise SchemaError(f"unknown finding kind {self.kind!r}")
        if self.disposition not in DISPOSITIONS:
            raise SchemaError(f"unknown finding disposition {self.disposition!r}")

    def as_dict(self) -> dict:
        return {
            "finding_id": self.finding_id,
            "code": self.code,
            "kind": self.kind,
            "severity": self.severity,
            "confidence": self.confidence,
            "scope": self.scope,
            "target": self.target,
            "span": self.span.as_dict() if self.span else None,
            "detector": self.detector,
            "evidence": dict(self.evidence),
            "disposition": self.disposition,
            "inputs": self.inputs,
            "history": [dict(h) for h in self.history],
        }

    @classmethod
    def from_dict(cls, data: Any) -> Finding:
        data = _mapping(data, "finding")
        span = data.get("span")
        return cls(
            finding_id=_text(data.get("finding_id")),
            code=_text(data.get("code")),
            kind=_text(data.get("kind")) or "technical",
            severity=_text(data.get("severity")) or "warning",
            confidence=_opt_float(data.get("confidence"), "finding confidence"),
            scope=_text(data.get("scope")) or "cue",
            target=_text(data.get("target")),
            span=Span.from_dict(span) if span else None,
            detector=_text(data.get("detector")),
            evidence=_mapping(data.get("evidence"), "finding evidence"),
            disposition=_text(data.get("disposition")) or "open",
            inputs=_text(data.get("inputs")),
            history=[_mapping(h, "finding history") for h in
                     _sequence(data.get("history"), "finding history")],
        )


@dataclass
class CueAudio:
    """Every audio artifact belonging to one cue, by role, plus take selection."""

    takes: list[Take] = field(default_factory=list)
    selection: Selection | None = None
    renders: list[Artifact] = field(default_factory=list)

    def take(self, take_id: str) -> Take | None:
        return next((t for t in self.takes if t.take_id == take_id), None)

    def selected(self) -> Take | None:
        if self.selection and self.selection.take_id:
            return self.take(self.selection.take_id)
        return self.takes[-1] if self.takes else None

    def raw(self) -> Artifact | None:
        take = self.selected()
        return take.raw if take else None

    def render(self, role: str) -> Artifact | None:
        return next((a for a in self.renders if a.role == role), None)

    def current(self) -> Artifact | None:
        """The most processed artifact available — what the mix would place."""
        for role in reversed(RENDER_ORDER):
            found = self.render(role)
            if found is not None:
                return found
        if self.renders:
            return self.renders[-1]
        return self.raw()

    def upstream_of(self, role: str) -> Artifact | None:
        """The most processed artifact strictly before `role`, else the raw take.

        A processing step reads this rather than `current()`, so re-running it
        with new settings reprocesses its input instead of its own output.
        """
        if role not in RENDER_ORDER:
            return self.current()
        for candidate in reversed(RENDER_ORDER[:RENDER_ORDER.index(role)]):
            if candidate == RAW:
                break
            found = self.render(candidate)
            if found is not None:
                return found
        return self.raw()

    def put_render(self, artifact: Artifact) -> Artifact:
        """Register (or replace) the derivative for `artifact.role`.

        Replacing an artifact with different audio invalidates everything
        downstream of it: a derivative made from the previous version is not a
        derivative of this one, and silently keeping it is how a stale render
        reaches the mix.
        """
        previous = self.render(artifact.role)
        self.renders = [a for a in self.renders if a.role != artifact.role]
        self.renders.append(artifact)
        self.renders.sort(key=lambda a: RENDER_ORDER.index(a.role)
                          if a.role in RENDER_ORDER else len(RENDER_ORDER))
        if previous is None or previous.fingerprint != artifact.fingerprint:
            self.invalidate_after(artifact.role)
        return artifact

    def invalidate_after(self, role: str) -> None:
        """Drop every derivative downstream of `role` in the processing order."""
        if role not in RENDER_ORDER:
            return
        downstream = set(RENDER_ORDER[RENDER_ORDER.index(role) + 1:])
        self.renders = [a for a in self.renders if a.role not in downstream]

    def drop_renders(self, roles) -> None:
        """Invalidate derivatives; the immutable raw take is never dropped."""
        self.renders = [a for a in self.renders if a.role not in tuple(roles)]

    def as_dict(self) -> dict:
        return {
            "takes": [t.as_dict() for t in self.takes],
            "selection": self.selection.as_dict() if self.selection else None,
            "renders": [a.as_dict() for a in self.renders],
        }

    @classmethod
    def from_dict(cls, data: Any) -> CueAudio:
        data = _mapping(data, "cue audio")
        selection = data.get("selection")
        return cls(
            takes=[Take.from_dict(t) for t in _sequence(data.get("takes"), "takes")],
            selection=Selection.from_dict(selection) if selection else None,
            renders=[Artifact.from_dict(a) for a in _sequence(data.get("renders"), "renders")],
        )


# --------------------------------------------------------------------------
# Identity
# --------------------------------------------------------------------------

def script_ref(input_file, source_lang: str, subtitle_file=None) -> str:
    """Stable identity of the *source document* a cue was imported from.

    Deliberately excludes the target language, so the same source cue keeps one
    identity across locales, and excludes translated text, so wording edits
    cannot mint a new cue.
    """
    return digest({
        "input": str(Path(input_file).resolve()) if input_file else "",
        "source_lang": source_lang or "",
        "subtitles": str(Path(subtitle_file).resolve()) if subtitle_file else None,
    })[:16]


def imported_cue_id(script: str, ordinal: int) -> str:
    return digest({"script": script, "origin": "import", "ordinal": int(ordinal)})[:16]


def legacy_cue_id(script: str, index: int) -> str:
    """Deterministic identity for a cue restored from a pre-schema snapshot."""
    return digest({"script": script, "origin": "legacy", "index": int(index)})[:16]


def derived_cue_id(parents, origin: str, ordinal: int = 0) -> str:
    """A split/merge child. Never reuses a parent's ID."""
    if origin not in ("split", "merge"):
        raise SchemaError("derived cue ids are only for split or merge")
    return digest({"parents": [str(p) for p in parents], "origin": origin,
                   "ordinal": int(ordinal)})[:16]


def phrase_id(cue: str, order: int) -> str:
    """Identity of one phrase inside a cue. Stable across re-planning."""
    return f"{cue}:p{int(order)}"


def pause_id(cue: str, order: int) -> str:
    return f"{cue}:g{int(order)}"


def anchor_id(cue: str, phrase: str, edge: str) -> str:
    return f"{cue}:a{phrase.rsplit(':', 1)[-1]}{edge[:1]}"


def event_id(cue: str, ordinal: int = 0) -> str:
    """Identity of a nonverbal event: where the evidence was found, nothing else.

    Deliberately excludes the cue's text *and* the type it was classified as.
    Re-parsing the same subtitle must produce the same id, so a coverage
    decision made in review survives a rerun — and improving the parser, so
    that `[chuckles]` stops reading as `unknown` and starts reading as a laugh,
    must not mint a second event and orphan the decision on the first.
    """
    return digest({"cue": cue, "ordinal": int(ordinal)})[:16]


def take_id(fingerprint: str, attempt: int = 0) -> str:
    return digest({"generation": fingerprint, "attempt": int(attempt)})[:16]


def finding_id(cue: str, code: str, target: str = "") -> str:
    return digest({"cue": cue, "code": code, "target": target})[:16]


# --------------------------------------------------------------------------
# Lineage
# --------------------------------------------------------------------------

def retire(lineage: dict, retired: str, successors) -> None:
    """Record that `retired` no longer exists and what replaced it."""
    ids = [str(s) for s in successors if s]
    if not retired or retired in ids:
        return
    existing = lineage.setdefault(retired, [])
    for cue in ids:
        if cue not in existing:
            existing.append(cue)


def resolve_cue(segments, key, lineage: dict | None = None):
    """Find the cue an edit addresses, by cue ID or by legacy index string.

    A key naming a retired cue raises an actionable conflict: after a split or
    merge an old edit must never be redirected onto a different line.
    """
    key = str(key)
    for seg in segments:
        if getattr(seg, "cue_id", "") == key:
            return seg
    for seg in segments:
        if str(seg.index) == key:
            return seg
    for seg in segments:
        if str(seg.lineage.legacy_index) == key:
            return seg
    successors = (lineage or {}).get(key)
    if successors:
        raise SchemaError(
            f"line {key} was split or merged into {', '.join(successors)}; "
            "re-open the review and edit the current line instead")
    raise SchemaError(f"line edits refer to unknown cue {key}")


def ensure_identity(job) -> str:
    """Give the job and every cue a stable identity without disturbing existing ones.

    Called by the stages that create cues and again before persisting, so a cue
    built outside transcription (a test, a CLI caller, an older snapshot) still
    gets a deterministic ID scoped to the source document.
    """
    if not job.script_ref:
        job.script_ref = script_ref(job.input_file, job.source_lang, job.subtitle_file)
    script = job.script_ref
    for seg in job.segments:
        if not seg.cue_id:
            ordinal = seg.lineage.ordinal if seg.lineage.ordinal is not None else seg.index
            seg.cue_id = imported_cue_id(script, ordinal)
            seg.lineage = CueLineage(origin="import", script_ref=script, ordinal=ordinal,
                                     legacy_index=seg.index)
        if not seg.lineage.script_ref:
            seg.lineage.script_ref = script
        if not seg.source.spans and seg.source_start is None and seg.end > seg.start >= 0:
            # An imported subtitle cue's window IS its source interval until a
            # review edit or a montage moves the target placement.
            seg.source.spans = [Span(seg.start, seg.end, SOURCE)]
            seg.source.method = seg.source.method or "unknown"
        if seg.source.speaker is None:
            seg.source.speaker = seg.speaker
    return script


def split_cue(job, parent, children) -> None:
    """Mint child IDs for a split and retire the parent."""
    for position, child in enumerate(children):
        child.cue_id = derived_cue_id([parent.cue_id], "split", position)
        child.lineage = CueLineage(origin="split", script_ref=parent.lineage.script_ref,
                                   ordinal=parent.lineage.ordinal, parents=[parent.cue_id])
    retire(job.cue_lineage, parent.cue_id, [c.cue_id for c in children])


def merge_cues(job, parents, merged) -> None:
    """Mint a new ID for a merge and retire every parent."""
    ids = [p.cue_id for p in parents]
    first = parents[0]
    merged.cue_id = derived_cue_id(ids, "merge")
    merged.lineage = CueLineage(origin="merge", script_ref=first.lineage.script_ref,
                                ordinal=first.lineage.ordinal, parents=list(ids))
    # Flatten chains: a cue retired into one of these parents now points at the
    # merged cue, so resolution never stops on an intermediate that is also gone.
    for key, successors in job.cue_lineage.items():
        updated: list[str] = []
        for successor in successors:
            replacement = merged.cue_id if successor in ids else successor
            if replacement not in updated:
                updated.append(replacement)
        job.cue_lineage[key] = updated
    for retired in ids:
        retire(job.cue_lineage, retired, [merged.cue_id])


# --------------------------------------------------------------------------
# Codec
# --------------------------------------------------------------------------

def cue_payload(seg) -> dict:
    """Serialize one cue's typed records (identity, source, placement, audio)."""
    return {
        "cue_id": seg.cue_id,
        "lineage": seg.lineage.as_dict(),
        "source": seg.source.as_dict(),
        "placement": seg.placement.as_dict(),
        "audio": seg.audio.as_dict(),
        "preparation": seg.preparation.as_dict(),
        "intent": seg.intent.as_dict(),
        "measurement": seg.measurement.as_dict(),
        "level": seg.level.as_dict(),
        "verification": seg.verification.as_dict(),
        "phrasing": seg.phrasing.as_dict(),
        "findings": [f.as_dict() for f in seg.findings],
    }


def apply_cue_payload(seg, data: Any) -> None:
    """Restore one cue's typed records from `cue_payload` output."""
    data = _mapping(data, "cue")
    cue_id = _text(data.get("cue_id"))
    if not cue_id:
        raise SchemaError("a cue record needs a cue_id")
    seg.cue_id = cue_id
    seg.lineage = CueLineage.from_dict(data.get("lineage"))
    seg.source = SourceSpans.from_dict(data.get("source"))
    seg.placement = Placement.from_dict(data.get("placement"))
    seg.audio = CueAudio.from_dict(data.get("audio"))
    seg.preparation = SpeechPreparation.from_dict(data.get("preparation"))
    # A version-1 payload has none of these. Empty is their honest state, not a
    # migration failure: the run that wrote it never measured or directed
    # anything, and inventing a value here would fabricate evidence.
    seg.intent = PerformanceIntent.from_dict(data.get("intent"))
    seg.measurement = SourceMeasurement.from_dict(data.get("measurement"))
    seg.level = LevelDecision.from_dict(data.get("level"))
    seg.verification = Verification.from_dict(data.get("verification"))
    # A version-1 or version-2 payload has no timing plan. Empty is honest: that
    # run fitted whole clips, and inventing phrases for it would claim evidence
    # nobody gathered.
    seg.phrasing = TimingPlan.from_dict(data.get("phrasing"))
    seg.findings = [Finding.from_dict(f) for f in _sequence(data.get("findings"), "findings")]


def check_schema(version: Any, what: str = "script") -> int:
    """Accept this schema or older; reject a future one instead of guessing."""
    if version is None:
        return 0
    try:
        found = int(version)
    except (TypeError, ValueError) as exc:
        raise SchemaError(f"this {what} carries an unreadable cue schema version") from exc
    if found > CUE_SCHEMA_VERSION:
        raise SchemaError(
            f"this {what} needs cue schema {found}; this Doblarr understands "
            f"{CUE_SCHEMA_VERSION}. Upgrade Doblarr to read it.")
    return found


def register_unknown_clip(seg) -> None:
    """Record a clip that no stage claimed, as `unknown` rather than as raw.

    A cue can arrive with audio and no provenance — a pre-schema snapshot, or a
    job assembled outside the stages. The file is real and should be playable,
    but nothing proves whether it is the raw generation or something already
    normalized or time-fitted, so it is never labelled as either.
    """
    if seg.audio_clip and seg.audio.current() is None:
        seg.audio.put_render(Artifact(role=UNKNOWN, path=str(seg.audio_clip), proven=False))


def adopt_legacy(seg, script: str) -> None:
    """Give a pre-schema cue a deterministic identity and source span.

    Repeating this on the same snapshot always produces the same cue ID. A
    legacy `audio_clip` is registered with role `unknown` and `proven=False`: a
    stored clip may already be normalized or time-fitted, so it must never be
    presented as the raw generation.
    """
    seg.cue_id = legacy_cue_id(script, seg.index)
    seg.lineage = CueLineage(origin="legacy", script_ref=script, legacy_index=seg.index)
    start = seg.source_start if seg.source_start is not None else seg.start
    end = start + max(seg.end - seg.start, 0.0)
    if end > start and start >= 0:
        seg.source.spans = [Span(start, end, SOURCE)]
    seg.source.speaker = seg.speaker
    if seg.source_start is not None and seg.source_start != seg.start and seg.end > seg.start:
        seg.placement.montage = Span(seg.start, seg.end, MONTAGE)
    register_unknown_clip(seg)


def validate_plan(plan: TimingPlan, where: str = "cue") -> list[dict]:
    """Structural conflicts in a timing plan, as actionable findings.

    Returns the conflicts rather than raising: contradictory anchors are a
    review problem with a fix, not a corrupt record. A dangling reference is
    different — that is a malformed plan and it raises.
    """
    known = {p.phrase_id for p in plan.phrases}
    if len(known) != len(plan.phrases):
        raise SchemaError(f"{where} has two phrases with the same id")
    for anchor in plan.anchors:
        if anchor.phrase_id and anchor.phrase_id not in known:
            raise SchemaError(
                f"{where} anchors phrase {anchor.phrase_id}, which it does not have")
    for pause in plan.pauses:
        if pause.after and pause.after not in known:
            raise SchemaError(
                f"{where} has a pause after phrase {pause.after}, which it does not have")
    order = {p.phrase_id: p.order for p in plan.phrases}
    conflicts: list[dict] = []
    hard = sorted((a for a in plan.anchors if a.kind == "hard"),
                  key=lambda a: (order.get(a.phrase_id, 0), a.edge != "start"))
    for previous, current in zip(hard, hard[1:], strict=False):
        if current.at < previous.at:
            conflicts.append({
                "code": "anchor_order",
                "detail": (f"phrase {current.phrase_id} is anchored at "
                           f"{current.at:.3f}s, before {previous.phrase_id} at "
                           f"{previous.at:.3f}s"),
                "anchors": [previous.anchor_id, current.anchor_id]})
    if plan.slot:
        for anchor in hard:
            if anchor.at > plan.slot:
                conflicts.append({
                    "code": "anchor_outside_slot",
                    "detail": (f"phrase {anchor.phrase_id} is anchored at "
                               f"{anchor.at:.3f}s, past the {plan.slot:.3f}s window"),
                    "anchors": [anchor.anchor_id]})
    return conflicts


def validate_events(events, cues=None) -> None:
    """Fail loudly on duplicate event ids or a reference to a cue that is gone."""
    seen: set[str] = set()
    for event in events:
        if not event.event_id:
            raise SchemaError("a nonverbal event needs an id")
        if event.event_id in seen:
            raise SchemaError(f"duplicate nonverbal event id {event.event_id}")
        seen.add(event.event_id)
        if event.artifact is not None and event.coverage == "unresolved":
            raise SchemaError(
                f"event {event.event_id} has audio but no coverage decision")
        if cues is not None and event.cue_id and event.cue_id not in cues:
            # The cue a reaction was found on is often removed from synthesis —
            # that is the whole point. The reference is kept as provenance and
            # is not required to resolve.
            continue


def validate_cues(segments, lineage: dict | None = None) -> None:
    """Fail loudly on duplicate IDs, impossible times or dangling references."""
    seen: set[str] = set()
    for seg in segments:
        if not seg.cue_id:
            raise SchemaError(f"line {seg.index} has no cue id")
        if seg.cue_id in seen:
            raise SchemaError(f"duplicate cue id {seg.cue_id}")
        seen.add(seg.cue_id)
        if seg.cue_id in (lineage or {}):
            raise SchemaError(f"cue {seg.cue_id} is recorded as retired but still present")
        _finite(seg.start, f"line {seg.index} start")
        _finite(seg.end, f"line {seg.index} end")
        if seg.start < 0 or seg.end <= seg.start:
            raise SchemaError(f"line {seg.index} needs a positive time window")
        takes = [t.take_id for t in seg.audio.takes]
        if len(takes) != len(set(takes)):
            raise SchemaError(f"cue {seg.cue_id} has duplicate take ids")
        selection = seg.audio.selection
        if selection and selection.take_id and selection.take_id not in takes:
            raise SchemaError(
                f"cue {seg.cue_id} selects take {selection.take_id}, which it does not have")
        roles = [a.role for a in seg.audio.renders]
        if len(roles) != len(set(roles)):
            raise SchemaError(f"cue {seg.cue_id} has two artifacts for one role")
        if seg.audio.render(PHRASED) is not None and seg.audio.render(FITTED) is not None:
            # Two timing owners for one line means one of them rendered audio
            # the other then reprocessed. Exactly one owns a run.
            raise SchemaError(
                f"cue {seg.cue_id} has both a phrase-fitted and a whole-fitted render")
        validate_plan(seg.phrasing, f"cue {seg.cue_id}")
    for retired, successors in (lineage or {}).items():
        if retired in seen:
            raise SchemaError(f"cue {retired} is both live and retired")
        if not successors:
            raise SchemaError(f"retired cue {retired} has no successor")
