"""Phase F acceptance — the whole review loop, through the real API and pipeline.

flagged cue -> hear its context -> change the wording and the direction ->
compare takes -> select one -> adjust its gain -> render -> compare with the
previous version. Every step goes through the routes the browser uses.
"""

import shutil

import pytest

from doblarr import benchmarks
from doblarr.cues import LEVELED
from doblarr.models import DubJob
from doblarr.pipeline import run_job
from doblarr.services import Services

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")


class Engine(benchmarks.ToneEngine):
    def __init__(self, root, heard=""):
        super().__init__(root, benchmarks.SCENE)
        self.heard = heard
        self.transcriptions = 0

    def transcribe(self, path, language=""):
        self.transcriptions += 1
        return {"text": self.heard}


def fixture_media(tmp_path):
    """The source media, written once.

    Rewriting it between runs would make every cached artifact stale, which is
    correct behaviour for a changed input and completely wrong as a model of
    re-rendering the same episode.
    """
    root = tmp_path / "media"
    media, subtitles = root / "fixture.mkv", root / "fixture.srt"
    if not media.is_file():
        media, subtitles = benchmarks.write_media(root)
    return media, subtitles


def render(client, tmp_path, overrides=None, engine=None, job_id=None):
    """Run one real dub through the pipeline and register it in the queue."""
    config = client.app.state.worker.config.with_overrides({
        "dub.dry_run": False, "dub.voice_mode": "preset",
        "dub.preset_voices": ["tone-voice"], "dub.preserve_versions": True,
        "transcribe.diarize": False, "levels.mode": "consistent",
        "translate.provider": "passthrough",
        "quality.asr": "all", "quality.max_retries": 0,
        **(overrides or {}),
    })
    media, subtitles = fixture_media(tmp_path)
    tone = engine or Engine(tmp_path, heard="algo distinto")
    services = Services(config)
    services._cache["voicebox"] = tone
    job = DubJob(input_file=media, source_lang="ja", target_lang="es",
                 subtitle_file=subtitles)
    job.script_is_target = True
    run_job(job, config, services=services)
    store = client.app.state.jobs
    if job_id:
        store.update(job_id, status="done", review_file=str(job.review_file),
                     report_file=str(job.report_file),
                     output_file=str(job.output_file), version_id=job.version_id,
                     version_name=job.version_name)
        return store.get(job_id), job, tone
    queued = store.add(title="Fixture", source="manual", source_lang="ja",
                       target_lang="es", input_file=str(job.input_file), status="done",
                       review_file=str(job.review_file),
                       report_file=str(job.report_file),
                       output_file=str(job.output_file), version_id=job.version_id,
                       version_name=job.version_name)
    return queued, job, tone


