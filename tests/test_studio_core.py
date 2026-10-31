"""Studio records, alignment, casting scope, session styles and export planning."""

import json

import pytest

from doblarr.store import Database
from doblarr.studio import alignment, casting, importer, records, session
from doblarr.studio.alignment import MapSegment, TimeMap
from tests.studio_fixtures import ENGLISH, JAPANESE


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "doblarr.db")
    yield database
    database.close()


# ----------------------------------------------------------------- records

def test_revisions_conflicts_and_frozen_records(db):
    first = records.put(db, "session", "s1", {"view": "overview"}, scope="s1")
    assert first["revision"] == 1
    second = records.put(db, "session", "s1", {"view": "cast"}, base_revision=1)
    assert second["revision"] == 2 and second["scope"] == "s1"
    with pytest.raises(records.StudioConflict) as stale:
        records.put(db, "session", "s1", {"view": "compare"}, base_revision=1)
    assert stale.value.current["view"] == "cast"      # the newer edit is handed back
    records.put(db, "variant", "v1", {"frozen": True, "outputs": {}})
    with pytest.raises(records.FrozenRecord):
        records.put(db, "variant", "v1", {"frozen": False})
    assert [r["view"] for r in records.history(db, "session", "s1")] == ["overview", "cast"]
    assert records.get(db, "session", "s1", 1)["view"] == "overview"
    with pytest.raises(ValueError, match="too large"):
        records.put(db, "session", "big", {"blob": "x" * (records.MAX_DOCUMENT_BYTES + 1)})


def test_migration_is_additive(tmp_path):
    from doblarr import store as store_mod

    path = tmp_path / "old.db"
    original = store_mod.MIGRATIONS
    try:
        store_mod.MIGRATIONS = original[:8]
        old = Database(path)
        old.execute("INSERT INTO voice_casts (title_key,title,cast_data,updated_at) "
                    "VALUES ('k','t','[]','now')")
        old.close()
    finally:
        store_mod.MIGRATIONS = original
    upgraded = Database(path)
    assert upgraded.load_cast("k") is not None
    assert upgraded.query_one("PRAGMA user_version")[0] == store_mod.SCHEMA_VERSION
    records.put(upgraded, "session", "x", {"a": 1})
    upgraded.close()


# --------------------------------------------------------------- alignment

def test_offset_merge_and_unmatched_lines():
    body = alignment.align(JAPANESE, ENGLISH, TimeMap([MapSegment(0, 1000, -2.0)]))
    groups = {tuple(g["source"]): g for g in body["groups"] if g["source"]}
    assert groups[("ja-1",)]["reference"] == ["en-1"]
    # Mina's two lines were merged into one English line: one 2:1 group.
    assert groups[("ja-2", "ja-3")]["reference"] == ["en-2"]
    assert groups[("ja-2", "ja-3")]["state"] == "matched"
    added = [g for g in body["groups"] if g["reference"] == ["en-5"]]
    assert added and added[0]["state"] == "unmatched"
    assert body["stats"]["shapes"]["2:1"] == 1


def test_a_missing_offset_shows_up_as_lower_confidence():
    wrong = alignment.align(JAPANESE[3:], ENGLISH[2:4], TimeMap.identity())
    right = alignment.align(JAPANESE[3:], ENGLISH[2:4], TimeMap([MapSegment(0, 1000, -2.0)]))

    def best(body):
        return max(g["confidence"] or 0 for g in body["groups"])

    assert best(right) > best(wrong)


def test_speed_drift_and_a_cut_are_mapped_piecewise():
    # The reference runs 2% slow, and 30-60 s of it is a scene the source does not have.
    source = [{"utt_id": f"s{i}", "start": t, "end": t + 2} for i, t in
              enumerate([5.0, 20.0, 70.0, 90.0])]
    reference = [{"utt_id": "r0", "start": 5.1, "end": 7.14},
                 {"utt_id": "r1", "start": 20.4, "end": 22.44},
                 {"utt_id": "rcut", "start": 40.0, "end": 42.0},
                 {"utt_id": "r2", "start": 100.0, "end": 102.0},
                 {"utt_id": "r3", "start": 120.0, "end": 122.0}]
    time_map = TimeMap([MapSegment(0, 30, 0.0, 1 / 1.02, 0.9, "xcorr"),
                        MapSegment(60, 200, -30.0, 1.0, 0.9, "xcorr")])
    body = alignment.align(source, reference, time_map)
    pairs = {tuple(g["source"]): tuple(g["reference"]) for g in body["groups"]
             if g["source"] and g["reference"]}
    assert pairs == {("s0",): ("r0",), ("s1",): ("r1",), ("s2",): ("r2",), ("s3",): ("r3",)}
    cut = next(g for g in body["groups"] if g["reference"] == ["rcut"])
    assert cut["state"] == "unmatched" and "outside the mapped range" in \
        cut["evidence"]["reason"]
    assert time_map.to_reference(45.0, 46.0) is None or \
        time_map.to_reference(45.0, 46.0)[2].start == 60


