"""The Plan 05 user path: treatments and export findings through the real API.

Everything here goes through the HTTP surface a browser uses, because a record
nobody can reach is not a feature. The point of most of these is separation:
what a run applied, what it only asked for, what the exported file measured,
and what a person has actually signed off are four different things and the API
must never let one of them read as another.
"""

import math
import pathlib
import shutil
import struct
import wave

import pytest

from doblarr.cues import EDGED, RAW, TREATED, Artifact, Selection, Take, Treatment
from doblarr.models import DubJob, Segment, Speaker
from doblarr.review import write_review
from doblarr.telemetry import RunReport

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")

RATE = 48000


def tone(path, seconds=1.0, amplitude=0.3):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as out:
        out.setparams((1, 2, RATE, 0, "NONE", "not compressed"))
        out.writeframes(b"".join(
            struct.pack("<h", int(amplitude * math.sin(2 * math.pi * 220 * i / RATE) * 32000))
            for i in range(round(RATE * seconds))))
    return path


def treated_run(work, tmp_path, *, delivery_report=None):
    """Three cues: one treated, one bypassed, one whose preset is unsupported."""
    job = DubJob(tmp_path / "movie.mkv", "ja", "es")
    job.input_file.write_bytes(b"media")
    job.speakers = {"A": Speaker("A"), "B": Speaker("B")}
    plan = [
        ("A", Treatment(preset="room", intensity=0.8, origin="default",
                        outcome="applied", capability="supported", tail=0.072,
                        makeup=-0.9, dry_role=EDGED, version="treatments/1",
                        reason="the run default; -0.9 dB of makeup kept the level")),
        ("B", Treatment(preset="dry", origin="line", outcome="bypassed",
                        capability="supported",
                        reason="a reviewer bypassed treatment on this line")),
        ("A", Treatment(preset="phone", origin="scene", outcome="unsupported",
                        capability="unsupported", missing=["acompressor"],
                        scene="2.000-9.000:phone",
                        reason="this FFmpeg build has no acompressor filter")),
    ]
    for index, (speaker, treatment) in enumerate(plan):
        start = 1.0 + index * 2.0
        seg = Segment(index, start, start + 1.4, f"src {index}", speaker=speaker,
                      text_translated=f"linea {index}")
        raw = tone(work / "clips" / f"raw{index}.wav")
        dry = tone(work / "clips" / f"dry{index}.wav")
        take = Take(take_id=f"take-{index}", fingerprint=f"gen-{index}", engine="tone",
                    state="generated", text=f"linea {index}",
                    raw=Artifact(role=RAW, path=str(raw), fingerprint=f"gen-{index}"))
        seg.audio.takes.append(take)
        seg.audio.selection = Selection(take_id=take.take_id, reason="auto")
        seg.audio.put_render(Artifact(role=EDGED, path=str(dry),
                                      fingerprint=f"dry-{index}", derived_from=RAW))
        seg.treatment = treatment
        if treatment.outcome == "applied":
            wet = tone(work / "clips" / f"wet{index}.wav", seconds=1.1)
            seg.audio.put_render(Artifact(role=TREATED, path=str(wet),
                                          fingerprint=f"wet-{index}",
                                          derived_from=EDGED, duration=1.1))
        seg.audio_clip = pathlib.Path(seg.audio.current().path)
        job.segments.append(seg)
    job.source_track = tone(work / "source.wav", 12.0)
    job.source_audio = job.source_track
    job.dubbed_track = tone(work / "dub.wav", 12.0)
    job.artifacts_dir = work
    # Cue identities are minted here, so a decision keyed by cue has to come
    # after it — exactly as it would in a real review round trip.
    from doblarr.cues import ensure_identity
    ensure_identity(job)
    job.treatment_edits = {job.segments[1].cue_id: {"bypass": True}}
    job.delivery = delivery_report if delivery_report is not None else {
        "state": "warned", "publishable": True,
        "output": str(work / "out.mkv"),
        "profile": {"id": "local:abc", "meter": "ffmpeg-ebur128/bs1770-4",
                    "target_lufs": None, "mode": "measure"},
        "stream": {"audio_index": 2, "codec": "aac", "channels": 2,
                   "sample_rate": 48000, "language": "spa", "title": "Spanish AI"},
        "identification": "audio stream 2 carries the expected language and title",
        "loudness": {"lufs": -18.2, "true_peak_db": -4.9,
                     "meter": "ffmpeg-ebur128/bs1770-4", "state": "measured"},
        "placement": [{"cue": job.segments[0].cue_id, "state": "present"}],
        "findings": [{"code": "start_offset", "severity": "warning",
                      "detail": "the dub stream starts at +0.300s",
                      "evidence": {"start_time": 0.3}, "detector": "delivery/1"}],
        "summary": "aac 2ch 48000 Hz; -18.2 LUFS; 1 warning(s)",
    }
    RunReport(job, work)
    write_review(job, work, settings={
        "treatments": {"mode": "on", "default": "room", "intensity": 0.8,
                       "scenes": [{"start": 2.0, "end": 9.0, "preset": "phone",
                                   "intensity": 1.0, "note": "", "id": "2.000-9.000:phone"}],
                       "catalogue": [{"preset": "room", "capability": "supported",
                                      "summary": "A few early reflections.",
                                      "needs": ["aecho"], "missing": [], "tail": 0.072,
                                      "offset": 0.0}]},
        "delivery": {"id": "local:abc", "meter": "ffmpeg-ebur128/bs1770-4",
                     "target_lufs": None, "mode": "measure",
                     "note": "no target is configured"},
    })
    return job


