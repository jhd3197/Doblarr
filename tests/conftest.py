"""Shared isolated API clients; workers start only in explicit lifespan tests."""

import pytest
import yaml
from fastapi.testclient import TestClient

from doblarr.config import Config
from doblarr.server import create_app


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
