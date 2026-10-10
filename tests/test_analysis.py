"""Episode analysis: voices grouped locally, lines measured, names that carry over."""

import json
import math
import struct
import wave
from types import SimpleNamespace

import pytest

from doblarr import analysis, speakers


def _unit(*values):
    norm = math.sqrt(sum(v * v for v in values))
    return [v / norm for v in values]


def test_lines_in_one_voice_group_together_and_the_longest_voice_comes_first():
    np = pytest.importorskip("numpy")
    a, b = _unit(1, 0.05, 0), _unit(0.05, 1, 0)
    vectors = [np.array(v) for v in (a, b, a, a, b)] + [None]
    durations = [2.0, 3.0, 2.5, 1.5, 4.0, 0.2]
    labels = speakers.cluster(vectors, durations)
    assert labels[0] == labels[2] == labels[3]
    assert labels[1] == labels[4] != labels[0]
    assert labels[4] == "SPEAKER_00"         # B is heard for 7 s, A for 6 s
    assert labels[5] == labels[4]            # no voice at all: the previous line's speaker


def test_a_short_line_joins_the_nearest_voice_instead_of_starting_its_own():
    np = pytest.importorskip("numpy")
    a, b = _unit(1, 0, 0), _unit(0, 1, 0)
    vectors = [np.array(a), np.array(b), np.array(_unit(0.9, 0.1, 0))]
    labels = speakers.cluster(vectors, [3.0, 3.0, 0.5])
    assert labels[2] == labels[0]


def test_recognised_words_join_the_line_they_were_said_in():
    words = [{"start": 1.0, "end": 1.3, "word": "久しぶり"},
             {"start": 1.3, "end": 1.6, "word": "だな"},
             {"start": 5.1, "end": 5.4, "word": "はい"}, {"start": 9.0, "end": 9.2, "word": "ok"}]
    lines = analysis.words_by_line(words, [(0.9, 2.0), (5.0, 6.0), (7.0, 8.0)])
    assert lines == ["久しぶりだな", "はい", ""]          # a word far from every line is dropped


def test_each_line_gets_its_pitch_and_how_much_it_moves():
    np = pytest.importorskip("numpy")
    rate = analysis.RATE
    t = np.arange(rate * 2) / rate
    flat = np.sin(2 * np.pi * 200 * t)
    gliding = np.sin(2 * np.pi * np.cumsum(220 + 80 * np.sin(2 * np.pi * 1.5 * t)) / rate)
    samples = np.concatenate([flat, gliding]).astype(np.float32)
    calm, lively = analysis.voice_measures(samples, [(0.0, 2.0), (2.0, 4.0)])
    assert calm["pitch_hz"] == pytest.approx(200, rel=0.05)
    assert lively["movement_st"] > calm["movement_st"]


def _episode(work, client):
    media = work / "media" / "abc"
    locale = media / "es-419"
    locale.mkdir(parents=True)
    rows = [("SPEAKER_00", 0.0, 3.0, 5.0, "It's been a long time."),
            ("SPEAKER_01", 4.0, 6.0, 0.0, "Yes."),
            ("SPEAKER_02", 7.0, 9.0, -4.0, "Kaito.")]
    (locale / "e02.script.json").write_text(json.dumps({"script_lang": "en", "segments": [
        {"index": i, "speaker": spk, "start": a, "end": b, "text_src": text, "issues": [],
         "cue": {"cue_id": f"c{i}", "measurement": {"relative_db": db}}}
        for i, (spk, a, b, db, text) in enumerate(rows)]}), encoding="utf-8")
    (locale / "e02.analysis.json").write_text(json.dumps({
        "source_lang": "ja", "original_text": True, "lines": [
            {"cue": "c0", "pitch_hz": 240.0, "movement_st": 4.1,
             "original_text": "久しぶりだな"}]}),
        encoding="utf-8")
    tone = media / "e02.vocals.wav"
    with wave.open(str(tone), "wb") as out:
        out.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        out.writeframes(b"".join(struct.pack("<h", 0) for _ in range(16000 * 10)))
    return "M:/Shows/HarborLights/Season 01/e02.mkv"


def test_an_analysed_episode_reads_line_by_line_and_names_join_voices(client_factory):
    client = client_factory()
    work = client.app.state.worker.config.work_dir
    path = _episode(work, client)
    data = client.get("/api/analysis", params={"path": path}).json()
    assert data["analysed"] and len(data["lines"]) == 3
    first = data["lines"][0]
    assert first["band"] == "intense" and first["original_text"] == "久しぶりだな"
    assert first["pitch_hz"] == 240.0 and data["languages"]["original"] == "ja"
    # SPEAKER_00 and SPEAKER_02 turn out to be the same character.
    saved = client.put("/api/analysis/names", json={"path": path, "names": {
        "SPEAKER_00": "Kaito", "SPEAKER_02": "Kaito", "SPEAKER_01": " ", "bogus": "x"}})
    assert saved.json()["names"] == {"SPEAKER_00": "Kaito", "SPEAKER_02": "Kaito"}
    named = client.get("/api/analysis", params={"path": path}).json()
    assert named["lines"][2]["character"] == "Kaito"
    assert named["speakers"][0]["speaker"] == "Kaito" and named["speakers"][0]["seconds"] == 5
    # The same file copied elsewhere keeps its names: they follow the file name.
    other = client.get("/api/analysis", params={"path": "D:/copy/e02.mkv"}).json()
    assert other["names"] == {"SPEAKER_00": "Kaito", "SPEAKER_02": "Kaito"}