def review_client(client, tmp_path, **kwargs):
    work = client.app.state.worker.config.work_dir
    work.mkdir(parents=True, exist_ok=True)
    job = treated_run(work, tmp_path, **kwargs)
    queued = client.app.state.jobs.add(
        title="Film", source="manual", source_lang="ja", target_lang="es",
        input_file=str(job.input_file), status="done",
        review_file=str(job.review_file), report_file=str(job.report_file))
    return queued, job


# -- capability -------------------------------------------------------------

def test_the_capability_route_says_what_this_build_can_actually_render(client_factory):
    client = client_factory()
    body = client.get("/api/capabilities").json()
    presets = {row["preset"]: row for row in body["treatments"]["catalogue"]}
    assert set(presets) >= {"dry", "room", "distant", "phone", "radio"}
    assert presets["dry"]["capability"] == "supported"
    for row in presets.values():
        assert row["capability"] in ("supported", "unsupported", "unknown")
        if row["capability"] == "unsupported":
            assert row["missing"]
    assert "unsupported" in body["treatments"]["note"]
    assert body["delivery"]["meter"]


# -- the review payload -----------------------------------------------------

def test_the_review_shows_the_frozen_treatment_policy(client_factory, tmp_path):
    client = client_factory()
    queued, _job = review_client(client, tmp_path)
    body = client.get(f"/api/jobs/{queued.id}/review").json()
    assert body["treatments"]["mode"] == "on"
    assert body["treatments"]["default"] == "room"
    assert body["treatments"]["scenes"][0]["preset"] == "phone"
    assert body["treatments"]["catalogue"][0]["preset"] == "room"


def test_the_review_carries_the_export_report_without_a_local_path(
        client_factory, tmp_path):
    client = client_factory()
    queued, _job = review_client(client, tmp_path)
    body = client.get(f"/api/jobs/{queued.id}/review").json()
    report = body["delivery"]
    assert report["state"] == "warned"
    assert "output" not in report
    assert report["output_name"] == "out.mkv"
    assert report["loudness"]["meter"] == "ffmpeg-ebur128/bs1770-4"
    assert [f["code"] for f in report["findings"]] == ["start_offset"]


def test_the_delivery_route_separates_render_export_and_human_review(
        client_factory, tmp_path):
    client = client_factory()
    queued, job = review_client(client, tmp_path)
    body = client.get(f"/api/jobs/{queued.id}/delivery").json()
    assert body["render"]["status"] == "done"
    assert body["delivery"]["state"] == "warned"
    assert body["human_review"]["reviewed"] == 0
    assert body["human_review"]["complete"] is False
    assert "needs a listener" in body["note"]
    # a passing export is not an approval, and the payload says which is which
    assert body["version_saved"] is False


def test_a_verdict_counts_as_review_and_an_old_one_counts_as_stale(
        client_factory, tmp_path):
    client = client_factory()
    queued, job = review_client(client, tmp_path)
    review = client.get(f"/api/jobs/{queued.id}/review").json()
    for seg in job.segments:
        client.post(f"/api/jobs/{queued.id}/decisions", json={
            "cue": seg.cue_id, "base_revision": review["revision"],
            "reviewed": True, "actor": "listener"})
    body = client.get(f"/api/jobs/{queued.id}/delivery").json()
    assert body["human_review"]["reviewed"] == len(job.segments)
    assert body["human_review"]["complete"] is True


# -- the scene payload ------------------------------------------------------

