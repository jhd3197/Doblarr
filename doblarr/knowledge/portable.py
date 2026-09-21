"""Convert knowledge records to and from the schema v2 recipe overlay format."""

from __future__ import annotations

from ..recipes import RecipeEntry, RecipeRealization
from .models import Entry, Realization


def entry_to_overlay(entry: Entry, realizations: list[Realization]) -> RecipeEntry:
    return RecipeEntry(
        id=entry.id,
        revision=entry.revision,
        kind=entry.kind,  # type: ignore[arg-type]
        locale=entry.locale,
        coverage=list(entry.coverage),
        source_lang=entry.source_lang,
        source_form=entry.source_form,
        phrase=entry.phrase,
        sense=entry.sense,
        usage=entry.usage,
        examples=list(entry.examples),
        pronunciation=entry.pronunciation,
        ipa=entry.ipa,
        scope=entry.scope if entry.scope in ("episode", "movie", "show") else "episode",  # type: ignore[arg-type]
        status=entry.status,  # type: ignore[arg-type]
        license=entry.license,
        contributor=entry.contributor,
        review_history=list(entry.review_history),
        realizations=[
            RecipeRealization(
                id=r.id,
                revision=r.revision,
                engine=r.engine,
                model=r.model,
                voice=r.voice,
                replacement=r.replacement,
                evidence=r.evidence,
                status=r.status,  # type: ignore[arg-type]
            )
            for r in realizations
        ],
    )


def overlay_to_entry(overlay: RecipeEntry, *, scope_ref: str) -> Entry:
    """Bind an overlay rule to this install's title key / series id, always local."""
    return Entry(
        id=overlay.id,
        revision=overlay.revision,
        kind=overlay.kind,
        locale=overlay.locale,
        coverage=tuple(overlay.coverage),
        source_lang=overlay.source_lang,
        source_form=overlay.source_form,
        phrase=overlay.phrase,
        sense=overlay.sense,
        usage=overlay.usage,
        examples=tuple(overlay.examples),
        pronunciation=overlay.pronunciation,
        ipa=overlay.ipa,
        scope=overlay.scope,
        scope_ref=scope_ref,
        status=overlay.status,
        origin="local",
        license=overlay.license,
        contributor=overlay.contributor,
        review_history=tuple(overlay.review_history),
    )


def overlay_to_realizations(overlay: RecipeEntry) -> list[Realization]:
    return [
        Realization(
            id=r.id,
            revision=r.revision,
            entry_id=overlay.id,
            engine=r.engine,
            model=r.model,
            voice=r.voice,
            replacement=r.replacement,
            evidence=r.evidence,
            status=r.status,
            origin="local",
        )
        for r in overlay.realizations
    ]
