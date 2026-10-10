"""Shared isolated API clients; workers start only in explicit lifespan tests."""

import contextlib
import os
import sys
from pathlib import Path

import pytest
import requests
import yaml
from fastapi.testclient import TestClient

# The server's static routes are tested against a stand-in page, so the Python
# suite needs no Node build of the real UI (ui/dist). Set before the import.
os.environ.setdefault("DOBLARR_UI_DIR", str(Path(__file__).parent / "fixtures" / "web"))

from doblarr import hardware  # noqa: E402
from doblarr.clients.base import ArrClient  # noqa: E402
from doblarr.config import Config  # noqa: E402
from doblarr.server import create_app  # noqa: E402

# The offline fixture below hides torch as sys.modules["torch"] = None. Newer
# SciPy inspects that entry while it is first imported (scikit-learn imports
# it to group voices) and fails on the None; importing it first, while torch
# is still only absent, keeps the hiding from reaching it.
with contextlib.suppress(ImportError):
    import sklearn.cluster  # noqa: F401


# Files whose tests render real audio through the pipeline (FFmpeg, ~10 s each).
# `dev.ps1 validate` skips them while watching; `check` and CI run everything.
SLOW_FILES = {
    "test_adaptive_pipeline.py", "test_benchmarks.py", "test_comparison.py",
    "test_generation_integration.py", "test_plan03_integration.py",
    "test_plan04_integration.py", "test_plan05_integration.py", "test_review_workflow.py",
}


def pytest_collection_modifyitems(items):
    for item in items:
        if item.path.name in SLOW_FILES:
            item.add_marker(pytest.mark.slow)


@pytest.fixture(autouse=True)
def run_in_tmp_path(tmp_path, monkeypatch):
    """Run each test from its own directory, so a config loaded from a relative
    path (whose default work_dir is ./work) never writes into the repo."""
    monkeypatch.chdir(tmp_path)


def _refuse(self, method, url, *args, **kwargs):
    raise requests.ConnectionError(f"tests never reach the network ({method} {url})")


@pytest.fixture(scope="session", autouse=True)
def offline_dependencies():
    """Isolate even module-scoped tone fixtures from local models and API keys.

    Separation unit tests explicitly inject their fake Demucs modules after
    this fixture; pipeline tests exercise the same fallback as the CI dev extra.
    """
    with pytest.MonkeyPatch.context() as patch:
        patch.delenv("ANTHROPIC_API_KEY", raising=False)
        patch.delenv("CLAUDE_API_KEY", raising=False)
        patch.setitem(sys.modules, "demucs", None)
        patch.setitem(sys.modules, "demucs.separate", None)
        # Device choice must not depend on this machine's GPU: no torch, and
        # CTranslate2 (which can see a GPU next to a CPU torch) sees none.
        # Tests that need a GPU install a fake torch of their own.
        patch.setitem(sys.modules, "torch", None)
        patch.setattr(hardware, "ctranslate2_cuda_devices", lambda: 0)
        # The decision layer's default model runs in-process; never load real
        # weights in a test. It falls back to the rules alone, as designed.
        patch.setitem(sys.modules, "laya", None)
        # The API reports hardware; never probe the real machine to answer it.
        # A service nobody started (Voicebox, Radarr, …) refuses at once. On
        # Windows a closed localhost port otherwise takes ~2 s to refuse, and
        # with two retries every such call cost 7.5 s. Tests that fake HTTP
        # patch Session.request themselves, on top of this.
        patch.setattr(requests.Session, "request", _refuse)
        patch.setattr(ArrClient, "backoff", 0)
        patch.setattr(hardware, "_cached", {
            "source": "none", "devices": [], "libraries": {}, "notes": ["test"],
            "torch": {"installed": False, "version": None, "cuda": None,
                      "cuda_available": False}})
        yield


@pytest.fixture
def client_factory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    clients = []

    def make(data=None):
        root = tmp_path / f"app-{len(clients)}"
        root.mkdir()
        path = root / "config.yaml"
        config = Config.load(path).with_overrides({
            "paths.work_dir": str(root / "work"),
            "paths.output_dir": str(root / "output"),
        }).with_overrides(data or {})
        path.write_text(yaml.safe_dump(config.as_dict()), encoding="utf-8")
        client = TestClient(create_app(config))
        clients.append(client)
        return client

    yield make
    for client in reversed(clients):
        client.app.state.library.debouncer.cancel()
        client.close()
        client.app.state.jobs.close()
