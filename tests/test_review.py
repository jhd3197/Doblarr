import json

from doblarr.models import DubJob, Segment, Speaker
from doblarr.review import write_review
from doblarr.telemetry import RunReport
from doblarr.voices import character_cast, save_characters


def review_job(client, tmp_path):
    work = client.app.state.worker.config.work_dir
    raw = work / "raw.wav"
    work.mkdir(parents=True, exist_ok=True)
    raw.write_bytes(b"original-audio")
    job = DubJob(tmp_path / "movie.mkv", "en", "es")
    job.segments = [
        Segment(
            0, 1, 3, "hello", text_translated="hola", audio_clip=raw, issues=["timing_overflow"]
        )
    ]
    RunReport(job, work)
    write_review(job, work)
    queued = client.app.state.jobs.add(
        title="Film",
        source="manual",
        source_lang="en",
        target_lang="es",
        input_file=str(job.input_file),
        status="done",
        review_file=str(job.review_file),
        report_file=str(job.report_file),
    )
    return queued, job, raw


def test_review_edit_creates_selective_job_without_mutating_snapshot(client_factory, tmp_path):
    client = client_factory()
    queued, job, raw = review_job(client, tmp_path)
    response = client.get(f"/api/jobs/{queued.id}/review")
    assert response.status_code == 200
    assert "audio_clip" not in response.json()["segments"][0]
    before = job.review_file.read_bytes()
    response = client.post(
        f"/api/jobs/{queued.id}/review",
        json={"edits": [{"index": 0, "text": "buenas", "regenerate": True}]},
    )
    assert response.status_code == 200
    updated = response.json()["job"]
    assert updated["id"] != queued.id
    assert updated["overrides"]["dub.line_edits"]["0"]["revision"] == 1
    assert updated["overrides"]["dub.line_edits"]["0"]["text"] == "buenas"
    assert job.review_file.read_bytes() == before
    replacement = raw.with_suffix(".partial.wav")
    replacement.write_bytes(b"new-audio")
    replacement.replace(raw)
    audio = client.get(f"/api/jobs/{queued.id}/clips/0", headers={"Range": "bytes=0-7"})
    assert audio.status_code == 206 and audio.content == b"original"


def test_review_rejects_invalid_edits_and_running_jobs(client_factory, tmp_path):
    client = client_factory()
    queued, _, _ = review_job(client, tmp_path)
    for edits in [
        [{"index": 999, "text": "x"}],
        [{"index": 0, "start": 4, "end": 3}],
        [{"index": 0, "text": " "}],
        [{"index": 0}, {"index": 0}],
    ]:
        assert (
            client.post(f"/api/jobs/{queued.id}/review", json={"edits": edits}).status_code == 422
        )
    client.app.state.jobs.update(queued.id, status="running")
    assert (
        client.post(
            f"/api/jobs/{queued.id}/review", json={"edits": [{"index": 0, "regenerate": True}]}
        ).status_code
        == 409
    )


def test_review_guards_paths(client_factory, tmp_path):
    client = client_factory()
    queued, job, _ = review_job(client, tmp_path)
    data = json.loads(job.review_file.read_text())
    data["segments"][0]["audio_clip"] = str(tmp_path / "outside.wav")
    job.review_file.write_text(json.dumps(data))
    assert client.get(f"/api/jobs/{queued.id}/clips/0").status_code == 403


def test_character_reuse_requires_explicit_mapping(client_factory, tmp_path):
    client = client_factory()
    db = client.app.state.jobs.db
    job = DubJob(tmp_path / "episode.mkv", "ja", "es")
    job.speakers = {"SPEAKER_00": Speaker("SPEAKER_00", voicebox_profile_id="ginko")}
    save_characters(job, db, "Mushishi", {"SPEAKER_00": "Ginko"})
    job.speakers = {"SPEAKER_03": Speaker("SPEAKER_03")}
    assert not character_cast(job, db, "Mushishi", {})
    cast = character_cast(job, db, "Mushishi", {"SPEAKER_03": "Ginko"})
    assert cast[0]["speaker_id"] == "SPEAKER_03" and cast[0]["voice"] == "ginko"