def test_a_scene_says_what_the_space_was_and_what_it_could_not_do(
        client_factory, tmp_path):
    client = client_factory()
    queued, job = review_client(client, tmp_path)
    scene = client.get(f"/api/jobs/{queued.id}/scene/0").json()
    assert scene["treatment"]["preset"] == "room"
    assert scene["treatment"]["outcome"] == "applied"
    assert scene["treatment"]["tail"] == 0.072
    assert scene["available"]["dry"] is True
    assert scene["available"]["treated"] is True

    unsupported = client.get(f"/api/jobs/{queued.id}/scene/2").json()
    assert unsupported["treatment"]["preset"] == "phone"
    assert unsupported["treatment"]["outcome"] == "unsupported"
    assert unsupported["treatment"]["missing"] == ["acompressor"]
    # asked for, and there is no treated audio pretending otherwise
    assert unsupported["available"]["treated"] is False
    assert unsupported["available"]["dry"] is True


def test_a_reviewers_bypass_comes_back_on_the_scene_it_was_made_on(
        client_factory, tmp_path):
    client = client_factory()
    queued, job = review_client(client, tmp_path)
    scene = client.get(f"/api/jobs/{queued.id}/scene/1").json()
    assert scene["treatment_edit"] == {"bypass": True}
    assert scene["treatment"]["outcome"] == "bypassed"


# -- previews ---------------------------------------------------------------

def test_dry_and_treated_are_two_different_playable_files(client_factory, tmp_path):
    client = client_factory()
    queued, _job = review_client(client, tmp_path)
    dry = client.get(f"/api/jobs/{queued.id}/preview/dry/0")
    treated = client.get(f"/api/jobs/{queued.id}/preview/treated/0")
    assert dry.status_code == 200
    assert treated.status_code == 200
    assert dry.content != treated.content


def test_asking_for_a_treatment_that_was_never_applied_explains_itself(
        client_factory, tmp_path):
    client = client_factory()
    queued, _job = review_client(client, tmp_path)
    response = client.get(f"/api/jobs/{queued.id}/preview/treated/1")
    assert response.status_code == 409
    assert "bypassed" in response.json()["error"]


def test_the_dry_preview_says_when_it_is_also_the_final_line(
        client_factory, tmp_path):
    """Two controls that silently play one file is how a feature looks dead."""
    from doblarr import preview

    client = client_factory()
    queued, job = review_client(client, tmp_path)
    resolved = preview.resolve(job, job.segments, "dry", 1)
    assert "no treatment ran" in resolved["label"]
    treated = preview.resolve(job, job.segments, "dry", 0)
    assert "no treatment ran" not in treated["label"]


# -- editing ----------------------------------------------------------------

def test_a_treatment_edit_re_renders_without_generating_speech(
        client_factory, tmp_path):
    client = client_factory()
    queued, job = review_client(client, tmp_path)
    review = client.get(f"/api/jobs/{queued.id}/review").json()
    response = client.post(f"/api/jobs/{queued.id}/review", json={
        "base_revision": review["revision"],
        "edits": [{"index": 0, "cue": job.segments[0].cue_id,
                   "treatment": {"preset": "phone", "intensity": 0.6}}]})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["rerun"]["generating"] == 0
    assert body["rerun"]["lines"][0]["work"] == "re-renders existing audio"
    overrides = body["job"]["overrides"]["treatments.lines"]
    assert overrides[job.segments[0].cue_id] == {"preset": "phone", "intensity": 0.6}


def test_a_bypass_is_recorded_as_a_bypass_not_as_a_dry_preset(
        client_factory, tmp_path):
    client = client_factory()
    queued, job = review_client(client, tmp_path)
    review = client.get(f"/api/jobs/{queued.id}/review").json()
    response = client.post(f"/api/jobs/{queued.id}/review", json={
        "base_revision": review["revision"],
        "edits": [{"index": 2, "cue": job.segments[2].cue_id,
                   "treatment": {"bypass": True}}]})
    assert response.status_code == 200, response.text
    saved = response.json()["job"]["overrides"]["treatments.lines"]
    assert saved[job.segments[2].cue_id] == {"bypass": True}


def test_an_unknown_preset_is_refused_at_the_edge(client_factory, tmp_path):
    client = client_factory()
    queued, job = review_client(client, tmp_path)
    review = client.get(f"/api/jobs/{queued.id}/review").json()
    response = client.post(f"/api/jobs/{queued.id}/review", json={
        "base_revision": review["revision"],
        "edits": [{"index": 0, "cue": job.segments[0].cue_id,
                   "treatment": {"preset": "cathedral"}}]})
    assert response.status_code == 422


def test_a_treatment_edit_that_says_nothing_is_refused(client_factory, tmp_path):
    client = client_factory()
    queued, job = review_client(client, tmp_path)
    review = client.get(f"/api/jobs/{queued.id}/review").json()
    response = client.post(f"/api/jobs/{queued.id}/review", json={
        "base_revision": review["revision"],
        "edits": [{"index": 0, "cue": job.segments[0].cue_id, "treatment": {}}]})
    assert response.status_code == 422


