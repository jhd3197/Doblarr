"""Typed knowledge records: pronunciation/terminology entries and realizations.

An entry describes intent in human terms (phrase, sense, usage, examples,
optional IPA). A realization is a separate linked record: a tested replacement
spelling for one engine/model (and optionally voice), with listening evidence.
Unknown model versions stay unknown — nullable fields, never guessed.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

KINDS = ("pronunciation", "term")
SCOPES = ("line", "episode", "movie", "show", "personal", "pack")
STATUSES = ("proposed", "reviewed", "needs-retest", "retired")
ORIGINS = ("local", "installed")

# Scope priority, highest first (plan §5): an explicit line correction or
# suppression, then the episode/movie recipe, inherited show rules, personal
# rules, and installed reviewed packs last.
SCOPE_RANK = {"line": 0, "episode": 1, "movie": 1, "show": 2, "personal": 3, "pack": 4}


@dataclass(frozen=True)
class Entry:
    """One knowledge rule. `id` is stable across edits; every edit is a new revision."""

    phrase: str
    kind: str = "pronunciation"  # pronunciation | term
    locale: str = ""  # target locale, e.g. "es-MX" (base "es" allowed)
    coverage: tuple[str, ...] = ()  # extra locales explicitly covered (es-419 -> es-VE)
    source_lang: str | None = None
    source_form: str = ""  # source wording a term translates (glossary guidance)
    sense: str = ""  # disambiguates multiple senses of one phrase
    usage: str = ""  # usage constraints / when not to use
    examples: tuple[str, ...] = ()
    pronunciation: str = ""  # human description of the intended sound
    ipa: str | None = None
    scope: str = "personal"
    scope_ref: str = ""  # title key, show group, "title#line", pack id
    suppresses: str = ""  # id of an inherited rule this record disables
    status: str = "proposed"  # proposed | reviewed | needs-retest | retired
    origin: str = "local"  # local | installed — never reclassify legacy data
    license: str = ""
    contributor: str = ""
    review_history: tuple[dict, ...] = ()
    pack_id: str | None = None  # set on installed pack content; NULL for local
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    revision: int = 1

    def validate(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"kind must be one of {KINDS}")
        if self.scope not in SCOPES:
            raise ValueError(f"scope must be one of {SCOPES}")
        if self.status not in STATUSES:
            raise ValueError(f"status must be one of {STATUSES}")
        if self.origin not in ORIGINS:
            raise ValueError(f"origin must be one of {ORIGINS}")
        if not self.phrase.strip() and not self.suppresses:
            raise ValueError("a rule needs a phrase (suppressions may omit it)")
        if self.scope in {"episode", "movie", "show", "line"} and not self.scope_ref:
            raise ValueError(f"scope {self.scope} needs a scope_ref")
        if self.revision < 1:
            raise ValueError("revision starts at 1")


@dataclass(frozen=True)
class Realization:
    """A tested replacement spelling for one engine (optionally model/voice).

    model=None means deliberately broad (engine-wide); it never claims an
    unknown model version. voice=None means any voice.
    """

    entry_id: str
    replacement: str
    engine: str
    model: str | None = None
    voice: str | None = None
    evidence: str = ""  # listening evidence note
    status: str = "proposed"
    origin: str = "local"
    pack_id: str | None = None  # set on installed pack content; NULL for local
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    revision: int = 1

    def validate(self) -> None:
        if not self.entry_id:
            raise ValueError("a realization links to an entry")
        if not self.engine:
            raise ValueError("a realization is tested on a particular engine")
        if not self.replacement:
            raise ValueError("a realization needs a replacement spelling")
        if self.status not in STATUSES:
            raise ValueError(f"status must be one of {STATUSES}")
        if self.origin not in ORIGINS:
            raise ValueError(f"origin must be one of {ORIGINS}")
        if self.revision < 1:
            raise ValueError("revision starts at 1")
