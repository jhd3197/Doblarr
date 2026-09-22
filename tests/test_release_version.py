"""Release numbering must handle first publication, retries, and dev isolation."""

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / ".github/scripts/release_version.py"
spec = importlib.util.spec_from_file_location("release_version", SCRIPT)
assert spec and spec.loader
release_version = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release_version)


@pytest.mark.parametrize("branch", ["main", "master"])
def test_first_release_and_patch_bump(branch):
    resolve = release_version.resolve_version
    assert resolve([], [], "0.1.0", branch, "1") == "0.1.0"
    assert resolve(
        ["v0.1.9", "v0.1.10", "v9.0.0.dev3", "other"], [], "0.1.0", branch, "2"
    ) == "0.1.11"


def test_retry_reuses_stable_tag_on_same_commit():
    assert release_version.resolve_version(
        ["v0.1.0", "v0.1.1"], ["v0.1.1"], "0.1.0", "master", "7"
    ) == "0.1.1"


def test_dev_does_not_reuse_stable_version():
    assert release_version.resolve_version(
        ["v0.1.0"], ["v0.1.0"], "0.1.0", "dev", "12"
    ) == "0.1.1.dev12"
    assert release_version.resolve_version([], [], "0.1.0", "dev", "1") == "0.1.0.dev1"


def test_retry_cannot_roll_latest_back_across_stable_branches():
    with pytest.raises(ValueError, match="older stable release"):
        release_version.resolve_version(
            ["v0.1.0", "v0.1.1"], ["v0.1.0"], "0.1.0", "main", "7"
        )


@pytest.mark.parametrize(
    ("initial", "branch", "run"),
    [("invalid", "master", "1"), ("0.1.0", "feature", "1"), ("0.1.0", "dev", "0")],
)
def test_invalid_version_inputs_fail(initial, branch, run):
    with pytest.raises(ValueError):
        release_version.resolve_version([], [], initial, branch, run)


def test_runner_stamps_version_and_outputs_without_git_mutations(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "doblarr").mkdir()
    source = tmp_path / "doblarr/__init__.py"
    source.write_text('__version__ = "0.1.0"\n', encoding="utf-8")
    output = tmp_path / "outputs"
    monkeypatch.setenv("GITHUB_REF_NAME", "master")
    monkeypatch.setenv("GITHUB_RUN_NUMBER", "4")
    monkeypatch.setenv("GITHUB_REPOSITORY", "jhd3197/Doblarr")
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setattr(
        release_version, "git_tags", lambda *args: ["v0.1.0"] if args == ("--list",) else []
    )
    release_version.main()
    assert source.read_text(encoding="utf-8") == '__version__ = "0.1.1"\n'
    assert output.read_text(encoding="utf-8").splitlines() == [
        "version=0.1.1", "tag=v0.1.1", "channel=latest", "image=ghcr.io/jhd3197/doblarr"
    ]