def test_the_whole_review_loop_works_through_the_real_routes(client_factory, tmp_path):
    client = client_factory()
    queued, job, tone = render(client, tmp_path)
    first_version = job.version_id
    generated = len(tone.requests)
    assert generated > 0

    # 1. Find a flagged cue. The mock recognizer heard something else, so every
    #    line carries a content finding.
    data = client.get(f"/api/jobs/{queued.id}/review").json()
    assert data["verification_policy"] == "all"
    flagged = next(row for row in data["segments"] if row["cue"]["findings"])
    codes = {f["code"] for f in flagged["cue"]["findings"]}
    assert "content_mismatch" in codes
    assert flagged["cue"]["verification"]["state"] == "mismatch"
    assert flagged["cue"]["verification"]["heard"] == "algo distinto"

    # 2. Hear its context: the exchange around it, at actual levels.
    scene = client.get(f"/api/jobs/{queued.id}/scene/{flagged['index']}").json()
    assert flagged["index"] in scene["cues"]
    assert scene["available"]["dub"] and scene["available"]["line"]
    assert scene["boundary"] == "heuristic"
    for kind in ("dub", "source", "line", "take"):
        response = client.get(
            f"/api/jobs/{queued.id}/preview/{kind}/{flagged['index']}")
        assert response.status_code == 200, kind
        assert response.headers["content-type"] == "audio/wav"

    # 3. Record a verdict on one finding, against this exact snapshot.
    finding = flagged["cue"]["findings"][0]
    assert client.post(f"/api/jobs/{queued.id}/decisions", json={
        "cue": flagged["cue"]["cue_id"], "base_revision": data["revision"],
        "dispositions": [{"finding": finding["finding_id"], "disposition": "accepted",
                          "note": "the recognizer mis-hears tones"}]}).status_code == 200

    # 4. Change the wording and the direction, ask for alternatives, and see
    #    what that will actually cost before it runs.
    queue = client.post(f"/api/jobs/{queued.id}/review", json={
        "base_revision": data["revision"],
        "edits": [{"index": flagged["index"], "cue": flagged["cue"]["cue_id"],
                   "text": "Una linea distinta", "mode": "whisper",
                   "direction": "hold back", "candidates": 2}]})
    assert queue.status_code == 200
    plan = queue.json()["rerun"]
    assert plan["generating"] == 1 and plan["candidates"] == 2
    assert plan["lines"][0]["work"] == "generates new speech"
    assert queue.json()["previous_version"] == first_version
    overrides = queue.json()["job"]["overrides"]
    assert overrides["dub.candidates"] == {flagged["cue"]["cue_id"]: 2}
    # The verdict travelled with the re-render it was made about.
    carried = overrides["dub.line_edits"][str(flagged["index"])]
    assert carried["dispositions"][finding["finding_id"]]["disposition"] == "accepted"

    # 5. Run the queued job for real: only the edited line is regenerated, and
    #    the two requested alternatives are generated beside it.
    second = client.app.state.jobs.get(queue.json()["job"]["id"])
    engine = Engine(tmp_path, heard="Una linea distinta")
    _, rerun, tone2 = render(client, tmp_path, overrides=second.overrides,
                             engine=engine, job_id=second.id)
    assert len(tone2.requests) == 3, "one changed line plus its two alternatives"
    edited = next(s for s in rerun.segments if s.cue_id == flagged["cue"]["cue_id"])
    # The take of the *previous* wording is still there: a re-render adds
    # takes, it does not destroy the ones someone may want to go back to.
    assert len(edited.audio.takes) == 4
    assert sum(1 for t in edited.audio.takes if t.origin == "candidate") == 2
    assert edited.intent.mode == "whisper"
    assert rerun.metrics["candidates_generated"] == 2

    # 6. Compare the takes, choose one, and set a gain. Choosing costs nothing.
    data2 = client.get(f"/api/jobs/{second.id}/review").json()
    row = next(r for r in data2["segments"]
               if r["cue"]["cue_id"] == flagged["cue"]["cue_id"])
    scene2 = client.get(f"/api/jobs/{second.id}/scene/{row['index']}").json()
    alternatives = [t for t in scene2["takes"] if t["origin"] == "candidate"]
    assert len(alternatives) == 2 and all(t["available"] for t in alternatives)
    chosen = alternatives[0]["take_id"]
    assert client.get(
        f"/api/jobs/{second.id}/preview/take/{row['index']}?take={chosen}"
    ).status_code == 200

    third = client.post(f"/api/jobs/{second.id}/review", json={
        "base_revision": data2["revision"],
        "edits": [{"index": row["index"], "cue": row["cue"]["cue_id"],
                   "take": chosen, "gain_db": -3.0}]})
    assert third.status_code == 200
    assert third.json()["rerun"]["generating"] == 0
    assert third.json()["rerun"]["lines"][0]["work"] == "re-renders existing audio"

    third_job = client.app.state.jobs.get(third.json()["job"]["id"])
    engine3 = Engine(tmp_path, heard="Una linea distinta")
    _, final, tone3 = render(client, tmp_path, overrides=third_job.overrides,
                             engine=engine3, job_id=third_job.id)
    assert len(tone3.requests) == 0, "selecting a take and a gain needs no speech"
    picked = next(s for s in final.segments if s.cue_id == flagged["cue"]["cue_id"])
    assert picked.audio.selection.take_id == chosen
    assert picked.audio.selection.reason in ("review", "restored")
    assert picked.level.manual and picked.level.applied_db == pytest.approx(-3.0)
    assert picked.audio.render(LEVELED) is not None

    # 7. Compare with the previous version: both renders are still playable.
    versions = client.get(f"/api/jobs/{third_job.id}/versions").json()["versions"]
    assert len(versions) >= 2
    assert any(v["version_id"] == first_version for v in versions)
    assert all(v["available"] for v in versions)
    media = client.get(f"/api/jobs/{third_job.id}/versions/{first_version}/file")
    assert media.status_code == 200 and media.content


