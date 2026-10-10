"""Anime News Network's encyclopedia: the cast in every language it lists.

ANN credits each dub separately (`<cast lang="EN">`, `lang="ES"`, ...), which
makes it the best keyless source for who voices a character in a dub. Its
public XML API asks for at most one request per second per address; answers
are cached. ANN does not rank characters, so its cast carries no role.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

from . import base
from .base import Fetch, Unreachable

API = "https://cdn.animenewsnetwork.com/encyclopedia/api.xml"
PAGE = "https://www.animenewsnetwork.com/encyclopedia/anime.php?id={id}"
_ID = re.compile(r"animenewsnetwork\.com/encyclopedia/anime\.php\?id=(\d+)", re.IGNORECASE)
_FORMATS = {"TV": "TV", "movie": "MOVIE", "OAV": "OVA", "ONA": "ONA", "special": "SPECIAL"}
BATCH = 50


def _parse(text: str) -> ET.Element:
    try:
        return ET.fromstring(text)
    except ET.ParseError as exc:
        raise Unreachable("ANN answered something that is not XML") from exc


def _infos(node: ET.Element, kind: str) -> list[ET.Element]:
    return [i for i in node.findall("info") if i.get("type") == kind]


def _summary(node: ET.Element) -> dict:
    """An `<anime>` element as a search hit."""
    titles: dict[str, str] = {}
    for info in _infos(node, "Main title") + _infos(node, "Alternative title"):
        lang = (info.get("lang") or "").upper()
        if info.text and lang and lang not in titles:
            titles[lang] = info.text.strip()
    vintage = next((i.text for i in _infos(node, "Vintage") if i.text), "")
    episodes = next((i.text for i in _infos(node, "Number of episodes") if i.text), None)
    cid = base.int_or_none(node.get("id"))
    return base.hit(source="ann", cid=cid, title=node.get("name") or "", url=PAGE.format(id=cid),
                    titles=titles, year=base.year_of(vintage),
                    fmt=_FORMATS.get(node.get("type") or "", node.get("type")),
                    episodes=base.int_or_none(episodes))


def _cast(node: ET.Element, max_characters: int) -> tuple[list[dict], bool]:
    """Characters in credit order, each with every language's voice."""
    order: list[str] = []
    people: dict[str, dict] = {}
    for credit in node.findall("cast"):
        role = (credit.findtext("role") or "").strip()
        person = credit.find("person")
        if not role or person is None or not (person.text or "").strip():
            continue
        key = base.name_key(role)
        if key not in people:
            order.append(key)
            people[key] = base.character(cid=None, name=role, url=None)
        elif role != people[key]["name"] and role not in people[key]["alternative"]:
            people[key]["alternative"].append(role)
        lang = (credit.get("lang") or "").upper()
        people[key]["voice_actors"].append(
            {**base.voice(person.text or "", base.LANGUAGES.get(lang, lang)),
             "id": base.int_or_none(person.get("id"))})
    # "Anne" in one dub and "Anne Lapin" in another are one character when the
    # short name fits exactly one full name.
    for key in list(order):
        if " " in key:
            continue
        fuller = [k for k in order if k != key and key in k.split()]
        if len(fuller) == 1:
            into = people[fuller[0]]
            short = people.pop(key)
            order.remove(key)
            into["alternative"] = [*into["alternative"], short["name"], *short["alternative"]]
            into["voice_actors"] += [v for v in short["voice_actors"]
                                     if v not in into["voice_actors"]]
    cast = [people[k] for k in order]
    return cast[:max_characters], len(cast) <= max_characters


class ANN:
    name = "ann"
    label = "Anime News Network"
    host = "animenewsnetwork.com"

    def __init__(self, fetch: Fetch | None = None):
        self._fetch = fetch

    def _get(self, params: dict) -> ET.Element:
        text = (self._fetch or base.http_get)(API, params=params, interval=1.0)
        return _parse(text)

    def can_handle(self, url: str) -> bool:
        return bool(_ID.search(str(url or "")))

    def search(self, query: str, *, max_results: int = 8) -> list[dict]:
        query = " ".join(str(query or "").split())

        def produce() -> str:
            return ET.tostring(self._get({"title": f"~{query}"}), encoding="unicode")

        root = _parse(base.cached(f"ann:search:{query.casefold()}", produce))
        return [_summary(a) for a in root.findall("anime")][:max_results]

    def entry(self, ann_id: int, *, fresh: bool = False) -> ET.Element:
        text = base.cached(f"ann:anime:{int(ann_id)}", lambda: ET.tostring(
            self._get({"anime": int(ann_id)}), encoding="unicode"), fresh=fresh)
        node = _parse(text).find("anime")
        if node is None:
            raise LookupError(f"ANN has no anime {ann_id}")
        return node

    def entries(self, ids: list[int]) -> dict[int, ET.Element]:
        """Several entries, up to fifty per request (the API's batch form)."""
        out: dict[int, ET.Element] = {}
        for start in range(0, len(ids), BATCH):
            chunk = [int(i) for i in ids[start:start + BATCH]]
            root = self._get({"anime": "/".join(str(i) for i in chunk)})
            for node in root.findall("anime"):
                aid = base.int_or_none(node.get("id"))
                if aid is not None:
                    out[aid] = node
        return out

    def read(self, url: str, *, max_characters: int = 100, fresh: bool = False) -> dict:
        found = _ID.search(str(url or ""))
        if not found:
            raise ValueError(f"{url} is not an ANN encyclopedia anime page")
        node = self.entry(int(found.group(1)), fresh=fresh)
        summary = _summary(node)
        characters, complete = _cast(node, max_characters)
        staff = [{"task": (s.findtext("task") or "").strip(),
                  "name": (s.findtext("person") or "").strip()}
                 for s in node.findall("staff") if s.findtext("person")]
        episode_titles = {}
        for ep in node.findall("episode"):
            title = next((t.text for t in ep.findall("title")
                          if (t.get("lang") or "").upper() == "EN" and t.text), None)
            number = base.int_or_none(ep.get("num"))
            if title and number is not None:
                episode_titles[str(number)] = title.strip()
        return {"source_id": summary["id"], "url": summary["url"], "title": summary["title"],
                "titles": summary["titles"], "format": summary["format"],
                "year": summary["year"], "episodes": summary["episodes"],
                "characters": characters, "complete": complete, "staff": staff,
                "episode_titles": episode_titles}