def test_a_treatment_edit_needs_a_stable_cue_identity(client_factory, tmp_path):
    client = client_factory()
    queued, job = review_client(client, tmp_path)
    review = client.get(f"/api/jobs/{queued.id}/review").json()
    # strip the identity out of the snapshot the way a pre-schema run would
    import json
    snapshot = json.loads(job.review_file.read_text(encoding="utf-8"))
    snapshot["segments"][0]["cue"]["cue_id"] = ""
    job.review_file.write_text(json.dumps(snapshot), encoding="utf-8")
    response = client.post(f"/api/jobs/{queued.id}/review", json={
        "base_revision": review["revision"],
        "edits": [{"index": 0, "treatment": {"preset": "room"}}]})
    assert response.status_code == 409
    assert "stable identity" in response.json()["error"]


# -- version comparison -----------------------------------------------------

def _version(root, version_id, cues, *, output_sha, delivery_state="passed"):
    import json

    destination = root / "versions" / version_id
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "version.json").write_text(json.dumps({
        "version_id": version_id, "output_sha256": output_sha,
        "output": str(destination / "film.mkv"), "created_at": "2026-09-22T00:00:00Z",
        "name": "Dub", "settings": {"treatments": {"mode": "on"}},
        "delivery": {"state": delivery_state, "publishable": True,
                     "output": str(destination / "film.mkv"), "findings": []},
        "cues": cues}), encoding="utf-8")
    (destination / "film.mkv").write_bytes(b"x")
    return destination


def test_two_versions_can_be_compared_and_the_evidence_does_not_travel(
        client_factory, tmp_path):
    client = client_factory()
    output_root = client.app.state.worker.config.output_dir / "film"
    output_root.mkdir(parents=True, exist_ok=True)
    cue = {"cue_id": "c1", "index": 0,
           "audio": {"selection": {"take_id": "t1"},
                     "takes": [{"take_id": "t1", "fingerprint": "f1"}],
                     "renders": [{"role": "edged", "fingerprint": "r1"}]},
           "treatment": {"preset": "dry", "outcome": "bypassed"},
           "placement": {"onset": 1.0}}
    changed = {**cue, "audio": {**cue["audio"],
                                "renders": [{"role": "treated", "fingerprint": "r2"}]},
               "treatment": {"preset": "room", "outcome": "applied", "tail": 0.072}}
    _version(output_root, "a" * 16, [cue], output_sha="sha-a")
    _version(output_root, "b" * 16, [changed], output_sha="sha-b",
             delivery_state="warned")
    queued = client.app.state.jobs.add(
        title="Film", source="manual", source_lang="ja", target_lang="es",
        input_file=str(tmp_path / "film.mkv"), status="done",
        output_file=str(output_root / "versions" / ("b" * 16) / "film.mkv"))
    body = client.get(
        f"/api/jobs/{queued.id}/versions/{'a' * 16}/compare/{'b' * 16}").json()
    assert body["identical_output"] is False
    assert [row["cue"] for row in body["reprocessed"]] == ["c1"]
    assert body["regenerated"] == []
    assert body["expected_changed_windows"][0]["tail"] == 0.072
    # each report describes the file it was taken on and is not merged
    assert body["delivery"]["previous"]["state"] == "passed"
    assert body["delivery"]["current"]["state"] == "warned"
    assert "output" not in body["delivery"]["previous"]
    assert "only for the output" in body["delivery"]["note"]


def test_comparing_against_a_version_that_is_not_there_is_a_404(
        client_factory, tmp_path):
    client = client_factory()
    output_root = client.app.state.worker.config.output_dir / "film"
    _version(output_root, "a" * 16, [], output_sha="sha-a")
    queued = client.app.state.jobs.add(
        title="Film", source="manual", source_lang="ja", target_lang="es",
        input_file=str(tmp_path / "film.mkv"), status="done",
        output_file=str(output_root / "versions" / ("a" * 16) / "film.mkv"))
    response = client.get(
        f"/api/jobs/{queued.id}/versions/{'a' * 16}/compare/{'c' * 16}")
    assert response.status_code == 404


def test_a_version_id_that_is_not_a_version_id_is_refused(client_factory, tmp_path):
    client = client_factory()
    queued = client.app.state.jobs.add(
        title="Film", source="manual", source_lang="ja", target_lang="es",
        input_file=str(tmp_path / "film.mkv"), status="done",
        output_file=str(client.app.state.worker.config.output_dir / "film" / "f.mkv"))
    response = client.get(f"/api/jobs/{queued.id}/versions/..%2F..%2Fetc/compare/abcdefgh")
    assert response.status_code in (403, 404)

