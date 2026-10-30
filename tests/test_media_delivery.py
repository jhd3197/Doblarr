"""The path guard in front of every media download, and the Range responses."""

import os
from types import SimpleNamespace

import pytest

from doblarr.routes.media_delivery import allowed_path, ranged_response


@pytest.fixture
def roots(tmp_path):
    work, out, elsewhere = tmp_path / "work", tmp_path / "out", tmp_path / "elsewhere"
    for folder in (work, out, elsewhere):
        folder.mkdir()
    return SimpleNamespace(work=work, out=out, elsewhere=elsewhere,
                           config=SimpleNamespace(work_dir=work, output_dir=out))


def test_inside_either_root_is_allowed(roots):
    for folder in (roots.work, roots.out):
        clip = folder / "harbor-lights.wav"
        clip.write_bytes(b"x")
        assert allowed_path(roots.config, str(clip)) == clip.resolve()


def test_outside_empty_and_escaping_paths_are_refused(roots):
    secret = roots.elsewhere / "notes.txt"
    secret.write_text("private")
    assert allowed_path(roots.config, str(secret)) is None
    assert allowed_path(roots.config, "") is None
    escape = roots.work / ".." / "elsewhere" / "notes.txt"
    assert allowed_path(roots.config, str(escape)) is None


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlinks unavailable")
def test_symlink_out_of_the_roots_is_refused(roots):
    secret = roots.elsewhere / "notes.txt"
    secret.write_text("private")
    link = roots.work / "mina.wav"
    try:
        link.symlink_to(secret)
    except OSError:
        pytest.skip("symlinks not permitted here")
    assert allowed_path(roots.config, str(link)) is None


def test_guard_follows_a_changed_setting(roots):
    clip = roots.elsewhere / "sora.wav"
    clip.write_bytes(b"x")
    assert allowed_path(roots.config, str(clip)) is None
    roots.config.output_dir = roots.elsewhere
    assert allowed_path(roots.config, str(clip)) == clip.resolve()


@pytest.fixture
def clip(tmp_path):
    path = tmp_path / "kaito.wav"
    path.write_bytes(bytes(range(100)))
    return path


@pytest.mark.parametrize("header", [
    "bytes=-0",       # an empty suffix
    "bytes=100-",     # start at the end of a 100-byte file
    "bytes=500-600",  # start past the end
    "bytes=9-3",      # start after end
    "bytes=-",        # neither bound
    "items=0-5",      # wrong unit
])
def test_unsatisfiable_ranges_are_416(clip, header):
    response = ranged_response(clip, header)
    assert response.status_code == 416
    assert response.headers["content-range"] == "bytes */100"


def test_valid_ranges_are_206(clip):
    assert ranged_response(clip, "bytes=10-19").headers["content-range"] == "bytes 10-19/100"
    assert ranged_response(clip, "bytes=-5").headers["content-range"] == "bytes 95-99/100"
    assert ranged_response(clip, "bytes=90-").headers["content-range"] == "bytes 90-99/100"
    # An end past the file is clamped, not refused.
    assert ranged_response(clip, "bytes=90-500").headers["content-range"] == "bytes 90-99/100"
    assert ranged_response(clip, None).status_code == 200
