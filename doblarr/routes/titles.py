"""Voice casts and per-title dubbing plans."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, field_validator

from ..clients.voicebox import VoiceboxError
from ..config import Config
from ..errors import ConfigError
from ..events import EventBus
from ..services import Services
from ..store import Database
from ..voices import CATEGORY_LABELS, cast_key


class CastEntryIn(BaseModel):
    speaker_id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    category: str
    voice: str = ""            # voicebox profile id; "" = unassigned
    previewed: bool = False
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
        return cast_key(title=self.title, path=self.path,
                        tmdb_id=self.tmdb_id, tvdb_id=self.tvdb_id)


class CastPutIn(TitleIdentity):
    title: str = ""
    cast: list[CastEntryIn]


class PlanPutIn(TitleIdentity):
    title: str = ""
    plan: dict[str, Any] = Field(default_factory=dict)


def build_router(config: Config, db: Database, bus: EventBus,
                 services: Services) -> APIRouter:
    api = APIRouter()

    # -- voice casting ------------------------------------------------------
    @api.get("/api/voices")
    def list_voices():
        """Voice list: voicebox profiles when reachable, else config presets."""
        try:
            return {"source": "voicebox", "voices": services.voicebox.list_voices()}
        except VoiceboxError as exc:
            presets = config.get("dub", {}).get("preset_voices") or []
            return {"source": "config",
                    "voices": [{"id": v, "name": v} for v in presets],
                    "warning": str(exc)}

    @api.get("/api/cast")
    def get_cast(identity: Annotated[TitleIdentity, Depends()]):
        k = identity.resolve()
        saved = db.load_cast(k)
        return {"key": k, "title": (saved or {}).get("title", ""),
                "cast": (saved or {}).get("cast", [])}

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
        return {"key": k, "title": (saved or {}).get("title", ""),
                "plan": (saved or {}).get("plan", {})}

    @api.put("/api/plan")
    def put_plan(body: PlanPutIn):
        k = body.resolve()
        db.save_plan(k, body.title, body.plan)
        bus.publish("plan", {"type": "updated", "key": k})
        return {"ok": True, "key": k, "overrides": len(body.plan)}

    return api
