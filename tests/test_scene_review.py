"""D04 — scene windows, guarded previews, findings and reviewer decisions."""

import math
import shutil
import struct
import wave

import pytest

from doblarr import preview
from doblarr.cues import FITTED, RAW, Artifact, Finding, Selection, Span, Take
from doblarr.models import DubJob, Segment, Speaker
from doblarr.review import load_decisions, write_review
from doblarr.telemetry import RunReport

RATE = 48000


def tone(path, seconds=1.0, amplitude=0.3):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as out:
        out.setparams((1, 2, RATE, 0, "NONE", "not compressed"))
        out.writeframes(b"".join(
            struct.pack("<h", int(amplitude * math.sin(2 * math.pi * 220 * i / RATE) * 32000))
            for i in range(round(RATE * seconds))))
    return path


def exchange(work, tmp_path):
    """Four cues: three in one exchange, one after a long silence."""
    job = DubJob(tmp_path / "movie.mkv", "ja", "es")
    job.input_file.write_bytes(b"media")
    job.speakers = {"A": Speaker("A"), "B": Speaker("B")}
    starts = [(1.0, 2.0), (2.4, 3.6), (4.0, 5.2), (40.0, 41.0)]
    for index, (start, end) in enumerate(starts):
        seg = Segment(index, start, end, f"src {index}", speaker="A" if index % 2 else "B",
                      text_translated=f"linea {index}")
        seg.source.spans = [Span(start, end, "source")]
        clip = tone(work / "clips" / f"line{index}.wav")
        take = Take(take_id=f"take-{index}", fingerprint=f"gen-{index}", engine="tone",
                    state="generated", text=f"linea {index}",
                    raw=Artifact(role=RAW, path=str(clip), fingerprint=f"gen-{index}"))
        seg.audio.takes.append(take)
        seg.audio.selection = Selection(take_id=take.take_id, reason="auto")
        seg.audio.put_render(Artifact(role=FITTED, path=str(clip),
                                      fingerprint=f"fit-{index}", derived_from=RAW))
        seg.audio_clip = clip
        seg.findings.append(Finding(finding_id=f"finding-{index}", code="timing_overflow",
                                    kind="timing", severity="warning", detector="fit-timing/1",
                                    inputs=f"in-{index}"))
        job.segments.append(seg)
    job.source_track = tone(work / "source.wav", 45.0)
    job.source_audio = job.source_track
    job.dubbed_track = tone(work / "dub.wav", 45.0)
    job.artifacts_dir = work
    RunReport(job, work)
    write_review(job, work)
    return job


def review_client(client, tmp_path):
    work = client.app.state.worker.config.work_dir
    work.mkdir(parents=True, exist_ok=True)
    job = exchange(work, tmp_path)
    queued = client.app.state.jobs.add(
        title="Film", source="manual", source_lang="ja", target_lang="es",
        input_file=str(job.input_file), status="done",
        review_file=str(job.review_file), report_file=str(job.report_file))
    return queued, job


# -- windows ---------------------------------------------------------------

def test_a_window_gathers_the_exchange_and_stops_at_a_silence(tmp_path):
    job = exchange(tmp_path / "work", tmp_path)
    frame = preview.window(job.segments, 1, context=3)
    assert frame["cues"] == [0, 1, 2]          # the fourth cue is 35s later
    assert frame["boundary"] == "heuristic"
    assert "not a detected scene cut" in frame["boundary_note"]


def test_the_source_window_comes_from_source_spans_not_the_cue_window(tmp_path):
    job = exchange(tmp_path / "work", tmp_path)
    moved = job.segments[1]
    moved.start, moved.end = 20.0, 21.2        # a review edit moved the placement
    frame = preview.window([moved], 1, context=0)
    assert frame["target"]["start"] == pytest.approx(19.4)
    assert frame["source"]["start"] == pytest.approx(1.8)   # where it was spoken
    assert frame["source"]["domain"] == "source"


def test_a_window_is_bounded_however_much_context_is_asked_for(tmp_path):
    job = exchange(tmp_path / "work", tmp_path)
    frame = preview.window(job.segments, 0, context=8, bounds=(0.0, 500.0))
    assert frame["target"]["end"] - frame["target"]["start"] <= preview.MAX_WINDOW_SECONDS


