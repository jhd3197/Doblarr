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
CUE_SCHEMA_VERSION = 1

# Time domains. Never mix them in one number.
SOURCE = "source"    # the original media timeline
TARGET = "target"    # the dubbed timeline the mix places clips on
CLIP = "clip"        # offsets inside one generated clip
MONTAGE = "montage"  # an audition montage's own concatenated timeline
DOMAINS = (SOURCE, TARGET, CLIP, MONTAGE)

# Artifact roles in processing order. "unknown" is a real state, used when a
# migrated file's provenance cannot be proven.
RAW = "raw"
NORMALIZED = "normalized"
FITTED = "fitted"
UNKNOWN = "unknown"
ROLES = (RAW, NORMALIZED, FITTED, UNKNOWN)
# Later plans append their own role here; order defines "most processed last".
RENDER_ORDER = (RAW, NORMALIZED, FITTED)

ORIGINS = ("import", "legacy", "split", "merge", "manual")
DISPOSITIONS = ("open", "accepted", "fixed", "obsolete")
FINDING_KINDS = ("technical", "content", "timing", "performance", "delivery")


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
        )


@dataclass
class Selection:
    """Which take is rendered, and why. Resume must not silently pick another."""

    take_id: str = ""
    reason: str = "auto"      # auto | review | migrated
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

    def put_render(self, artifact: Artifact) -> Artifact:
        """Register (or replace) the derivative for `artifact.role`."""
        self.renders = [a for a in self.renders if a.role != artifact.role]
        self.renders.append(artifact)
        self.renders.sort(key=lambda a: RENDER_ORDER.index(a.role)
                          if a.role in RENDER_ORDER else len(RENDER_ORDER))
        return artifact

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
    for retired, successors in (lineage or {}).items():
        if retired in seen:
            raise SchemaError(f"cue {retired} is both live and retired")
        if not successors:
            raise SchemaError(f"retired cue {retired} has no successor")
