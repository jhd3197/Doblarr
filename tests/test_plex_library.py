"""Plex as a library source: only what the *arr sources miss, probed once per change."""

from types import SimpleNamespace

from doblarr import plex_library
from doblarr.clients.plex import PlexClient


def _leaf(key, file, updated=1):
    return {"ratingKey": str(key), "title": f"ep {key}", "parentIndex": 1, "index": key,
            "updatedAt": updated, "Media": [{"Part": [{"file": file}]}]}


class FakePlex:
    external_ids = staticmethod(PlexClient.external_ids)
    audio_languages = staticmethod(PlexClient.audio_languages)

    def __init__(self, shows, movies, langs):
        self.shows, self.movies, self.langs, self.probed = shows, movies, langs, []

    def sections(self):
        return [{"key": "1", "type": "movie", "title": "Movies"},
                {"key": "2", "type": "show", "title": "TV Shows"},
                {"key": "9", "type": "artist", "title": "Music"}]

    def section_items(self, key):
        return self.movies if key == "1" else self.shows if key == "2" else []

    def leaves(self, key):
        return next(s["_leaves"] for s in self.shows if s["ratingKey"] == key)

    def metadata(self, key):
        self.probed.append(key)
        return {"Media": [{"Part": [{"Stream": [
            {"streamType": 2, "languageCode": code} for code in self.langs[key]]}]}]}


def _plex():
    harbor = {"ratingKey": "4100", "title": "Harbor Lights", "year": 2007,
                 "thumb": "/library/metadata/4100/thumb/1",
                 "Guid": [{"id": "tvdb://81234"}, {"id": "tmdb://40404"}],
                 "_leaves": [_leaf(1, "M:/Shows/HarborLights/Season 01/e1.mkv"),
                             _leaf(2, "M:/Shows/HarborLights/Season 01/e2.mkv")]}
    in_sonarr = {"ratingKey": "100", "title": "Quiet Valley", "Guid": [{"id": "tvdb://70002"}],
                 "_leaves": [_leaf(5, "M:/Shows/QuietValley/e1.mkv")]}
    movie = {"ratingKey": "4200", "title": "Harbor Lights: The Movie", "year": 2007,
             "Guid": [{"id": "tmdb://50505"}], "updatedAt": 3,
             "Media": [{"Part": [{"file": "M:/Movies/Harbor Lights Movie/movie.mkv"}]}]}
    managed = {"ratingKey": "18000", "title": "In Radarr", "Guid": [{"id": "tmdb://42"}],
               "Media": [{"Part": [{"file": "M:/Movies/x.mkv"}]}]}
    youtube = {"ratingKey": "500", "title": "A video", "Guid": [],
               "Media": [{"Part": [{"file": "M:/YouTube/v.mp4"}]}]}
    # Matched by nothing but its folder, which an *arr already lists.
    by_path = {"ratingKey": "18001", "title": "Copied", "Guid": [{"id": "tmdb://7"}],
               "Media": [{"Part": [{"file": "M:/Movies/Known/known.mkv"}]}]}
    langs = {"1": ["jpn", "eng"], "2": ["jpn"], "5": ["jpn"], "4200": ["jpn", "spa"],
             "18000": ["eng"], "500": ["eng"], "18001": ["jpn"]}
    return FakePlex([harbor, in_sonarr], [movie, managed, youtube, by_path], langs)


def _scan(plex, tmp_path, **kw):
    return plex_library.scan_plex(
        plex, ["en", "es"], known_tvdb={70002}, known_tmdb={42},
        known_paths={"M:\\Movies\\Known"},
        cache=plex_library.AudioCache(tmp_path / "plex-audio.json"), **kw)


def test_plex_lists_only_what_the_arr_sources_miss(tmp_path):
    plex = _plex()
    items = {i.title: i for i in _scan(plex, tmp_path)}
    assert set(items) == {"Harbor Lights", "Harbor Lights: The Movie"}
    show = items["Harbor Lights"]
    assert show.tvdb_id == 81234 and show.media_type == "show" and show.original == "??"
    assert show.path.replace("\\", "/") == "M:/Shows/HarborLights"     # above the season folder
    assert show.status == "partial" and show.existing_audio == "en/ja (1/2)"
    assert show.poster == "/api/plex/thumb?path=%2Flibrary%2Fmetadata%2F4100%2Fthumb%2F1"
    movie = items["Harbor Lights: The Movie"]
    assert movie.tmdb_id == 50505 and movie.status == "available"


def test_unmatched_titles_need_an_explicit_opt_in(tmp_path):
    titles = {i.title for i in _scan(_plex(), tmp_path, include_unmatched=True)}
    assert "A video" in titles


def test_a_rescan_only_probes_new_or_changed_files(tmp_path):
    plex = _plex()
    _scan(plex, tmp_path)
    first = len(plex.probed)
    plex.probed.clear()
    _scan(plex, tmp_path)
    assert first and plex.probed == []
    plex.shows[0]["_leaves"][1]["updatedAt"] = 2      # one episode re-encoded
    _scan(plex, tmp_path)
    assert plex.probed == ["2"]


def test_a_show_only_plex_has_reads_like_a_sonarr_inventory(tmp_path):
    show, episodes, files = plex_library.show_inventory(
        _plex(), 81234, plex_library.AudioCache(tmp_path / "c.json"))
    assert show["title"] == "Harbor Lights" and show["source"] == "Plex"
    assert [(e["seasonNumber"], e["episodeNumber"]) for e in episodes] == [(1, 1), (1, 2)]
    assert files[0]["mediaInfo"]["audioLanguages"] == "jpn/eng"
    assert plex_library.show_inventory(_plex(), 1, None) is None


def test_the_plex_poster_route_serves_only_artwork(client_factory):
    client = client_factory()
    client.app.state.services._cache["plex"] = SimpleNamespace(
        image=lambda path: (b"jpeg", "image/jpeg"))
    ok = client.get("/api/plex/thumb", params={"path": "/library/metadata/1/thumb/2"})
    assert ok.status_code == 200 and ok.content == b"jpeg"
    for bad in ("/library/sections/1/all", "/library/metadata/../../:/prefs", "http://x"):
        assert client.get("/api/plex/thumb", params={"path": bad}).status_code == 422