def test_an_unknown_line_is_an_actionable_error(tmp_path):
    job = exchange(tmp_path / "work", tmp_path)
    with pytest.raises(preview.PreviewError, match="not in this review"):
        preview.window(job.segments, 99)


# -- preview resolution ----------------------------------------------------

@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")
def test_source_and_dub_excerpts_cover_the_same_exchange(tmp_path):
    job = exchange(tmp_path / "work", tmp_path)
    source = preview.resolve(job, job.segments, "source", 1)
    dub = preview.resolve(job, job.segments, "dub", 1)
    assert source["path"].is_file() and dub["path"].is_file()
    assert source["cues"] == dub["cues"] == [0, 1, 2]
    with wave.open(str(source["path"])) as audio:
        seconds = audio.getnframes() / audio.getframerate()
    assert seconds == pytest.approx(5.8 - 0.4, abs=0.2)
    # Reused rather than recut when nothing changed.
    assert preview.resolve(job, job.segments, "source", 1)["path"] == source["path"]


def test_a_dry_take_and_the_processed_line_are_different_things(tmp_path):
    job = exchange(tmp_path / "work", tmp_path)
    take = preview.resolve(job, job.segments, "take", 1)
    line = preview.resolve(job, job.segments, "line", 1)
    assert take["role"] == RAW and line["role"] == FITTED
    assert take["whole_file"] and line["whole_file"]


def test_missing_media_is_an_explicit_refusal_not_an_empty_file(tmp_path):
    job = exchange(tmp_path / "work", tmp_path)
    job.source_track = job.source_audio = None
    with pytest.raises(preview.PreviewError, match="not available"):
        preview.resolve(job, job.segments, "source", 1)
    job.dubbed_track = None
    with pytest.raises(preview.PreviewError, match="no mixed dub"):
        preview.resolve(job, job.segments, "dub", 1)
    with pytest.raises(preview.PreviewError, match="unknown preview kind"):
        preview.resolve(job, job.segments, "video", 1)


# -- the API ---------------------------------------------------------------

def test_the_scene_route_reports_cues_takes_and_what_can_play(client_factory, tmp_path):
    client = client_factory()
    queued, _ = review_client(client, tmp_path)
    scene = client.get(f"/api/jobs/{queued.id}/scene/1").json()
    assert scene["cues"] == [0, 1, 2]
    assert scene["available"]["source"] and scene["available"]["dub"]
    assert scene["available"]["line"] and scene["available"]["take"]
    assert [t["take_id"] for t in scene["takes"]] == ["take-1"]
    assert scene["takes"][0]["selected"] is True
    assert scene["selection"]["reason"] == "auto"


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")
def test_previews_stream_with_ranges_and_never_leave_the_work_dir(client_factory, tmp_path):
    client = client_factory()
    queued, _ = review_client(client, tmp_path)
    for kind in ("source", "dub", "line", "take"):
        whole = client.get(f"/api/jobs/{queued.id}/preview/{kind}/1")
        assert whole.status_code == 200, kind
        assert whole.headers["content-type"] == "audio/wav"
        part = client.get(f"/api/jobs/{queued.id}/preview/{kind}/1",
                          headers={"Range": "bytes=0-15"})
        assert part.status_code == 206 and len(part.content) == 16


def test_a_preview_of_a_line_without_audio_is_refused_with_a_reason(client_factory, tmp_path):
    client = client_factory()
    queued, job = review_client(client, tmp_path)
    for seg in job.segments:
        for artifact in [*seg.audio.renders, seg.audio.selected().raw]:
            from pathlib import Path

            Path(artifact.path).unlink(missing_ok=True)
    response = client.get(f"/api/jobs/{queued.id}/preview/line/1")
    assert response.status_code == 409
    assert "rendered audio" in response.json()["error"]


def test_the_review_payload_says_which_previews_exist_without_leaking_paths(
        client_factory, tmp_path):
    client = client_factory()
    queued, _ = review_client(client, tmp_path)
    data = client.get(f"/api/jobs/{queued.id}/review").json()
    assert data["previews"] == {
        "source": True, "dub": True, "note": "",
        # No separation ran for this fixture, so both stems are honestly absent
        # and the bed is named as what it is rather than offered as a preview.
        "vocals": False, "bed": False,
        "bed_note": "separation did not run, so the original mix is the bed"}
    assert "media" not in data
    assert data["levels"]["mode"] == "legacy"
    assert data["verification_policy"] == "off"
    assert data["timing"]["mode"] == "whole" and data["coverage"]["mode"] == "off"