def test_a_line_can_be_heard_but_only_a_short_window_of_the_analysed_audio(client_factory):
    client = client_factory()
    path = _episode(client.app.state.worker.config.work_dir, client)
    ok = client.get("/api/analysis/clip", params={"path": path, "start": 1, "end": 2})
    assert ok.status_code == 200 and ok.content[:4] == b"RIFF"
    long = client.get("/api/analysis/clip", params={"path": path, "start": 0, "end": 60})
    assert long.status_code == 422
    unknown = client.get("/api/analysis/clip", params={"path": "x/none.mkv", "start": 0, "end": 1})
    assert unknown.status_code == 404


def test_an_episode_not_analysed_says_so(client_factory):
    client = client_factory()
    data = client.get("/api/analysis", params={"path": "M:/nothing.mkv"}).json()
    assert data == {"analysed": False, "job": None}


def test_show_voices_count_named_characters_across_episodes(client_factory, tmp_path):
    from tests.test_series import setup_series

    client = client_factory()
    first, _second = setup_series(client, tmp_path)
    work = client.app.state.worker.config.work_dir
    locale = work / "media" / "z" / "es-419"
    locale.mkdir(parents=True)
    (locale / "first.script.json").write_text(json.dumps({"segments": [
        {"speaker": "SPEAKER_00", "start": 0, "end": 4,
         "cue": {"measurement": {"relative_db": 0}}},
        {"speaker": "SPEAKER_01", "start": 5, "end": 6,
         "cue": {"measurement": {"relative_db": 0}}}]}), encoding="utf-8")
    client.app.state.services._cache["speech"] = SimpleNamespace(
        voice_profiles=lambda: [], preset_engines=(), preset_voices=lambda e: [])
    client.put("/api/analysis/names", json={"path": str(first), "names": {"SPEAKER_00": "KAITO"}})
    rows = {r["speaker"]: r for r in client.get("/api/series/81234/voices").json()["speakers"]}
    assert rows["KAITO"]["share"] == 0.8 and "SPEAKER_00" not in rows


def test_without_a_token_lines_are_grouped_locally_not_given_one_narrator(tmp_path, monkeypatch):
    from doblarr.models import DubJob, Segment, Speaker
    from doblarr.stages import diarize

    monkeypatch.delenv("HF_TOKEN", raising=False)
    job = DubJob(tmp_path / "e.mkv", "ja", "es")
    job.segments = [Segment(0, 0, 2, "a"), Segment(1, 3, 5, "b")]
    job.vocals = tmp_path / "e.vocals.wav"
    job.vocals.write_bytes(b"x")

    def grouped(j, audio, sidecar=None, device="cpu", **_):
        for seg, label in zip(j.segments, ["SPEAKER_00", "SPEAKER_01"], strict=True):
            seg.speaker = label
        j.speakers = {k: Speaker(k) for k in ("SPEAKER_00", "SPEAKER_01")}
        return ["SPEAKER_00", "SPEAKER_01"]

    monkeypatch.setattr(diarize.speakers, "assign", grouped)
    diarize.run(job, enabled=True)
    assert set(job.speakers) == {"SPEAKER_00", "SPEAKER_01"}
    assert job.metrics["diarize"]["method"] == "local"

    def broken(*a, **k):
        raise RuntimeError("model missing")

    monkeypatch.setattr(diarize.speakers, "assign", broken)
    job.speakers = {}
    diarize.run(job, enabled=True)
    assert set(job.speakers) == {"NARRATOR"}          # the last resort, still a working dub


def test_songs_signs_and_titles_in_styled_subtitles_are_not_dialogue():
    from doblarr.subtitles import dialogue_only

    ass = "\n".join([
        "[Script Info]", "Title: x", "[V4+ Styles]",
        "Format: Name, Fontname", "Style: Default,Arial", "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
        "Dialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,It's been a long time, you two.",
        "Dialogue: 0,0:02:08.00,0:02:10.00,Lyrics JPN OP,,0,0,0,,tooku de kikoeru koe",
        "Dialogue: 0,0:02:08.00,0:02:10.00,Lyrics ENG OP,,0,0,0,,A voice heard in the distance",
        "Dialogue: 0,0:03:29.00,0:03:31.00,Title,,0,0,0,,The Storm Makes Its Move",
        "Dialogue: 0,0:05:00.00,0:05:01.00,Signs,,0,0,0,,Captain's Office",
        "Dialogue: 0,0:21:38.00,0:21:40.00,NEP,,0,0,0,,The time to unveil my newest work has come.",
    ])
    kept = [line for line in dialogue_only(ass).splitlines() if line.startswith("Dialogue:")]
    assert [line.split(",", 9)[3] for line in kept] == ["Default", "NEP"]
    assert "Style: Default,Arial" in dialogue_only(ass)          # styles section untouched
