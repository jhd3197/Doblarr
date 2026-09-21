"""Read-only catalog API: the canonical language identities every feature shares."""

from fastapi import APIRouter

from .. import languages


def build_router() -> APIRouter:
    api = APIRouter()

    @api.get("/api/languages")
    def list_languages():
        return {
            "languages": [
                {
                    "id": e.id,
                    "name": e.name,
                    "native_name": e.native_name,
                    "base": e.base,
                    "script": e.script,
                    "region": e.region,
                    "supported": e.supported,
                }
                for e in languages.catalog()
            ]
        }

    return api