# -- decisions -------------------------------------------------------------

def test_a_disposition_is_recorded_against_the_snapshot_it_was_made_on(
        client_factory, tmp_path):
    client = client_factory()
    queued, job = review_client(client, tmp_path)
    data = client.get(f"/api/jobs/{queued.id}/review").json()
    cue_id = data["segments"][1]["cue"]["cue_id"]

    response = client.post(f"/api/jobs/{queued.id}/decisions", json={
        "cue": cue_id, "base_revision": data["revision"], "actor": "reviewer",
        "dispositions": [{"finding": "finding-1", "disposition": "accepted",
                          "note": "the overlap is intentional here"}]})
    assert response.status_code == 200
    stored = load_decisions(
        client.app.state.worker.config.work_dir, queued.id)["cues"][cue_id]
    assert stored["dispositions"]["finding-1"]["disposition"] == "accepted"
    assert stored["revision"] == data["revision"]

    # It survives a reload and is shown against the finding itself.
    reloaded = client.get(f"/api/jobs/{queued.id}/review").json()
    finding = next(f for f in reloaded["segments"][1]["cue"]["findings"]
                   if f["finding_id"] == "finding-1")
    assert finding["disposition"] == "accepted"
    assert finding["review"]["stale"] is False
    assert finding["review"]["note"] == "the overlap is intentional here"


def test_a_stale_decision_is_rejected_and_an_old_one_is_shown_as_stale(
        client_factory, tmp_path):
    client = client_factory()
    queued, job = review_client(client, tmp_path)
    data = client.get(f"/api/jobs/{queued.id}/review").json()
    cue_id = data["segments"][1]["cue"]["cue_id"]
    client.post(f"/api/jobs/{queued.id}/decisions", json={
        "cue": cue_id, "base_revision": data["revision"],
        "dispositions": [{"finding": "finding-1", "disposition": "accepted"}]})

    conflict = client.post(f"/api/jobs/{queued.id}/decisions", json={
        "cue": cue_id, "base_revision": "stale-revision",
        "dispositions": [{"finding": "finding-1", "disposition": "fixed"}]})
    assert conflict.status_code == 409

    # A re-render moves the snapshot on; the old verdict is visible but not applied.
    job.segments[1].text_translated = "una linea distinta"
    write_review(job, client.app.state.worker.config.work_dir)
    reloaded = client.get(f"/api/jobs/{queued.id}/review").json()
    finding = next(f for f in reloaded["segments"][1]["cue"]["findings"]
                   if f["finding_id"] == "finding-1")
    assert finding["review"]["stale"] is True
    assert finding["disposition"] == "open"     # the new audio is not approved


def test_a_verdict_travels_with_the_re_render_it_was_made_about(client_factory, tmp_path):
    client = client_factory()
    queued, _ = review_client(client, tmp_path)
    data = client.get(f"/api/jobs/{queued.id}/review").json()
    cue_id = data["segments"][1]["cue"]["cue_id"]
    client.post(f"/api/jobs/{queued.id}/decisions", json={
        "cue": cue_id, "base_revision": data["revision"],
        "dispositions": [{"finding": "finding-1", "disposition": "accepted",
                          "note": "intentional"}]})

    queued_again = client.post(f"/api/jobs/{queued.id}/review", json={
        "edits": [{"index": 0, "text": "otra cosa"}],
        "base_revision": data["revision"]}).json()["job"]
    edits = queued_again["overrides"]["dub.line_edits"]
    carried = next(e for e in edits.values() if e.get("cue") == cue_id)
    assert carried["dispositions"]["finding-1"]["disposition"] == "accepted"


def test_an_unknown_finding_or_disposition_is_refused(client_factory, tmp_path):
    client = client_factory()
    queued, _ = review_client(client, tmp_path)
    data = client.get(f"/api/jobs/{queued.id}/review").json()
    cue_id = data["segments"][1]["cue"]["cue_id"]
    assert client.post(f"/api/jobs/{queued.id}/decisions", json={
        "cue": cue_id, "base_revision": data["revision"],
        "dispositions": [{"finding": "finding-1",
                          "disposition": "looks-fine"}]}).status_code == 422
    assert client.post(f"/api/jobs/{queued.id}/decisions", json={
        "cue": "not-a-cue", "base_revision": data["revision"],
        "dispositions": []}).status_code == 404


