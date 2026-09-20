"""Job output streaming — range requests, traversal guard, has_file flag."""

import pytest

API_KEY = "test-key"
HEADERS = {"X-Api-Key": API_KEY}
CONTENT = b"fake-video-bytes" * 100  # 1600 bytes


@pytest.fixture
def client(tmp_path, client_factory):
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    (out_dir / "film.tease.mkv").write_bytes(CONTENT)
    client = client_factory({"web": {"api_key": API_KEY},
                              "paths": {"output_dir": str(out_dir)}})
    return client, client.app, out_dir


def _job_with_output(app, output_file):
    job = app.state.jobs.add(title="Film", source="t", source_lang="ko",
                             target_lang="en")
    app.state.jobs.update(job.id, status="done", output_file=output_file)
    return job


def test_full_download(client):
    c, app, out_dir = client
    job = _job_with_output(app, str(out_dir / "film.tease.mkv"))
    r = c.get(f"/api/jobs/{job.id}/file", headers=HEADERS)
    assert r.status_code == 200
    assert r.content == CONTENT
    assert r.headers["accept-ranges"] == "bytes"
    assert r.headers["content-type"] == "video/x-matroska"


def test_range_request(client):
    c, app, out_dir = client
    job = _job_with_output(app, str(out_dir / "film.tease.mkv"))
    r = c.get(f"/api/jobs/{job.id}/file",
              headers={**HEADERS, "Range": "bytes=100-199"})
    assert r.status_code == 206
    assert r.headers["content-range"] == f"bytes 100-199/{len(CONTENT)}"
    assert r.content == CONTENT[100:200]
    # suffix range: last 50 bytes
    r = c.get(f"/api/jobs/{job.id}/file",
              headers={**HEADERS, "Range": "bytes=-50"})
    assert r.status_code == 206 and r.content == CONTENT[-50:]
    assert r.headers["content-range"] == f"bytes {len(CONTENT)-50}-{len(CONTENT)-1}/{len(CONTENT)}"
    # unsatisfiable
    r = c.get(f"/api/jobs/{job.id}/file",
              headers={**HEADERS, "Range": f"bytes={len(CONTENT)+10}-"})
    assert r.status_code == 416
    assert r.headers["content-range"] == f"bytes */{len(CONTENT)}"


def test_path_traversal_rejected(client, tmp_path):
    c, app, _ = client
    outside = tmp_path / "secret.mkv"
    outside.write_bytes(b"outside")
    job = _job_with_output(app, str(outside))  # outside output/work dirs
    assert c.get(f"/api/jobs/{job.id}/file", headers=HEADERS).status_code == 403
    # relative traversal out of the output dir
    job2 = _job_with_output(app, str(tmp_path / "out" / ".." / "secret.mkv"))
    assert c.get(f"/api/jobs/{job2.id}/file", headers=HEADERS).status_code == 403


def test_missing_and_absent_files(client, tmp_path):
    c, app, out_dir = client
    job = _job_with_output(app, str(out_dir / "deleted.mkv"))
    assert c.get(f"/api/jobs/{job.id}/file", headers=HEADERS).status_code == 404
    # job exists but has no output (e.g. dry-run plan)
    plain = app.state.jobs.add(title="Dry", source="t", source_lang="ko",
                               target_lang="en")
    assert c.get(f"/api/jobs/{plain.id}/file", headers=HEADERS).status_code == 404
    assert c.get("/api/jobs/nope/file", headers=HEADERS).status_code == 404
    assert c.get(f"/api/jobs/{job.id}/file").status_code == 401


def test_has_file_flag_in_job_list(client, tmp_path):
    c, app, out_dir = client
    _job_with_output(app, str(out_dir / "film.tease.mkv"))
    app.state.jobs.add(title="Planned", source="t", source_lang="ko",
                       target_lang="en")
    jobs = c.get("/api/jobs", headers=HEADERS).json()["jobs"]
    by_title = {j["title"]: j for j in jobs}
    assert by_title["Film"]["has_file"] is True
    assert by_title["Film"]["output_file"].endswith("film.tease.mkv")
    assert by_title["Planned"]["has_file"] is False
    # a job pointing outside the allowed roots never reports has_file
    bad = _job_with_output(app, str(tmp_path / "secret2.mkv"))
    (tmp_path / "secret2.mkv").write_bytes(b"x")
    jobs = {j["id"]: j for j in c.get("/api/jobs", headers=HEADERS).json()["jobs"]}
    assert jobs[bad.id]["has_file"] is False
