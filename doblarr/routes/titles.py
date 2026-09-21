"""Voice casts and per-title dubbing plans."""

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from ..clients.voicebox import VoiceboxError
from ..config import Config
from ..errors import ConfigError
from ..events import EventBus
from ..knowledge import portable
from ..knowledge import store as knowledge_store
from ..knowledge.models import Entry
from ..knowledge.resolver import locale_rank
from ..languages import base_language, normalize
from ..presets import effective_config
from ..recipes import (
    SETTING_KEYS,
    DubRecipe,
    RecipeCharacter,
    RecipeKnowledge,
    RecipeMedia,
    RecipeShow,
    RecipeVoice,
    media_matches,
)
from ..services import Services
from ..store import Database
from ..voices import CATEGORY_LABELS, cast_key


class CastEntryIn(BaseModel):
    speaker_id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    category: str
    voice: str = ""  # voicebox profile id; "" = unassigned
    previewed: bool = False
    engine: str = Field(default="", max_length=50)
    delivery: str = Field(default="", max_length=500)
    revision: int = Field(default=0, ge=0)

    @field_validator("category")
    @classmethod
    def _known_category(cls, v: str) -> str:
        if v not in CATEGORY_LABELS:
            raise ValueError(f"unknown category {v!r}")
        return v


class TitleIdentity(BaseModel):
    key: str | None = None
    path: str | None = None
    tmdb_id: int | None = None
    tvdb_id: int | None = None
    title: str | None = None

    def resolve(self) -> str:
        if self.key:
            return self.key
        if not (self.path or self.title or self.tmdb_id or self.tvdb_id):
            raise ConfigError("title lookup needs key / path / tmdb_id / tvdb_id / title")
        return cast_key(
            title=self.title, path=self.path, tmdb_id=self.tmdb_id, tvdb_id=self.tvdb_id
        )


class CastPutIn(TitleIdentity):
    title: str = ""
    cast: list[CastEntryIn]


class PlanPutIn(TitleIdentity):
    title: str = ""
    plan: dict[str, Any] = Field(default_factory=dict)


class RecipeExportIn(BaseModel):
    identity: TitleIdentity
    parent: TitleIdentity | None = None
    media: RecipeMedia
    source_language: str = ""
    target_language: str = "en"
    creator: str = Field(default="", max_length=100)
    revision: str = Field(default="1", max_length=40)
    notes: str = Field(default="", max_length=2000)
    schema_version: Literal[1, 2] = 1
    include_personal: list[str] = Field(default_factory=list, max_length=200)


class RecipeImportIn(BaseModel):
    identity: TitleIdentity
    media: RecipeMedia
    recipe: DubRecipe
    target_locale: str = ""  # the destination title's resolved target locale
    # Explicit local selections; an empty value means use local source audio.
    voices: dict[str, str] = Field(default_factory=dict, max_length=101)
    engines: dict[str, str] = Field(default_factory=dict, max_length=101)


def _overlay_entries(db, title_ref, show_refs, target, base):
    """This title's own show/episode local rules, applicable to the target locale."""
    pins = knowledge_store.active_installed_pins(db)
    overlay, personal = [], []
    installed_packs = set()
    for entry in knowledge_store.latest_entries(db, pins):
        if entry.suppresses or entry.status == "retired":
            continue
        if locale_rank(entry, target, base) is None:
            continue
        if entry.origin != "local":
            if entry.pack_id:
                installed_packs.add(entry.pack_id)
            continue
        own_rule = (
            entry.scope in {"episode", "movie"}
            and entry.scope_ref == title_ref
            or entry.scope == "show"
            and entry.scope_ref in show_refs
        )
        if own_rule:
            overlay.append(entry)
        elif entry.scope == "personal":
            personal.append(entry)
    # Pack dependencies pin the ACTIVE release of each applicable installed pack.
    packs = {
        row["pack_id"]: row["release"]
        for row in knowledge_store.pack_releases(db)
        if row["active"] and row["pack_id"] in installed_packs
    }
    return overlay, personal, packs