# -- the whole workflow ----------------------------------------------------

def test_direction_take_and_gain_survive_the_queue(client_factory, tmp_path):
    client = client_factory()
    queued, _ = review_client(client, tmp_path)
    data = client.get(f"/api/jobs/{queued.id}/review").json()
    row = data["segments"][1]
    response = client.post(f"/api/jobs/{queued.id}/review", json={
        "base_revision": data["revision"],
        "edits": [{"index": 1, "cue": row["cue"]["cue_id"], "mode": "whisper",
                   "traits": ["urgent"], "direction": "hold back",
                   "take": "take-1", "gain_db": -2.5, "candidates": 2}]})
    assert response.status_code == 200
    overrides = response.json()["job"]["overrides"]
    edit = overrides["dub.line_edits"]["1"]
    assert edit["mode"] == "whisper" and edit["traits"] == ["urgent"]
    assert edit["direction"] == "hold back" and edit["take"] == "take-1"
    assert edit["gain_db"] == -2.5
    assert overrides["dub.candidates"] == {row["cue"]["cue_id"]: 2}


def test_an_unknown_take_or_mode_is_refused_before_anything_is_queued(
        client_factory, tmp_path):
    client = client_factory()
    queued, _ = review_client(client, tmp_path)
    data = client.get(f"/api/jobs/{queued.id}/review").json()
    cue_id = data["segments"][1]["cue"]["cue_id"]
    assert client.post(f"/api/jobs/{queued.id}/review", json={
        "base_revision": data["revision"],
        "edits": [{"index": 1, "cue": cue_id, "take": "take-9"}]}).status_code == 409
    assert client.post(f"/api/jobs/{queued.id}/review", json={
        "base_revision": data["revision"],
        "edits": [{"index": 1, "mode": "singing"}]}).status_code == 422
    assert client.post(f"/api/jobs/{queued.id}/review", json={
        "base_revision": data["revision"],
        "edits": [{"index": 1, "gain_db": 90}]}).status_code == 422


# -- Plan 04: phrase timing and coverage over the API ------------------------


def planned(job, work, tmp_path):
    """Give the middle cue a real phrase plan, a collision and one event.

    Written as the pipeline would write it, so the API tests exercise the same
    records the run produces rather than a shape invented for the test.
    """
    from doblarr.cues import (
        CLIP,
        PHRASED,
        Anchor,
        NonverbalEvent,
        Pause,
        Phrase,
        TimingPlan,
    )

    seg = job.segments[1]
    phrased = tone(work / "clips" / "line1.phrased.wav", 1.2)
    seg.audio.drop_renders((FITTED,))
    seg.audio.put_render(Artifact(role=PHRASED, path=str(phrased),
                                  fingerprint="phrase-1", derived_from=RAW,
                                  duration=1.2))
    seg.audio_clip = phrased
    first, second = f"{seg.cue_id}:p0", f"{seg.cue_id}:p1"
    seg.phrasing = TimingPlan(
        mode="phrase", state="applied", planner="phrase-timing/1",
        reason="2 phrases; 0.18s of padding redistributed", slot=1.2,
        planned_duration=1.18, actual_duration=1.2, max_stretch=1.0, moved=0.18,
        protected_kept=0.5, speech_in=0.7, speech_out=0.7, inputs="phrase-1",
        phrases=[Phrase(phrase_id=first, order=0, text="Primera",
                        clip=Span(0.0, 0.35, CLIP), method="aligned"),
                 Phrase(phrase_id=second, order=1, text="Segunda",
                        clip=Span(0.85, 1.2, CLIP), method="aligned")],
        pauses=[Pause(pause_id=f"{seg.cue_id}:g0", after=first,
                      clip=Span(0.35, 0.85, CLIP), kind="pause", protected=True,
                      planned=0.5)],
        anchors=[Anchor(anchor_id=f"{seg.cue_id}:ap1s", phrase_id=second,
                        edge="start", at=0.85, kind="hard", origin="review",
                        observed=0.87)],
        pieces=[{"kind": "speech", "phrase": first, "in": [0.0, 0.35],
                 "factor": 1.0, "at": 0.0, "out": 0.35},
                {"kind": "gap", "pause": f"{seg.cue_id}:g0", "measured": 0.5,
                 "at": 0.35, "out": 0.5},
                {"kind": "speech", "phrase": second, "in": [0.85, 1.2],
                 "factor": 1.0, "at": 0.85, "out": 0.35}])
    seg.findings.append(Finding(
        finding_id="collision-1", code="timing_collision", kind="timing",
        severity="warning", detector="conversation/1", inputs="2.400/3.700",
        evidence={"with_cue": job.segments[2].cue_id, "with_line": 2,
                  "seconds": 0.3, "source_overlap": 0.0,
                  "note": "these two lines did not overlap in the original"}))

    placed = tone(work / "reactions" / "laugh.wav", 0.8)
    job.nonverbal = [NonverbalEvent(
        event_id="event-1", cue_id="retired-cue", type="laugh", category="vocal",
        speaker="B", text="[laughter]", source=[Span(4.6, 5.4, "source")],
        target=Span(4.6, 5.4, "target"), evidence="subtitle",
        decision="retain", coverage="retained", reason="cut from the vocals stem",
        artifact=Artifact(role="unknown", path=str(placed), fingerprint="ev-1",
                          derived_from="vocals", proven=False),
        checks={"position": "whole", "bed_kind": "estimated"})]
    job.vocals = tone(work / "vocals.wav", 45.0)
    job.background = tone(work / "background.wav", 45.0)
    job.timing_edits = {seg.cue_id: {"anchors": [
        {"phrase": second, "edge": "start", "at": 0.85}]}}
    write_review(job, work)
    return job