def test_manual_corrections_make_new_groups_and_keep_history():
    body = alignment.align(JAPANESE, ENGLISH, TimeMap([MapSegment(0, 1000, -2.0)]))
    merged = next(g for g in body["groups"] if g["source"] == ["ja-2", "ja-3"])
    excluded = alignment.apply_override(body, {"action": "exclude",
                                               "group_id": merged["group_id"]},
                                        JAPANESE, ENGLISH)
    assert next(g for g in excluded["groups"]
                if g["group_id"] == merged["group_id"])["state"] == "excluded"
    assert merged["group_id"] not in {g["group_id"] for g in alignment.usable_groups(excluded)}
    relinked = alignment.apply_override(excluded, {"action": "link", "source": ["ja-3"],
                                                   "reference": ["en-2"]},
                                        JAPANESE, ENGLISH)
    manual = next(g for g in relinked["groups"] if g["source"] == ["ja-3"])
    assert manual["manual"] and manual["state"] == "matched"
    assert len(relinked["overrides"]) == 2
    with pytest.raises(ValueError):
        alignment.apply_override(body, {"action": "link", "source": ["nope"],
                                        "reference": ["en-1"]}, JAPANESE, ENGLISH)


def test_differences_are_prompts_to_look():
    assert "numbers differ" in alignment.differences("二年半 2", "two and a half years 3")
    assert alignment.differences("行くの？", "Let's go.") == ["one is a question, the other is not"]
    assert "nothing in the reference here" in alignment.differences("はい", "")


# ----------------------------------------------------------------- casting

def test_series_episode_and_line_scopes_with_visible_exceptions(db):
    casting.decide(db, casting.ChoiceIn(character="KAITO", scope="series",
                                        scope_ref="series:kaito", voice="vb-1"))
    casting.decide(db, casting.ChoiceIn(character="MINA", scope="series",
                                        scope_ref="series:kaito", voice="vb-2"))
    casting.decide(db, casting.ChoiceIn(character="MINA", scope="episode",
                                        scope_ref="ep1", voice="vb-3"))
    casting.decide(db, casting.ChoiceIn(character="KAITO", scope="line", scope_ref="cue-9",
                                        episode_ref="ep1", voice="vb-4"))
    chosen = casting.effective(db, series_ref="series:kaito", episode_ref="ep1",
                               speakers=["KAITO", "MINA", "REN"])
    rows = {r["speaker"]: r for r in chosen["speakers"]}
    assert rows["KAITO"]["source"] == "series" and rows["KAITO"]["voice"] == "vb-1"
    assert rows["MINA"]["source"] == "episode" and rows["MINA"]["overrides_series"]
    assert rows["REN"]["source"] == "none"
    assert chosen["line_exceptions"][0]["cue_id"] == "cue-9"
    episode2 = casting.effective(db, series_ref="series:kaito", episode_ref="ep2",
                                 speakers=["MINA"])
    assert episode2["speakers"][0]["voice"] == "vb-2"          # carried into episode 2
    impact = casting.affected(db, series_ref="series:kaito", character="MINA",
                              episodes=["ep1", "ep2"])
    assert [r["episode"] for r in impact["follow"]] == ["ep2"]
    assert [r["episode"] for r in impact["keep"]] == ["ep1"]
    cast = casting.to_voice_cast([{"speaker_id": "MINA", "label": "Mina",
                                   "category": "young_f", "voice": "old"}], chosen)
    by = {e["speaker_id"]: e for e in cast}
    assert by["MINA"]["voice"] == "vb-3" and by["MINA"]["revision"]
    assert "REN" not in by


# -------------------------------------------------------------- session

def test_styles_change_questions_not_data(db, tmp_path):
    media = tmp_path / "ep.mkv"
    media.write_bytes(b"x")
    found = session.create(db, path=str(media), title="Ep 1")
    assert found["style"] == "manual"
    again = session.create(db, path=str(media), title="Ep 1")
    assert again["id"] == found["id"]
    guided = session.patch(db, found["id"], session.SessionPatch(
        base_revision=found["revision"], style="guided", checkpoints=["casting", "script"],
        view="cast", position=12.5))
    assert guided["position"] == 12.5 and guided["view"] == "cast"
    assert session.next_action(guided, {"job": None})["action"] == "audition"
    assert session.next_action(guided, {"job": None, "cast_decided": True})["action"] == \
        "render"
    done = {"job": {"status": "done"}, "cast_decided": True}
    assert session.next_action(guided, done)["action"] == "review"
    auto = session.patch(db, found["id"], session.SessionPatch(
        base_revision=guided["revision"], style="automatic", checkpoints=[]))
    assert auto["position"] == 12.5                                  # work preserved
    assert session.next_action(auto, {"job": {"status": "done"}})["action"] == "done"
    with pytest.raises(records.StudioConflict):
        session.patch(db, found["id"], session.SessionPatch(base_revision=1, view="compare"))