def build_router(config: Config, db: Database, bus: EventBus, services: Services) -> APIRouter:
    api = APIRouter()

    # -- voice casting ------------------------------------------------------
    @api.get("/api/voices")
    def list_voices():
        """Voice list: voicebox profiles when reachable, else config presets."""
        try:
            return {"source": "voicebox", "voices": services.voicebox.list_voices()}
        except VoiceboxError as exc:
            presets = config.get("dub", {}).get("preset_voices") or []
            return {
                "source": "config",
                "voices": [{"id": v, "name": v} for v in presets],
                "warning": str(exc),
            }

    @api.get("/api/cast")
    def get_cast(identity: Annotated[TitleIdentity, Depends()]):
        k = identity.resolve()
        saved = db.load_cast(k)
        return {
            "key": k,
            "title": (saved or {}).get("title", ""),
            "cast": (saved or {}).get("cast", []),
        }

    @api.put("/api/cast")
    def put_cast(body: CastPutIn):
        k = body.resolve()
        db.save_cast(k, body.title, [e.model_dump() for e in body.cast])
        bus.publish("cast", {"type": "updated", "key": k})
        return {"ok": True, "key": k, "saved": len(body.cast)}

    # -- per-title dub plans ------------------------------------------------
    @api.get("/api/plan")
    def get_plan(identity: Annotated[TitleIdentity, Depends()]):
        k = identity.resolve()
        saved = db.load_plan(k)
        return {
            "key": k,
            "title": (saved or {}).get("title", ""),
            "plan": (saved or {}).get("plan", {}),
        }

    @api.put("/api/plan")
    def put_plan(body: PlanPutIn):
        k = body.resolve()
        db.save_plan(k, body.title, body.plan)
        bus.publish("plan", {"type": "updated", "key": k})
        return {"ok": True, "key": k, "overrides": len(body.plan)}

    @api.get("/api/recipes/schema")
    def recipe_schema():
        return DubRecipe.model_json_schema()

    @api.post("/api/recipes/export")
    def export_recipe(body: RecipeExportIn):
        key = body.identity.resolve()
        plan = {}
        if body.parent:
            plan.update((db.load_plan(body.parent.resolve()) or {}).get("plan", {}))
        plan.update((db.load_plan(key) or {}).get("plan", {}))
        effective = effective_config(
            config.with_overrides({k: v for k, v in plan.items() if k != "target_lang"})
        ).as_dict()
        settings = {k: effective[k.split(".")[0]][k.split(".")[1]] for k in SETTING_KEYS}
        settings["dub.preset"] = "custom"
        cast = (db.load_cast(key) or {}).get("cast", [])
        # A saved profile is a reference only. Never include samples or profile payloads.
        try:
            names = {v["id"]: v["name"] for v in services.voicebox.list_voices()}
        except VoiceboxError:
            names = {}
        warnings = []

        def voice(profile, engine, delivery):
            if profile and profile not in names:
                warnings.append(
                    "An unavailable voice has no exported name. Choose a replacement on import."
                )
            return RecipeVoice(name=names.get(profile, ""), engine=engine, delivery=delivery)

        try:
            narrator = voice(
                effective["dub"].get("narrator_voice", ""),
                effective["voicebox"]["default_engine"],
                effective["dub"].get("narrator_delivery", ""),
            )
            characters = [
                RecipeCharacter(
                    speaker_id=c["speaker_id"],
                    label=c["label"],
                    category=c["category"],
                    voice=voice(
                        c.get("voice", ""),
                        c.get("engine") or narrator.engine,
                        c.get("delivery", ""),
                    ),
                )
                for c in cast
            ]
            target = normalize(plan.get("target_lang") or body.target_language) or "en"
            base = base_language(target)
            show_refs = {
                r
                for r in (
                    effective["dub"].get("cast_group", ""),
                    f"series:{body.media.tvdb_id}" if body.media.tvdb_id else "",
                )
                if r
            }
            overlay, personal, packs = _overlay_entries(db, key, show_refs, target, base)
            loss: list[str] = []
            personal_dependencies = []
            knowledge = None
            show = None
            target_locale = ""
            if body.schema_version == 2:
                promoted = set(body.include_personal)
                unknown = promoted - {e.id for e in personal}
                if unknown:
                    raise HTTPException(422, f"Unknown personal entries: {sorted(unknown)}")
                promoted_entries = [e for e in personal if e.id in promoted]
                overlay += [
                    Entry(**{**e.__dict__, "scope": body.media.kind, "scope_ref": key})
                    for e in promoted_entries
                ]
                personal_dependencies = [
                    {
                        "id": e.id,
                        "phrase": e.phrase,
                        "kind": e.kind,
                        "locale": e.locale,
                        "status": e.status,
                        "included": e.id in promoted,
                    }
                    for e in personal
                ]
                knowledge = RecipeKnowledge(
                    entries=[
                        portable.entry_to_overlay(e, knowledge_store.realizations_for(db, e.id))
                        for e in overlay
                    ],
                    pack_dependencies=packs,
                )
                if body.media.tvdb_id:
                    show = RecipeShow(
                        series_id=f"series:{body.media.tvdb_id}", title=body.media.title
                    )
                target_locale = target if target != base else ""
            else:
                if overlay:
                    loss.append(
                        f"{len(overlay)} knowledge overlay rule(s) are not included"
                        " in schema v1 recipes"
                    )
                if target != base:
                    loss.append(f"Target locale {target} is exported as {base} in schema v1")
            recipe = DubRecipe(
                format="doblarr-recipe",
                schema_version=body.schema_version,
                mode="recipe-only",
                media=body.media,
                source_language=body.source_language,
                target_language=base,
                settings=settings,
                narrator=narrator,
                characters=characters,
                creator=body.creator,
                revision=body.revision,
                notes=body.notes,
                target_locale=target_locale,
                show=show,
                knowledge=knowledge,
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {
            "recipe": recipe.model_dump(),
            "warnings": list(dict.fromkeys(warnings)),
            "loss": loss,
            "personal_dependencies": personal_dependencies,
        }

    def recipe_preview(body):
        if not media_matches(body.recipe.media, body.media):
            raise HTTPException(
                422,
                "Recipe belongs to a different movie or episode. Open the matching title first.",
            )
        warnings = ["Speaker numbers may differ. Review character assignments after an audition."]
        a, b = body.recipe.media.runtime_seconds, body.media.runtime_seconds
        if a is None or b is None:
            warnings.append(
                "Runtime has not been verified. Check the release and timing before generating."
            )
        elif abs(a - b) > 2:
            warnings.append("Runtime differs by over two seconds. Check the intended release.")
        knowledge_info = None
        if body.recipe.schema_version == 2:
            # A v2 overlay is locale-bound: never retarget it to another locale.
            current = normalize(body.target_locale or body.recipe.target_language) or ""
            expected = body.recipe.target_locale or body.recipe.target_language
            if current and current != expected:
                raise HTTPException(
                    422,
                    f"Recipe targets {expected}; this title targets {current}. "
                    "Knowledge overlays are never retargeted to another locale.",
                )
            knowledge_info = {"entries": [], "missing_packs": [], "engine_mismatches": []}
            default_engine = config["voicebox"].get("default_engine", "")
            for overlay in body.recipe.knowledge.entries if body.recipe.knowledge else []:
                present = knowledge_store.get_entry(db, overlay.id)
                knowledge_info["entries"].append(
                    {
                        "id": overlay.id,
                        "phrase": overlay.phrase,
                        "scope": overlay.scope,
                        "revision": overlay.revision,
                        "state": "already-present"
                        if present is not None and present.revision == overlay.revision
                        else "will-import",
                    }
                )
                for realization in overlay.realizations:
                    if default_engine and realization.engine != default_engine:
                        knowledge_info["engine_mismatches"].append(
                            f"'{overlay.phrase}' is realized for {realization.engine};"
                            f" the configured engine is {default_engine}"
                        )
            for pack_id, revision in (
                body.recipe.knowledge.pack_dependencies.items() if body.recipe.knowledge else []
            ):
                if not any(r["release"] == str(revision)
                           for r in knowledge_store.pack_releases(db, pack_id)):
                    knowledge_info["missing_packs"].append(
                        f"Pack '{pack_id}' (pinned release {revision}) is not installed;"
                        " install that release before importing"
                    )
            warnings += knowledge_info["missing_packs"] + knowledge_info["engine_mismatches"]
        try:
            voices = services.voicebox.list_voices()
        except VoiceboxError:
            voices = []
            warnings.append(
                "Voice service unavailable. Only local source-audio casting is available."
            )
        return {
            "recipe": body.recipe.model_dump(),
            "voices": voices,
            "warnings": warnings,
            "knowledge": knowledge_info,
        }

    @api.post("/api/recipes/preview")
    def preview_recipe(body: RecipeImportIn):
        return recipe_preview(body)

    @api.post("/api/recipes/import")
    def import_recipe(body: RecipeImportIn):
        preview = recipe_preview(body)
        if preview.get("knowledge") and preview["knowledge"]["missing_packs"]:
            raise HTTPException(422, "Install the recipe's exact pack releases before importing.")
        slots = {"narrator", *("character:" + c.speaker_id for c in body.recipe.characters)}
        if set(body.voices) != slots:
            raise HTTPException(422, "Choose a local voice or source audio for every role.")
        available = {v["id"] for v in preview["voices"]}
        if any(v and v not in available for v in body.voices.values()):
            raise HTTPException(
                422, "A selected voice is no longer available. Preview the recipe again."
            )
        references = {
            "narrator": body.recipe.narrator,
            **{"character:" + c.speaker_id: c.voice for c in body.recipe.characters},
        }
        engines = {}
        for slot, voice in references.items():
            engine = body.engines.get(slot, voice.engine)
            try:
                RecipeVoice.model_validate({"engine": engine, "delivery": voice.delivery})
            except ValueError as exc:
                raise HTTPException(422, f"Choose a compatible engine for {slot}.") from exc
            if not body.voices[slot] and engine in ("kokoro", "qwen_custom_voice"):
                raise HTTPException(
                    422, f"{slot} needs a preset voice, or a cloning engine such as Qwen."
                )
            engines[slot] = engine
        key = body.identity.resolve()
        plan = dict((db.load_plan(key) or {}).get("plan", {}))
        plan.update(body.recipe.settings)
        if body.recipe.schema_version == 2:
            plan["dub.target_locale"] = body.recipe.target_locale or body.recipe.target_language
            plan["knowledge.pack_releases"] = {
                k: str(v) for k, v in (body.recipe.knowledge.pack_dependencies.items()
                                      if body.recipe.knowledge else [])
            }
        plan.update(
            {
                "target_lang": body.recipe.target_language,
                "voicebox.default_engine": engines["narrator"],
                "dub.narrator_voice": body.voices["narrator"],
                "dub.narrator_delivery": body.recipe.narrator.delivery,
                "dub.voice_mode": "clone",
                "dub.preset_voices": [],
                "dub.cast_group": "",
                "dub.character_map": {},
                "dub.line_edits": {},
            }
        )
        cast = [
            dict(
                speaker_id=c.speaker_id,
                label=c.label,
                category=c.category,
                voice=body.voices["character:" + c.speaker_id],
                engine=engines["character:" + c.speaker_id],
                delivery=c.voice.delivery,
                previewed=False,
                revision=0,
            )
            for c in body.recipe.characters
        ]
        db.save_recipe(key, body.media.title, plan, cast)
        applied, skipped = 0, []
        if body.recipe.schema_version == 2 and body.recipe.knowledge:
            series_id = body.recipe.show.series_id if body.recipe.show else ""
            for overlay in body.recipe.knowledge.entries:
                # A show rule imported for a title with no series binds to the
                # title itself; nothing is silently dropped or retargeted elsewhere.
                if overlay.scope == "show" and series_id:
                    entry = portable.overlay_to_entry(overlay, scope_ref=series_id)
                elif overlay.scope == "show":
                    entry = portable.overlay_to_entry(
                        portable.RecipeEntry(**{**overlay.model_dump(), "scope": body.media.kind}),
                        scope_ref=key,
                    )
                else:
                    entry = portable.overlay_to_entry(overlay, scope_ref=key)
                try:
                    inserted = knowledge_store.insert_entry_version(db, entry)
                except ValueError:
                    inserted = False
                if inserted:
                    applied += 1
                else:
                    # A different rule already owns this pin; never overwritten.
                    skipped.append(overlay.phrase)
                for realization in portable.overlay_to_realizations(overlay):
                    try:
                        knowledge_store.insert_realization_version(db, realization)
                    except ValueError:
                        skipped.append(f"{overlay.phrase} (realization)")
        bus.publish("plan", {"type": "updated", "key": key})
        bus.publish("cast", {"type": "updated", "key": key})
        return {
            "ok": True,
            "key": key,
            "plan": plan,
            "characters": len(cast),
            "knowledge_applied": applied,
            "knowledge_skipped": skipped,
        }

    return api