def planned_client(client, tmp_path):
    work = client.app.state.worker.config.work_dir
    work.mkdir(parents=True, exist_ok=True)
    job = planned(exchange(work, tmp_path), work, tmp_path)
    queued = client.app.state.jobs.add(
        title="Film", source="manual", source_lang="ja", target_lang="es",
        input_file=str(job.input_file), status="done",
        review_file=str(job.review_file), report_file=str(job.report_file))
    return queued, job


def test_the_scene_says_how_the_line_was_fitted_and_what_it_collides_with(
        client_factory, tmp_path):
    client = client_factory()
    queued, job = planned_client(client, tmp_path)
    scene = client.get(f"/api/jobs/{queued.id}/scene/1").json()
    plan = scene["phrasing"]
    assert plan["mode"] == "phrase" and plan["state"] == "applied"
    assert [p["text"] for p in plan["phrases"]] == ["Primera", "Segunda"]
    assert plan["phrases"][1]["at"] == pytest.approx(0.85)
    assert plan["pauses"][0]["protected"] is True
    # The anchor reports where it actually landed, not where it was asked to.
    anchor = plan["anchors"][0]
    assert anchor["at"] == pytest.approx(0.85)
    assert anchor["observed"] == pytest.approx(0.87)
    assert anchor["error"] == pytest.approx(0.02, abs=1e-6)
    collision = scene["collisions"][0]
    assert collision["code"] == "timing_collision" and collision["with_line"] == 2
    assert scene["timing_edit"]["anchors"][0]["at"] == pytest.approx(0.85)


def test_the_scene_lists_the_events_inside_its_window_without_local_paths(
        client_factory, tmp_path):
    client = client_factory()
    queued, job = planned_client(client, tmp_path)
    scene = client.get(f"/api/jobs/{queued.id}/scene/2").json()
    event = scene["events"][0]
    assert event["event_id"] == "event-1" and event["type"] == "laugh"
    assert event["coverage"] == "retained" and event["playable"] is True
    assert "path" not in event["artifact"] and event["artifact"]["available"] is True
    assert scene["available"]["event"] is True
    assert scene["available"]["bed"] is True and scene["available"]["vocals"] is True


def test_the_review_payload_carries_the_frozen_timing_and_coverage_policy(
        client_factory, tmp_path):
    client = client_factory()
    queued, job = planned_client(client, tmp_path)
    data = client.get(f"/api/jobs/{queued.id}/review").json()
    assert data["timing"]["mode"] == "whole"      # this snapshot froze none
    assert data["coverage"]["mode"] == "off"
    assert data["nonverbal"][0]["event_id"] == "event-1"
    assert "path" not in data["nonverbal"][0]["artifact"]
    assert data["segments"][1]["cue"]["phrasing"]["state"] == "applied"