def test_overrides_only_carry_direction_and_budgets():
    found = {"direction": {"reference_policy": "reference_suggestions", "slang": True,
                           "adaptation": "localized"},
             "budgets": {"requests": 30, "candidates": 2, "retries": 1}}
    out = session.overrides(found, "ref.json", ["hold.json"])
    assert out["translate.reference_policy"] == "reference_suggestions"
    assert out["translate.holdout_files"] == ["hold.json"]
    assert out["quality.request_budget"] == 30
    assert "translate.reference_file" not in session.overrides(found, "", [])


def snapshot_rows():
    def row(index, text, take_text, findings=()):
        return {"index": index, "start": index * 3.0, "end": index * 3.0 + 2,
                "speaker": "A", "text_src": "x", "text_translated": text, "tts_text": text,
                "cue": {"cue_id": f"c{index}", "findings": list(findings),
                        "audio": {"takes": [{"take_id": f"t{index}", "text": take_text}],
                                  "selection": {"take_id": f"t{index}", "reason": "auto"}}}}
    return {"segments": [
        row(0, "Hola", "Hola"),
        row(1, "Adiós", "Adiós", [{"finding_id": "f1", "code": "content_mismatch",
                                   "severity": "error", "disposition": "open"}]),
        row(2, "Nos vemos", "Nos vemos", [{"finding_id": "f2", "code": "x",
                                           "severity": "warning", "disposition": "open"}]),
    ]}


def test_export_plan_marks_stale_audio_and_unresolved_findings():
    decisions = {"cues": {"c2": {"dispositions": {"f2": {"disposition": "accepted"}}}}}
    plan = session.export_plan(snapshot_rows(), decisions, {"c0": {"text": "Buenas"}})
    assert plan["stale"] == 1
    stale = next(r for r in plan["lines"] if r["cue"] == "c0")
    assert stale["stale"] and stale["text"] == "Buenas"
    assert [u["cue"] for u in plan["unresolved"]] == ["c1"]


# --------------------------------------------------------------- importer

def legacy(tmp_path):
    root = tmp_path / "work" / "benchmarks" / "quality-final" / "cmp-1"
    scene = root / "scene-00"
    scene.mkdir(parents=True)
    for name in ("baseline.wav", "reference-eng-0.wav", "source.wav"):
        (scene / name).write_bytes(b"RIFF")
    manifest = {
        "manifest_version": 2, "comparison_id": "cmp-1", "labels": {"A": "baseline"},
        "variants": {"baseline": {"timing.mode": "whole"}, "improved": {"timing.mode": "phrase"}},
        "source": {"media": str(tmp_path / "gone.mkv"), "stamp": {},
                   "stream": {"audio_index": 1, "available": [
                       {"audio_index": 0, "language": "eng"},
                       {"audio_index": 1, "language": "jpn"}]}},
        "scenes": [{"scene": {"index": 0, "title": "Talk", "window": {"start": 1, "end": 20},
                              "duration": 19, "speakers": ["SORA", "TOMOE"], "text": []},
                    "source_excerpt": str(scene / "source.wav"),
                    "references": [{"path": str(scene / "reference-eng-0.wav"),
                                    "language": "eng", "role": "reference dub"}],
                    "variants": [{"name": "baseline", "mixed": str(scene / "baseline.wav"),
                                  "matched": str(scene / "baseline-matched.wav")},
                                 {"name": "improved", "mixed": str(scene / "improved.wav")}]}],
    }
    path = root / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


def test_import_preview_maps_identities_and_names_missing_files(db, tmp_path):
    path = legacy(tmp_path)
    roots = [tmp_path / "work"]
    found = importer.preview(path, roots, tmp_path)
    assert found["kind"] == "comparison" and found["media"]["state"] == "missing"
    assert found["speakers"] == ["SORA", "TOMOE"]
    assert {t["legacy_role"] for t in found["tracks"]} == {"original", "reference dub"}
    assert "improved.wav" in " ".join(found["missing"])
    assert "no speech" in found["generates"]
    assert "local storage" in found["judgments"]
    with pytest.raises(importer.ImportError_, match="map every legacy speaker"):
        importer.apply(db, "s1", found, {"speakers": {"SORA": "SORA"}})
    mapping = {"speakers": {"SORA": "SPEAKER_00", "TOMOE": "SPEAKER_01"},
               "tracks": {"stream-0": "adaptation", "stream-1": "meaning"}}
    saved = importer.apply(db, "s1", found, mapping)
    assert saved["experimental_settings"]["improved"] == {"timing.mode": "phrase"}
    assert "not product defaults" in saved["settings_note"]
    again = importer.apply(db, "s1", importer.preview(path, roots, tmp_path), mapping)
    assert again["repeated"] and again["id"] == saved["id"]
    assert len(records.history(db, "import", saved["id"])) == 1
    with pytest.raises(Exception, match="inside the configured"):
        importer.preview(path, [tmp_path / "elsewhere"], tmp_path)


