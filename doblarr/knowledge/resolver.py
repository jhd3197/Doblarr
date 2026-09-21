"""Resolve applicable knowledge into a frozen per-job selection.

Precedence (plan §5): explicit line correction/suppression > episode/movie
recipe > inherited show > personal > installed packs. Within a scope, an exact
regional rule beats a base-language rule; a specific model/voice realization
beats a deliberately broad one. `es-419` rules apply to `es-VE` only when the
rule explicitly declares that coverage — never by fallback inference. Two
equally applicable conflicting entries are surfaced in `conflicts` and BOTH
are skipped; load order never decides. A phrase with several active senses is
ambiguous, so it stays a suggestion (skipped) until all but one sense are
retired or rescoped.

Activation: retired and needs-retest records never apply. Personal local rules
apply immediately (proposed or reviewed); shared content (show/episode/movie
recipes, installed packs, any installed origin) requires reviewed status.

Legacy `dub.pronunciations` maps to personal-scope, base-applicable, all-engine
rules with visibly broad scope: they layer below every explicitly scoped entry
and are never reclassified as reviewed knowledge.
"""

from __future__ import annotations

import logging

from ..languages import base_language
from ..store import Database
from . import store as knowledge_store
from .matching import PhraseMatcher, boundary_policy, nfc
from .models import SCOPE_RANK, Entry, Realization

log = logging.getLogger("doblarr.knowledge")

_LOCAL_ACTIVE = {"proposed", "reviewed"}
_SHARED_ACTIVE = {"reviewed"}


def _entry_active(entry: Entry) -> bool:
    if entry.status in {"retired", "needs-retest"}:
        return False
    if entry.scope == "personal" and entry.origin == "local":
        return entry.status in _LOCAL_ACTIVE
    return entry.status in _SHARED_ACTIVE


def _realization_active(realization: Realization) -> bool:
    if realization.status in {"retired", "needs-retest"}:
        return False
    return realization.status in (
        _LOCAL_ACTIVE if realization.origin == "local" else _SHARED_ACTIVE
    )


def locale_rank(entry: Entry, locale: str, base: str) -> int | None:
    """0 = exact regional or declared coverage, 1 = base-applicable, None = inapplicable."""
    if entry.locale == locale or locale in entry.coverage:
        return 0
    if entry.locale == base:
        return 1
    return None


def _scope_applies(entry: Entry, title_ref: str, show_refs: frozenset) -> bool:
    if entry.scope == "line":
        return True  # filtered per line at spoken() time
    if entry.scope in {"episode", "movie"}:
        return bool(title_ref) and entry.scope_ref == title_ref
    if entry.scope == "show":
        return entry.scope_ref in show_refs
    return True  # personal, pack


def line_ref(title_ref: str, index: int) -> str:
    """scope_ref format for line-scoped corrections: "<title key>#<segment index>"."""
    return f"{title_ref}#{index}"


