"""Shared isolated API clients; workers start only in explicit lifespan tests."""

import sys

import pytest
import yaml
from fastapi.testclient import TestClient

from doblarr import hardware
from doblarr.config import Config
from doblarr.server import create_app


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
        # The API reports hardware; never probe the real machine to answer it.
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
