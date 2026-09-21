"""Knowledge pack management: install, update, rollback, download, contributions."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from ..knowledge import packs as pack_mod
from ..knowledge import store as knowledge_store


class InstallIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=1000)


class DownloadIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pack_id: str = Field(min_length=1, max_length=100)


class ContributionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entry_ids: list[str] = Field(min_length=1, max_length=200)
    author: str = Field(default="", max_length=200)
    license: str = Field(default="CC0-1.0", max_length=100)
    notes: str = Field(default="", max_length=2000)


def build_router(config, services, db) -> APIRouter:
    api = APIRouter()

    @api.get("/api/packs")
    def list_packs():
        releases = knowledge_store.pack_releases(db)
        grouped: dict[str, dict] = {}
        for row in releases:
            manifest = json.loads(row["manifest"])
            pack = grouped.setdefault(
                row["pack_id"],
                {
                    "pack_id": row["pack_id"],
                    "name": row["name"],
                    "source": row["source"],
                    "third_party": row["source"] != pack_mod.OFFICIAL,
                    "distribution": row["distribution"],
                    "releases": [],
                },
            )
            pack["releases"].append(
                {
                    "release": row["release"],
                    "active": bool(row["active"]),
                    "installed_at": row["installed_at"],
                    "content_hash": row["content_hash"][:12],
                    "coverage": manifest.get("coverage", {}),
                }
            )
        return {"packs": sorted(grouped.values(), key=lambda p: p["pack_id"])}

    @api.post("/api/packs/install")
    def install(body: InstallIn):
        path = Path(body.path)
        try:
            bundled = pack_mod.starter_pack_path()
            official = path.resolve() == bundled.resolve()
            result = pack_mod.install_pack(
                db,
                path,
                source=pack_mod.OFFICIAL if official else pack_mod.THIRD_PARTY,
            )
        except pack_mod.PackError as exc:
            raise HTTPException(422, str(exc)) from exc
        return result

    @api.post("/api/packs/download")
    def download(body: DownloadIn):
        knowledge_cfg = config.get("knowledge", {})
        cache = knowledge_cfg.get("pack_cache_dir") or (config.work_dir / "packs")
        try:
            path = pack_mod.download_pack(db, knowledge_cfg, body.pack_id, Path(cache))
            result = pack_mod.install_pack(db, path, source=pack_mod.OFFICIAL)
        except pack_mod.PackError as exc:
            raise HTTPException(422, str(exc)) from exc
        return result

    @api.post("/api/packs/{pack_id}/rollback")
    def rollback(pack_id: str):
        try:
            return pack_mod.rollback_pack(db, pack_id)
        except pack_mod.PackError as exc:
            raise HTTPException(422, str(exc)) from exc

    @api.post("/api/packs/contribution")
    def contribution(body: ContributionIn):
        try:
            bundle, warnings = pack_mod.contribution_bundle(
                db, body.entry_ids,
                author=body.author, license=body.license, notes=body.notes,
            )
        except pack_mod.PackError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"bundle": bundle, "warnings": warnings}

    return api
