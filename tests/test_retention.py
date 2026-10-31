"""Trimming regenerable work output, and refusing a write the disk cannot hold."""

import os
import time

import pytest

from doblarr import retention
from doblarr.config import Config


def _aged(path, days_ago: float, folder: bool = True):
    path.mkdir(parents=True) if folder else path.write_text("{}", encoding="utf-8")
    when = time.time() - days_ago * retention.DAY
    os.utime(path, (when, when))
    return path


def test_the_newest_are_kept_and_protected_paths_survive(tmp_path):
    old = [_aged(tmp_path / f"r{n}.json", n, folder=False) for n in range(5)]
    assert retention.prune(old, keep=2, protected={old[4]}) == 2
    assert sorted(p.name for p in tmp_path.iterdir()) == ["r0.json", "r1.json", "r4.json"]


def test_recent_folders_are_kept_whatever_the_count(tmp_path):
    folders = [_aged(tmp_path / f"e{n}", n) for n in range(4)]
    retention.prune(folders, keep=1, older_than_days=14)
    assert len(list(tmp_path.iterdir())) == 4


def test_tidy_trims_watch_copies(tmp_path):
    config = Config.load(tmp_path / "c.yaml").with_overrides({
        "paths.work_dir": str(tmp_path / "work"), "retention.watch_copies": 2})
    for n in range(4):
        _aged(tmp_path / "work" / "cache" / "watch" / f"w{n}", n)
    retention.tidy(config)
    assert sorted(p.name for p in (tmp_path / "work/cache/watch").iterdir()) == ["w0", "w1"]


def test_versions_are_kept_unless_asked(tmp_path):
    versions = [_aged(tmp_path / f"v{n}", n) for n in range(3)]
    retention.trim_versions(tmp_path, keep=0)
    assert all(v.exists() for v in versions)
    retention.trim_versions(tmp_path, keep=1, current=versions[2])
    assert sorted(p.name for p in tmp_path.iterdir()) == ["v0", "v2"]


def test_a_write_the_disk_cannot_hold_is_refused(tmp_path):
    with pytest.raises(retention.DiskFull, match="free some space"):
        retention.require_space(tmp_path / "not-yet" / "out", 10 ** 18)
    retention.require_space(tmp_path, 1)