def test_the_bed_the_vocals_and_one_event_are_all_playable(client_factory, tmp_path):
    client = client_factory()
    queued, job = planned_client(client, tmp_path)
    for kind in ("bed", "vocals"):
        response = client.get(f"/api/jobs/{queued.id}/preview/{kind}/1")
        assert response.status_code == 200, kind
        assert response.headers["content-type"] == "audio/wav"
    response = client.get(f"/api/jobs/{queued.id}/preview/event/2?event=event-1")
    assert response.status_code == 200
    missing = client.get(f"/api/jobs/{queued.id}/preview/event/2?event=nope")
    assert missing.status_code == 409


def test_a_coverage_only_change_queues_without_touching_any_line(
        client_factory, tmp_path):
    client = client_factory()
    queued, job = planned_client(client, tmp_path)
    data = client.get(f"/api/jobs/{queued.id}/review").json()
    response = client.post(f"/api/jobs/{queued.id}/review", json={
        "events": [{"event": "event-1", "decision": "omit"}],
        "base_revision": data["revision"]})
    assert response.status_code == 200
    body = response.json()
    assert body["rerun"]["coverage"] == 1
    assert body["rerun"]["generating"] == 0 and body["rerun"]["processing"] == 0
    overrides = body["job"]["overrides"]
    assert overrides["coverage.events"]["event-1"]["decision"] == "omit"
    assert "dub.line_edits" in overrides and overrides["dub.line_edits"] == {}


def test_a_replacement_sound_outside_the_configured_directories_is_refused(
        client_factory, tmp_path):
    client = client_factory()
    queued, job = planned_client(client, tmp_path)
    data = client.get(f"/api/jobs/{queued.id}/review").json()
    response = client.post(f"/api/jobs/{queued.id}/review", json={
        "events": [{"event": "event-1", "decision": "replace",
                    "asset": str(tmp_path / "elsewhere.wav")}],
        "base_revision": data["revision"]})
    assert response.status_code == 422
    assert "work or output directories" in response.json()["error"]


def test_an_unknown_event_is_rejected_rather_than_created(client_factory, tmp_path):
    client = client_factory()
    queued, job = planned_client(client, tmp_path)
    data = client.get(f"/api/jobs/{queued.id}/review").json()
    response = client.post(f"/api/jobs/{queued.id}/review", json={
        "events": [{"event": "invented", "decision": "retain"}],
        "base_revision": data["revision"]})
    assert response.status_code == 404


def test_a_timing_edit_re_renders_without_generating_speech(client_factory, tmp_path):
    client = client_factory()
    queued, job = planned_client(client, tmp_path)
    data = client.get(f"/api/jobs/{queued.id}/review").json()
    row = data["segments"][1]
    response = client.post(f"/api/jobs/{queued.id}/review", json={
        "edits": [{"index": 1, "cue": row["cue"]["cue_id"],
                   "anchors": [{"phrase": f'{row["cue"]["cue_id"]}:p1',
                                "edge": "start", "at": 0.6}],
                   "pauses": [{"pause": f'{row["cue"]["cue_id"]}:g0',
                               "protected": False, "kind": "padding"}]}],
        "base_revision": data["revision"]})
    assert response.status_code == 200
    plan = response.json()["rerun"]
    assert plan["generating"] == 0 and plan["processing"] == 1
    assert plan["lines"][0]["work"] == "re-renders existing audio"
    edit = response.json()["job"]["overrides"]["dub.line_edits"]["1"]
    assert edit["anchors"][0]["at"] == pytest.approx(0.6)
    assert edit["pauses"] == {f'{row["cue"]["cue_id"]}:g0':
                              {"protected": False, "kind": "padding"}}


def test_an_anchor_outside_the_line_window_is_refused_at_the_edge(
        client_factory, tmp_path):
    client = client_factory()
    queued, job = planned_client(client, tmp_path)
    data = client.get(f"/api/jobs/{queued.id}/review").json()
    row = data["segments"][1]
    response = client.post(f"/api/jobs/{queued.id}/review", json={
        "edits": [{"index": 1, "cue": row["cue"]["cue_id"],
                   "anchors": [{"phrase": f'{row["cue"]["cue_id"]}:p1',
                                "edge": "start", "at": 90.0}]}],
        "base_revision": data["revision"]})
    assert response.status_code == 422


