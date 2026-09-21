"""Local memory management; no implicit export or public upload."""

from fastapi import APIRouter, HTTPException, Query

from ..knowledge.memory import MemoryEntry, save


def build_router(db):
    api = APIRouter()

    @api.get("/api/memory")
    def list_memory(page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100)):
        latest = (" FROM translation_memory m WHERE NOT EXISTS"
                  " (SELECT 1 FROM translation_memory n WHERE n.id=m.id AND n.seq>m.seq)")
        total = db.query_one("SELECT COUNT(*) AS n" + latest)["n"]
        rows = db.query("SELECT document" + latest + " ORDER BY seq DESC LIMIT ? OFFSET ?",
                        (page_size, (page - 1) * page_size))
        return {"entries": [MemoryEntry.model_validate_json(r["document"]).model_dump()
                            for r in rows], "total": total}

    @api.post("/api/memory")
    def save_memory(body: MemoryEntry):
        try:
            return {"entry": save(db, body).model_dump()}
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @api.post("/api/memory/{entry_id}/retire")
    def retire_memory(entry_id: str):
        row = db.query_one("SELECT document FROM translation_memory WHERE id=?"
                           " ORDER BY seq DESC LIMIT 1", (entry_id,))
        if not row:
            raise HTTPException(404, "memory entry not found")
        entry = MemoryEntry.model_validate_json(row["document"])
        return {"entry": save(db, entry.model_copy(update={"status": "retired"})).model_dump()}

    return api
