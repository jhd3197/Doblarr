"""Knowledge CRUD, search, coverage, and preview API (plan §8).

Local edits always land as origin="local"; pack-scope content only ever arrives
through pack imports (phase 4), never through these endpoints.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..knowledge import store as knowledge_store
from ..knowledge.models import Entry, Realization
from ..knowledge.resolver import KnowledgeSelection
from ..languages import display_name
from ..languages import parse as parse_language_tag

KIND = Literal["pronunciation", "term"]
SCOPE = Literal["line", "episode", "movie", "show", "personal", "pack"]
STATUS = Literal["proposed", "reviewed", "needs-retest", "retired"]

_WRITABLE_SCOPES = ("line", "episode", "movie", "show", "personal")


def _entry_dict(entry: Entry) -> dict:
    return {
        "id": entry.id,
        "revision": entry.revision,
        "kind": entry.kind,
        "locale": entry.locale,
        "coverage": list(entry.coverage),
        "source_lang": entry.source_lang,
        "source_form": entry.source_form,
        "phrase": entry.phrase,
        "sense": entry.sense,
        "usage": entry.usage,
        "examples": list(entry.examples),
        "pronunciation": entry.pronunciation,
        "ipa": entry.ipa,
        "scope": entry.scope,
        "scope_ref": entry.scope_ref,
        "suppresses": entry.suppresses,
        "status": entry.status,
        "origin": entry.origin,
        "license": entry.license,
        "contributor": entry.contributor,
        "review_history": list(entry.review_history),
    }


def _realization_dict(realization: Realization) -> dict:
    return {
        "id": realization.id,
        "entry_id": realization.entry_id,
        "revision": realization.revision,
        "engine": realization.engine,
        "model": realization.model,
        "voice": realization.voice,
        "replacement": realization.replacement,
        "evidence": realization.evidence,
        "status": realization.status,
        "origin": realization.origin,
    }


class EntryIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    phrase: str = Field(default="", max_length=300)
    kind: KIND
    locale: str = Field(min_length=2, max_length=20)
    coverage: list[str] = Field(default_factory=list, max_length=20)
    source_lang: str | None = None
    source_form: str = Field(default="", max_length=300)
    sense: str = Field(default="", max_length=300)
    usage: str = Field(default="", max_length=2000)
    examples: list[str] = Field(default_factory=list, max_length=20)
    pronunciation: str = Field(default="", max_length=2000)
    ipa: str | None = Field(default=None, max_length=300)
    scope: SCOPE = "personal"
    scope_ref: str = Field(default="", max_length=300)
    suppresses: str = Field(default="", max_length=100)
    status: STATUS = "proposed"
    license: str = Field(default="", max_length=300)
    contributor: str = Field(default="", max_length=300)

    @field_validator("locale")
    @classmethod
    def _canonical_locale(cls, value: str) -> str:
        parsed = parse_language_tag(value)
        if parsed is None:
            raise ValueError("locale must be a language tag such as es, es-MX or es-419")
        return parsed

    @field_validator("coverage")
    @classmethod
    def _canonical_coverage(cls, value: list[str]) -> list[str]:
        out = []
        for item in value:
            parsed = parse_language_tag(item)
            if parsed is None:
                raise ValueError(f"coverage entry is not a language tag: {item}")
            out.append(parsed)
        return out

    @model_validator(mode="after")
    def _scope_needs_ref(self):
        if self.scope in ("line", "episode", "movie", "show") and not self.scope_ref:
            raise ValueError(f"scope {self.scope} needs a scope_ref")
        if not self.phrase.strip() and not self.suppresses:
            raise ValueError("a rule needs a phrase (suppressions may omit it)")
        return self

    def to_entry(self, **pins) -> Entry:
        return Entry(
            phrase=self.phrase,
            kind=self.kind,
            locale=self.locale,
            coverage=tuple(self.coverage),
            source_lang=self.source_lang,
            source_form=self.source_form,
            sense=self.sense,
            usage=self.usage,
            examples=tuple(self.examples),
            pronunciation=self.pronunciation,
            ipa=self.ipa,
            scope=self.scope,
            scope_ref=self.scope_ref,
            suppresses=self.suppresses,
            status=self.status,
            origin="local",
            license=self.license,
            contributor=self.contributor,
            **pins,
        )


class RealizationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entry_id: str = Field(min_length=1, max_length=100)
    engine: str = Field(min_length=1, max_length=100)
    model: str | None = Field(default=None, max_length=200)
    voice: str | None = Field(default=None, max_length=300)
    replacement: str = Field(min_length=1, max_length=300)
    evidence: str = Field(default="", max_length=2000)
    status: STATUS = "proposed"

    def to_realization(self, **pins) -> Realization:
        return Realization(
            entry_id=self.entry_id,
            engine=self.engine,
            model=self.model,
            voice=self.voice,
            replacement=self.replacement,
            evidence=self.evidence,
            status=self.status,
            origin="local",
            **pins,
        )


class PreviewIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entry: EntryIn
    realization: RealizationIn | None = None
    text: str = Field(min_length=1, max_length=2000)
    engine: str = Field(default="", max_length=100)
    model: str | None = None
    voice: str | None = None
    title_ref: str = Field(default="", max_length=300)
    show_ref: str = Field(default="", max_length=300)
    line_ref: str = Field(default="", max_length=300)


def build_router(config, services, db) -> APIRouter:
    api = APIRouter()

    @api.get("/api/knowledge/coverage")
    def coverage():
        counts = knowledge_store.coverage_counts(db)
        # The validation targets always appear, even with zero content.
        locales = ["en", "es-MX", "es-VE"]
        locales += sorted(loc for loc in counts if loc not in locales)
        return {
            "locales": [
                {
                    "locale": locale,
                    "name": display_name(locale),
                    **{
                        "entries": 0,
                        "reviewed": 0,
                        "proposed": 0,
                        "needs-retest": 0,
                        **counts.get(locale, {}),
                    },
                }
                for locale in locales
            ]
        }

    @api.get("/api/knowledge/entries")
    def list_entries(
        locale: str | None = None,
        kind: KIND | None = None,
        scope: SCOPE | None = None,
        status: STATUS | None = None,
        q: str | None = Query(default=None, max_length=200),
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=25, ge=1, le=100),
    ):
        entries, total = knowledge_store.search_entries(
            db,
            locale=locale,
            kind=kind,
            scope=scope,
            status=status,
            q=q,
            page=page,
            page_size=page_size,
        )
        return {
            "entries": [_entry_dict(e) for e in entries],
            "total": total,
            "page": page,
            "page_size": page_size,
        }

    @api.get("/api/knowledge/entries/{entry_id}")
    def entry_detail(entry_id: str):
        entry = knowledge_store.get_entry(db, entry_id)
        if entry is None:
            raise HTTPException(404, "No knowledge entry with this id")
        return {
            "entry": _entry_dict(entry),
            "realizations": [
                _realization_dict(r) for r in knowledge_store.realizations_for(db, entry_id)
            ],
            "suppressed_by": [_entry_dict(e) for e in knowledge_store.suppressed_by(db, entry_id)],
        }

    @api.post("/api/knowledge/entries")
    def create_entry(body: EntryIn):
        if body.scope == "pack":
            raise HTTPException(422, "Pack content only arrives through pack imports")
        entry = knowledge_store.save_entry(db, body.to_entry())
        return {"entry": _entry_dict(entry)}

    @api.put("/api/knowledge/entries/{entry_id}")
    def update_entry(entry_id: str, body: EntryIn):
        if knowledge_store.get_entry(db, entry_id) is None:
            raise HTTPException(404, "No knowledge entry with this id")
        if body.scope == "pack":
            raise HTTPException(422, "Pack content only arrives through pack imports")
        entry = knowledge_store.save_entry(db, body.to_entry(id=entry_id))
        return {"entry": _entry_dict(entry)}

    @api.post("/api/knowledge/entries/{entry_id}/retire")
    def retire_entry(entry_id: str):
        entry = knowledge_store.get_entry(db, entry_id)
        if entry is None:
            raise HTTPException(404, "No knowledge entry with this id")
        retired = knowledge_store.save_entry(
            db, Entry(**{**entry.__dict__, "status": "retired", "revision": 1})
        )
        return {"entry": _entry_dict(retired)}

    @api.post("/api/knowledge/realizations")
    def create_realization(body: RealizationIn):
        if knowledge_store.get_entry(db, body.entry_id) is None:
            raise HTTPException(422, "Realization links to an unknown entry")
        realization = knowledge_store.save_realization(db, body.to_realization())
        return {"realization": _realization_dict(realization)}

    @api.put("/api/knowledge/realizations/{realization_id}")
    def update_realization(realization_id: str, body: RealizationIn):
        if knowledge_store.get_realization(db, realization_id) is None:
            raise HTTPException(404, "No realization with this id")
        realization = knowledge_store.save_realization(db, body.to_realization(id=realization_id))
        return {"realization": _realization_dict(realization)}

    @api.post("/api/knowledge/preview")
    def preview(body: PreviewIn):
        """Resolve a sample sentence with the draft rule added, before anything is saved."""
        draft = body.entry.to_entry(id="draft-preview")
        entries = knowledge_store.latest_entries(db) + [draft]
        realizations = knowledge_store.latest_realizations(db)
        if body.realization is not None:
            draft_realization = RealizationIn(
                **{**body.realization.model_dump(), "entry_id": draft.id}
            )
            realizations.append(draft_realization.to_realization(id="draft-realization"))
        selection = KnowledgeSelection(
            entries=entries,
            realizations=realizations,
            locale=draft.locale,
            title_ref=body.title_ref,
            show_ref=body.show_ref,
        )
        after, applied = selection.spoken(
            body.text,
            engine=body.engine,
            model=body.model,
            voice=body.voice,
            line=body.line_ref,
        )
        return {
            "before": body.text,
            "after": after,
            "applied": applied,
            "conflicts": selection.conflicts,
        }

    return api
