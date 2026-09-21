import json

import pytest

from doblarr.errors import JobCancelled
from doblarr.models import DubJob
from doblarr.telemetry import RunReport


def test_report_records_work_and_failure(tmp_path):
    job = DubJob(tmp_path / "movie.mkv", "en", "es")
    report = RunReport(job, tmp_path)
    with report.stage("extract"):
        job.metrics["stage_cache_hits"] = 1
    with pytest.raises(JobCancelled), report.stage("synthesize"):
        raise JobCancelled("stop")
    result = json.loads(job.report_file.read_text())
    assert result["status"] == "cancelled"
    assert result["stages"][0]["status"] == "done"
    assert result["stages"][1]["status"] == "cancelled"
    assert result["stages"][0]["seconds"] >= 0
    assert result["counters"]["stage_cache_hits"] == 1


def test_each_run_has_its_own_report(tmp_path):
    job = DubJob(tmp_path / "movie.mkv", "en", "es")
    first = RunReport(job, tmp_path)
    second = RunReport(job, tmp_path)
    assert first.path != second.path
