"""Minimal Plex client — enough to find items and manage labels.

Labels are added/removed non-destructively: `label[0].tag.tag=X` adds X while
keeping every existing label (Kometa's included), and `label[].tag.tag-=X` removes
just X. `label.locked=1` keeps the label through metadata refreshes.
"""

from __future__ import annotations

import requests

TYPE_NUM = {"movie": 1, "show": 2}


class PlexError(RuntimeError):
    pass


class PlexClient:
    def __init__(self, base_url: str, token: str, timeout: int = 30):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _get(self, path: str, params: dict | None = None) -> dict:
        p = {"X-Plex-Token": self.token}
        if params:
            p.update(params)
        try:
            r = requests.get(f"{self.base_url}{path}", params=p,
                             headers={"Accept": "application/json"}, timeout=self.timeout)
        except requests.RequestException as exc:
            raise PlexError(f"Plex unreachable at {self.base_url}: {exc}")
        if r.status_code == 401:
            raise PlexError("Plex rejected the token (401)")
        if not r.ok:
            raise PlexError(f"Plex {r.status_code} on {path}: {r.text[:200]}")
        return r.json()

    def _put(self, path: str, params: dict) -> None:
        p = {"X-Plex-Token": self.token}
        p.update(params)
        try:
            r = requests.put(f"{self.base_url}{path}", params=p, timeout=self.timeout)
        except requests.RequestException as exc:
            raise PlexError(f"Plex unreachable at {self.base_url}: {exc}")
        if not r.ok:
            raise PlexError(f"Plex {r.status_code} on PUT {path}: {r.text[:200]}")

    def sections(self) -> list[dict]:
        data = self._get("/library/sections")
        return [{"key": s["key"], "type": s["type"], "title": s["title"]}
                for s in data["MediaContainer"].get("Directory", [])]

    @staticmethod
    def _meta_labels(m: dict) -> list[str]:
        return [l["tag"] for l in m.get("Label", [])]

    def find(self, section_key: str, type_num: int, title: str,
             year: int | None) -> dict | None:
        """Best match by exact (case-insensitive) title, preferring the right year."""
        data = self._get(f"/library/sections/{section_key}/all",
                         {"type": type_num, "title": title})
        cands = data["MediaContainer"].get("Metadata", [])
        exact = [m for m in cands if m.get("title", "").strip().lower() == title.strip().lower()]
        pool = exact or cands
        pick = None
        if year is not None:
            pick = next((m for m in pool if m.get("year") == year), None)
        pick = pick or (pool[0] if pool else None)
        if not pick:
            return None
        return {"ratingKey": pick["ratingKey"], "title": pick.get("title"),
                "year": pick.get("year"), "labels": self._meta_labels(pick)}

    def items_with_label(self, section_key: str, type_num: int, label: str) -> list[dict]:
        data = self._get(f"/library/sections/{section_key}/all",
                         {"type": type_num, "label": label})
        return [{"ratingKey": m["ratingKey"], "title": m.get("title"),
                 "year": m.get("year")} for m in data["MediaContainer"].get("Metadata", [])]

    def add_label(self, section_key: str, type_num: int, rating_key: str, label: str) -> None:
        self._put(f"/library/sections/{section_key}/all", {
            "type": type_num, "id": rating_key,
            "label[0].tag.tag": label, "label.locked": 1,
        })

    def remove_label(self, section_key: str, type_num: int, rating_key: str, label: str) -> None:
        self._put(f"/library/sections/{section_key}/all", {
            "type": type_num, "id": rating_key,
            "label[].tag.tag-": label, "label.locked": 1,
        })