def test_a_direction_the_engine_cannot_honor_changes_no_audio(client_factory, tmp_path):
    """Recorded as asked-for, not as applied, and it regenerates nothing.

    The tone engine takes no delivery instruction. Sending one anyway and
    calling the resulting identical audio "directed" would be the exact
    failure D06 forbids, so the request is unchanged and the intent says why.
    """
    client = client_factory()
    queued, _, _ = render(client, tmp_path, {"quality.asr": "off"})
    data = client.get(f"/api/jobs/{queued.id}/review").json()
    row = data["segments"][0]

    queue = client.post(f"/api/jobs/{queued.id}/review", json={
        "base_revision": data["revision"],
        "edits": [{"index": row["index"], "cue": row["cue"]["cue_id"],
                   "mode": "thought", "traits": ["restrained"],
                   "direction": "almost to himself", "gain_db": -2.0}]})
    second = client.app.state.jobs.get(queue.json()["job"]["id"])
    _, rerun, tone = render(client, tmp_path, overrides=second.overrides,
                            engine=Engine(tmp_path), job_id=second.id)
    assert len(tone.requests) == 0

    # Every field a reviewer set comes back through the API attached to the cue.
    data2 = client.get(f"/api/jobs/{second.id}/review").json()
    edited = next(r for r in data2["segments"]
                  if r["cue"]["cue_id"] == row["cue"]["cue_id"])
    intent = edited["cue"]["intent"]
    assert intent["mode"] == "thought" and intent["traits"] == ["restrained"]
    assert "almost to himself" in intent["effective"]
    # The tone engine is not directable, so the instruction is recorded as
    # asked-for rather than reported as applied.
    assert intent["capability"] in ("unsupported", "unknown")
    assert edited["cue"]["level"]["manual"] is True
    assert edited["cue"]["level"]["applied_db"] == pytest.approx(-2.0)
    assert data2["levels"]["mode"] == "consistent"


def test_only_the_edited_line_is_regenerated(client_factory, tmp_path):
    client = client_factory()
    queued, job, _ = render(client, tmp_path, {"quality.asr": "off"})
    lines = len(job.segments)
    data = client.get(f"/api/jobs/{queued.id}/review").json()
    row = data["segments"][1]

    queue = client.post(f"/api/jobs/{queued.id}/review", json={
        "base_revision": data["revision"],
        "edits": [{"index": row["index"], "cue": row["cue"]["cue_id"],
                   "text": "Una linea completamente distinta"}]})
    second = client.app.state.jobs.get(queue.json()["job"]["id"])
    _, rerun, tone = render(client, tmp_path, overrides=second.overrides,
                            engine=Engine(tmp_path), job_id=second.id)
    assert len(tone.requests) == 1, "one changed line, one generation"
    assert rerun.metrics["tts_cache_hits"] == lines - 1
    assert tone.requests[0]["text"] == "Una linea completamente distinta"


def test_a_render_alone_never_marks_a_scene_reviewed(client_factory, tmp_path):
    client = client_factory()
    queued, _, _ = render(client, tmp_path)
    data = client.get(f"/api/jobs/{queued.id}/review").json()
    row = data["segments"][0]
    finding = row["cue"]["findings"][0]
    client.post(f"/api/jobs/{queued.id}/decisions", json={
        "cue": row["cue"]["cue_id"], "base_revision": data["revision"],
        "dispositions": [{"finding": finding["finding_id"], "disposition": "accepted"}]})

    # Re-render the same job in place; the snapshot moves on.
    _, _, _ = render(client, tmp_path, {"quality.asr": "all"},
                     engine=Engine(tmp_path, heard="otra cosa"), job_id=queued.id)
    after = client.get(f"/api/jobs/{queued.id}/review").json()
    again = next(r for r in after["segments"]
                 if r["cue"]["cue_id"] == row["cue"]["cue_id"])
    verdicts = [f for f in again["cue"]["findings"]
                if f.get("review", {}).get("stale")]
    assert verdicts, "the earlier verdict must be visible as stale"
    assert all(f["disposition"] != "accepted" for f in verdicts)
