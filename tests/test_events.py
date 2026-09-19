"""Event bus + SSE endpoint tests — in-process, no network."""

import json

import pytest
import yaml
from fastapi.testclient import TestClient

from doblarr.config import Config
from doblarr.events import EventBus
from doblarr.server import create_app

API_KEY = "test-key"
HEADERS = {"X-Api-Key": API_KEY}


def test_event_bus_pub_sub_and_replay():
    bus = EventBus()
    bus.publish("job", {"type": "queued", "job_id": "1"})
    bus.publish("scan", {"type": "started"})

    q = bus.subscribe()  # replays the buffer to a late subscriber
    first, second = q.get_nowait(), q.get_nowait()
    assert first["topic"] == "job" and first["type"] == "queued"
    assert second["topic"] == "scan"
    assert "at" in first  # every event is timestamped

    bus.publish("job", {"type": "done", "job_id": "1"})  # live fanout
    assert q.get_nowait()["type"] == "done"

    bus.unsubscribe(q)
    assert bus.subscriber_count == 0


def test_event_bus_buffer_is_capped():
    bus = EventBus()
    for i in range(80):
        bus.publish("t", {"i": i})
    q = bus.subscribe()
    assert q.qsize() == 50
    assert q.get_nowait()["i"] == 30  # oldest events fell off the ring


@pytest.fixture
def sse_client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump({"web": {"api_key": API_KEY}}), encoding="utf-8")
    app = create_app(Config.load(p))
    app.state.events.publish("job", {"type": "queued", "job_id": "abc", "title": "T"})
    return TestClient(app)


def test_sse_requires_auth(sse_client):
    assert sse_client.get("/api/events").status_code == 401


def test_sse_streams_replayed_event(sse_client):
    """Drive the ASGI app directly: starlette's TestClient waits for the app
    coroutine to finish, which an infinite SSE stream never does — so consume
    the ASGI messages ourselves and disconnect after the first body chunk."""
    import asyncio

    app = sse_client.app
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
             "method": "GET", "scheme": "http", "path": "/api/events",
             "raw_path": b"/api/events", "query_string": b"", "root_path": "",
             "headers": [(b"host", b"test"), (b"x-api-key", API_KEY.encode())],
             "client": ("test", 123), "server": ("test", 80)}

    async def drive():
        sent: list[dict] = []
        disconnect = asyncio.Event()

        async def receive():
            await disconnect.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            sent.append(message)

        task = asyncio.create_task(app(scope, receive, send))
        for _ in range(100):
            if any(m["type"] == "http.response.body" and m.get("body") for m in sent):
                break
            await asyncio.sleep(0.05)
        disconnect.set()
        await asyncio.wait_for(task, timeout=5)
        return sent

    sent = asyncio.run(drive())
    start = next(m for m in sent if m["type"] == "http.response.start")
    assert start["status"] == 200
    content_types = dict(start["headers"])[b"content-type"]
    assert content_types.startswith(b"text/event-stream")
    body = b"".join(m.get("body", b"") for m in sent
                    if m["type"] == "http.response.body")
    line = body.decode().strip().splitlines()[0]
    assert line.startswith("data: ")
    evt = json.loads(line[len("data: "):])
    assert evt["topic"] == "job" and evt["job_id"] == "abc"
    # disconnecting unsubscribed (no leaked queues)
    assert app.state.events.subscriber_count == 0