def test_exported_results_import_with_limits(db, tmp_path):
    path = legacy(tmp_path)
    found = importer.preview(path, [tmp_path / "work"], tmp_path)
    saved = importer.apply(db, "s1", found, {"speakers": {"SORA": "a", "TOMOE": "b"},
                                             "tracks": {"stream-0": "x", "stream-1": "y"}})
    text = "\n".join([
        "# Listening results — cmp-1", "", "## Scene 1 — Talk (episode 0:01–0:20)", "",
        "- Best: **B**", "- Issues: Rushed, Flat", "- Note: second line rushed", "",
        "| At | Version | What | How bad | In the original? | Note |",
        "| --- | --- | --- | --- | --- | --- |",
        "| 0:11.0 | Version A | Odd noise | Major | No | a giggle |"])
    parsed = importer.parse_results(text)
    assert parsed["answered"] == 1 and parsed["marks"] == 1
    assert parsed["scenes"][0]["marks"][0]["at"] == 11.0
    updated = importer.attach_results(db, saved, parsed, "results.md")
    assert updated["judgments_imported"][0]["provenance"].startswith("imported")
    same = importer.attach_results(db, updated, parsed, "results.md")
    assert len(same["judgments_imported"]) == 1
    with pytest.raises(importer.ImportError_, match="not cmp-2"):
        importer.attach_results(db, saved, {**parsed, "legacy_id": "x"}, "r.md") if False \
            else importer.attach_results(db, {**saved, "legacy_id": "cmp-2"}, parsed, "r.md")
    with pytest.raises(importer.ImportError_):
        importer.parse_results("# Something else")


# ------------------------------------------------------------ auditions

def _vocals(path, lines):
    """A dialogue stem with one tone per (start, end, hz) line and silence between."""
    import math
    import struct
    import wave

    rate = 16000
    total = max(end for _s, end, _hz in lines) + 1.0
    frames = bytearray()
    for i in range(int(rate * total)):
        t = i / rate
        hz = next((h for s, e, h in lines if s <= t < e), None)
        value = 0.3 * math.sin(2 * math.pi * hz * t) if hz else 0.0
        frames += struct.pack("<h", int(value * 32000))
    with wave.open(str(path), "wb") as out:
        out.setparams((1, 2, rate, 0, "NONE", "not compressed"))
        out.writeframes(bytes(frames))
    return path


@pytest.mark.parametrize(("pick", "expected"), [("low", 0), ("high", 1), ("", 2)])
def test_a_clone_reference_can_be_picked_by_pitch(tmp_path, pick, expected):
    import shutil

    pytest.importorskip("numpy")
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg is required")
    from doblarr.studio.auditions import character_reference

    lines = [(0.0, 4.2, 180), (10.0, 14.3, 290), (20.0, 28.0, 230)]
    vocals = _vocals(tmp_path / "vocals.wav", lines)
    rows = [{"index": i, "speaker": "TAMAKI", "start": s, "end": e, "text_src": f"line {i}"}
            for i, (s, e, _hz) in enumerate(lines)]
    _clip, found = character_reference(rows, "TAMAKI", vocals, tmp_path / "work", pick=pick)
    assert found["line"] == expected          # the default keeps the line nearest 8 s
    assert found["pitch_hz"] and found["pick"] == pick


def test_casting_carries_shaping_into_the_voice_cast_and_clears_it(db):
    casting.decide(db, casting.ChoiceIn(character="TAMAKI", scope="episode", scope_ref="ep1",
                                        voice="vb-t", pitch_semitones=2, formant_semitones=5))
    chosen = casting.effective(db, series_ref="", episode_ref="ep1", speakers=["TAMAKI"])
    cast = casting.to_voice_cast([], chosen)
    assert (cast[0]["pitch_semitones"], cast[0]["formant_semitones"]) == (2, 5)
    casting.decide(db, casting.ChoiceIn(character="TAMAKI", scope="episode", scope_ref="ep1",
                                        voice="vb-t"))
    again = casting.to_voice_cast(cast, casting.effective(
        db, series_ref="", episode_ref="ep1", speakers=["TAMAKI"]))
    assert "pitch_semitones" not in again[0] and "formant_semitones" not in again[0]
    assert again[0]["revision"] != cast[0]["revision"]