def test_an_empty_submission_is_refused_rather_than_queued(client_factory, tmp_path):
    client = client_factory()
    queued, job = planned_client(client, tmp_path)
    data = client.get(f"/api/jobs/{queued.id}/review").json()
    response = client.post(f"/api/jobs/{queued.id}/review",
                           json={"base_revision": data["revision"]})
    assert response.status_code == 422


def test_a_timing_edit_makes_an_earlier_verdict_stale(tmp_path):
    from doblarr.cues import TimingPlan
    from doblarr.review import snapshot_revision

    job = exchange(tmp_path / "work", tmp_path)
    before = snapshot_revision(job)
    job.segments[1].phrasing = TimingPlan(mode="phrase", state="applied",
                                          inputs="phrase-xyz")
    assert snapshot_revision(job) != before


def test_a_coverage_change_also_moves_the_snapshot_revision(tmp_path):
    from doblarr.cues import NonverbalEvent
    from doblarr.review import snapshot_revision

    job = exchange(tmp_path / "work", tmp_path)
    before = snapshot_revision(job)
    job.nonverbal = [NonverbalEvent(event_id="e1", type="laugh", category="vocal",
                                    decision="retain", coverage="retained",
                                    inputs="ev-1")]
    assert snapshot_revision(job) != before


def test_a_verdict_about_a_reaction_is_filed_under_the_event_not_a_cue(
        client_factory, tmp_path):
    client = client_factory()
    queued, job = planned_client(client, tmp_path)
    data = client.get(f"/api/jobs/{queued.id}/review").json()
    response = client.post(f"/api/jobs/{queued.id}/decisions", json={
        "event": "event-1", "base_revision": data["revision"],
        "note": "the laugh is a beat late but it is the right laugh"})
    assert response.status_code == 200 and response.json()["scope"] == "event"
    stored = load_decisions(client.app.state.worker.config.work_dir, queued.id)
    assert "event-1" in stored["events"] and "event-1" not in stored["cues"]
    # And it comes back on the event, marked against the revision it was made on.
    again = client.get(f"/api/jobs/{queued.id}/review").json()
    decision = again["nonverbal"][0]["decision"]
    assert decision["note"].startswith("the laugh is a beat late")
    assert decision["stale"] is False


def test_an_event_verdict_goes_stale_when_the_audio_is_re_rendered(
        client_factory, tmp_path):
    client = client_factory()
    queued, job = planned_client(client, tmp_path)
    data = client.get(f"/api/jobs/{queued.id}/review").json()
    client.post(f"/api/jobs/{queued.id}/decisions", json={
        "event": "event-1", "base_revision": data["revision"], "note": "fine"})
    # The run changes: a different coverage decision, so a different snapshot.
    job.nonverbal[0].coverage = "omitted"
    job.nonverbal[0].inputs = "ev-2"
    write_review(job, client.app.state.worker.config.work_dir)
    again = client.get(f"/api/jobs/{queued.id}/review").json()
    assert again["revision"] != data["revision"]
    assert again["nonverbal"][0]["decision"]["stale"] is True


def test_a_decision_must_name_exactly_one_subject(client_factory, tmp_path):
    client = client_factory()
    queued, job = planned_client(client, tmp_path)
    data = client.get(f"/api/jobs/{queued.id}/review").json()
    for body in ({"base_revision": data["revision"]},
                 {"cue": "cue-a", "event": "event-1",
                  "base_revision": data["revision"]}):
        response = client.post(f"/api/jobs/{queued.id}/decisions", json=body)
        assert response.status_code == 422


def test_an_unknown_event_verdict_is_refused(client_factory, tmp_path):
    client = client_factory()
    queued, job = planned_client(client, tmp_path)
    data = client.get(f"/api/jobs/{queued.id}/review").json()
    response = client.post(f"/api/jobs/{queued.id}/decisions", json={
        "event": "invented", "base_revision": data["revision"], "note": "x"})
    assert response.status_code == 404
    assert "event is not part of this review" in response.json()["error"]
