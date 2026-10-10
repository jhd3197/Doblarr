"""A real analysis run over the tone fixture: snapshot, features, reuse and no speech."""

import json
import shutil
from pathlib import Path

import pytest

from doblarr import benchmarks, snapshots
from doblarr.config import Config
from doblarr.models import DubJob
from doblarr.pipeline import run_job
from doblarr.services import Services
from doblarr.store import Database

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")


def analyse(root, db, media, subtitles, overrides=None):
    config = Config.load(root / "config.yaml").with_overrides({
        "paths.work_dir": str(root / "work"),
        "paths.output_dir": str(root / "output"),
        "transcribe.diarize": False,
        "translate.provider": "passthrough",
        **(overrides or {}),
    })
    tone = benchmarks.ToneEngine(root)
    services = Services(config)
    services._cache["speech"] = tone
    job = DubJob(input_file=media, source_lang="ja", target_lang="es", kind="analyze",
                 subtitle_file=subtitles)
    run_job(job, config, services=services, db=db)
    return job, tone


def test_an_analysis_records_every_stage_measures_lines_and_speaks_no_words(tmp_path,
                                                                           monkeypatch):
    from doblarr.stages import transcribe

    monkeypatch.setattr(transcribe, "faster_whisper_segments", lambda *a, **k: [])
    db = Database(tmp_path / "d.db")
    media, subtitles = benchmarks.write_media(tmp_path / "media", benchmarks.SCENE)
    job, tone = analyse(tmp_path, db, media, subtitles)
    assert tone.requests == []                     # nothing generated, nothing cloned
    ident = job.metrics["identity"]
    assert ident["revision_id"].startswith("rev-c")
    snap = snapshots.get(db, ident["revision_id"], "ja")
    states = {k: v["state"] for k, v in snap["stages"].items()}
    for stage in ("probe", "transcribe", "diarize", "measure", "baselines", "analyze",
                  "features", "speaker_memory"):
        assert states[stage] == "done", stage
    script = Path(snap["stages"]["transcribe"]["outputs"]["script"])
    assert script.is_file()
    found = json.loads(Path(job.metrics["features"]["path"]).read_text(encoding="utf-8"))
    assert len(found["lines"]) == len(job.segments)
    assert found["contract"]["units"] == "dBFS-rms-sample"
    assert all(line["quality"] in ("ok", "insufficient", "contaminated", "missing")
               for line in found["lines"])
    assert job.metrics["features"]["reused"] is False
    # Running again reuses what did not change.
    again, tone2 = analyse(tmp_path, db, media, subtitles)
    assert again.metrics["features"]["reused"] is True and tone2.requests == []
    # A restored run records the same inputs: nothing it reused looks changed.
    after = snapshots.get(db, ident["revision_id"], "ja")["stages"]
    assert not [k for k, v in after.items() if v["state"] == "stale"]
    db.close()


def test_turning_features_off_marks_the_stage_skipped_not_done(tmp_path, monkeypatch):
    from doblarr.stages import transcribe

    monkeypatch.setattr(transcribe, "faster_whisper_segments", lambda *a, **k: [])
    db = Database(tmp_path / "d.db")
    media, subtitles = benchmarks.write_media(tmp_path / "media", benchmarks.SCENE)
    job, _ = analyse(tmp_path, db, media, subtitles, {"analysis.features": False})
    snap = snapshots.get(db, job.metrics["identity"]["revision_id"], "ja")
    assert snap["stages"]["features"]["state"] == "skipped"
    db.close()