class KnowledgeSelection:
    """Frozen entries/revisions for one job; matchers compile once per engine context."""

    def __init__(
        self,
        *,
        entries: list[Entry],
        realizations: list[Realization],
        legacy: dict[str, str] | None = None,
        locale: str,
        title_ref: str = "",
        show_ref: str = "",
        show_refs: tuple[str, ...] = (),
        snapshot: dict | None = None,
    ):
        self.locale = locale
        self.base = base_language(locale)
        self.title_ref = title_ref
        self.show_ref = show_ref
        self.show_refs = frozenset(r for r in (show_ref, *show_refs) if r)
        self.legacy = {k: v for k, v in (legacy or {}).items() if k}
        self.snapshot = snapshot or {"version": 1, "entries": {}, "realizations": {}}
        self._entries = entries
        self._realizations: dict[str, list[Realization]] = {}
        for realization in realizations:
            self._realizations.setdefault(realization.entry_id, []).append(realization)
        self.conflicts: list[dict] = []
        self._conflict_keys: set[tuple] = set()
        self._boundary = boundary_policy(self.base)
        self._matchers: dict[tuple, tuple] = {}

    @classmethod
    def load(
        cls,
        db: Database,
        *,
        snapshot: dict | None = None,
        locale: str,
        title_ref: str = "",
        show_ref: str = "",
        show_refs: tuple[str, ...] = (),
        legacy: dict[str, str] | None = None,
    ) -> KnowledgeSelection:
        """Build a selection pinned to `snapshot` revisions (or the latest rules)."""
        if snapshot:
            entries = knowledge_store.entries_at(db, snapshot.get("entries", {}))
            realizations = knowledge_store.realizations_at(db, snapshot.get("realizations", {}))
        else:
            pins = knowledge_store.active_installed_pins(db)
            entries = knowledge_store.latest_entries(db, pins)
            realizations = knowledge_store.latest_realizations(db, pins)
            snapshot = knowledge_store.snapshot(db)
        return cls(
            entries=entries,
            realizations=realizations,
            legacy=legacy,
            locale=locale,
            title_ref=title_ref,
            show_ref=show_ref,
            show_refs=show_refs,
            snapshot=snapshot,
        )

    def line_ref_for(self, index: int) -> str:
        return line_ref(self.title_ref, index) if self.title_ref else ""

    def _conflict(self, phrase: str, ids: list[str], reason: str) -> None:
        key = (phrase, tuple(ids), reason)
        if key in self._conflict_keys:
            return
        self._conflict_keys.add(key)
        self.conflicts.append({"phrase": phrase, "entries": ids, "reason": reason})
        log.warning("knowledge: %s for %r (%s); skipping all candidates", reason, phrase, ids)

    def _winners(self, kind: str, current_line: str | None) -> dict[str, Entry]:
        """One winning entry per phrase after activation, locale, scope and suppression."""
        applicable: list[tuple[Entry, int]] = []
        suppressed: set[str] = set()
        for entry in self._entries:
            if entry.kind != kind or not _entry_active(entry):
                continue
            rank = locale_rank(entry, self.locale, self.base)
            if rank is None or not _scope_applies(entry, self.title_ref, self.show_refs):
                continue
            if entry.scope == "line" and entry.scope_ref != current_line:
                continue
            if entry.suppresses:
                suppressed.add(entry.suppresses)
                continue
            applicable.append((entry, rank))
        by_phrase: dict[str, list[tuple[Entry, int]]] = {}
        for entry, rank in applicable:
            if entry.id not in suppressed:
                by_phrase.setdefault(nfc(entry.phrase), []).append((entry, rank))
        winners: dict[str, Entry] = {}
        for phrase, candidates in by_phrase.items():
            candidates.sort(key=lambda c: (SCOPE_RANK[c[0].scope], c[1], c[0].id))
            best = (SCOPE_RANK[candidates[0][0].scope], candidates[0][1])
            tied = [e for e, r in candidates if (SCOPE_RANK[e.scope], r) == best]
            if len(tied) > 1:
                self._conflict(
                    phrase, sorted(e.id for e in tied), "equally applicable rules conflict"
                )
                continue
            winners[phrase] = tied[0]
        return winners

    def _pick_realization(
        self, entry: Entry, engine: str, model: str | None, voice: str | None
    ) -> Realization | None:
        candidates = [
            r
            for r in self._realizations.get(entry.id, [])
            if _realization_active(r)
            and r.engine == engine
            and (r.model is None or r.model == model)
            and (r.voice is None or r.voice == voice)
        ]
        if not candidates:
            return None  # no tested realization for this engine: stays unknown
        candidates.sort(key=lambda r: (-(r.model is not None) - (r.voice is not None), r.id))
        top = candidates[0]
        specificity = (top.model is not None, top.voice is not None)
        tied = [r for r in candidates if (r.model is not None, r.voice is not None) == specificity]
        if len({r.replacement for r in tied}) > 1:
            self._conflict(
                nfc(entry.phrase),
                sorted(r.id for r in tied),
                "equally specific realizations conflict",
            )
            return None
        return top

    def spoken(
        self,
        text: str,
        *,
        engine: str,
        model: str | None = None,
        voice: str | None = None,
        line: str = "",
    ) -> tuple[str, list[dict]]:
        """Resolve the TTS payload for one line on one engine/model/voice.

        Returns (spoken text, applied rule refs). Lines keep their own resolved
        payload, so an unrelated rule edit never changes another line's audio.
        """
        key = (engine, model, voice, line)
        matcher = self._matchers.get(key)
        if matcher is None:
            winners = self._winners("pronunciation", line or None)
            by_scope: dict[str, tuple[str, str]] = {}
            refs: dict[str, dict] = {}
            for phrase, entry in winners.items():
                realization = self._pick_realization(entry, engine, model, voice)
                if realization is None:
                    continue
                by_scope[phrase] = (realization.replacement, entry.scope)
                refs[phrase] = {
                    "entry": entry.id,
                    "revision": entry.revision,
                    "realization": realization.id,
                    "realization_revision": realization.revision,
                }
            # Overlay lowest precedence first: installed packs, then the legacy
            # dub.pronunciations dict (personal-scope broad rules), then every
            # explicitly scoped entry — a scoped entry always beats the legacy map.
            merged = {p: r for p, (r, scope) in by_scope.items() if scope == "pack"}
            merged.update(self.legacy)
            merged.update({p: r for p, (r, scope) in by_scope.items() if scope != "pack"})
            matcher = (PhraseMatcher(merged, self._boundary), refs)
            self._matchers[key] = matcher
        compiled, refs = matcher
        spoken_text, matched = compiled.apply(text)
        applied = [refs.get(term) or {"legacy": term} for term in dict.fromkeys(matched)]
        return spoken_text, applied

    def glossary_terms(self, texts: list[str]) -> dict[str, str]:
        """Resolved terminology relevant to these segments: source_form -> phrase.

        Only entries whose source wording appears in the segments are offered;
        several active senses for one source form stay suggestions (skipped).
        """
        winners = self._winners("term", None)
        haystack = "\n".join(texts)
        by_source: dict[str, set[str]] = {}
        for entry in winners.values():
            if entry.source_form and entry.source_form in haystack:
                by_source.setdefault(entry.source_form, set()).add(entry.phrase)
        terms = {}
        for source_form, phrases in by_source.items():
            if len(phrases) > 1:
                self._conflict(
                    source_form,
                    sorted(e.id for e in winners.values() if e.source_form == source_form),
                    "ambiguous senses remain suggestions",
                )
                continue
            terms[source_form] = next(iter(phrases))
        return terms
