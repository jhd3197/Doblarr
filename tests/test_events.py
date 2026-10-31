"""Event bus + SSE endpoint tests — in-process, no network."""

import json

import pytest

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
def sse_client(client_factory):
    client = client_factory({"web": {"api_key": API_KEY}})
    client.app.state.events.publish("job", {"type": "queued", "job_id": "abc", "title": "T"})
    return client


def test_sse_requires_auth(sse_client):
    assert sse_client.get("/api/events").status_code == 401


def test_sse_streams_replayed_event(sse_client, monkeypatch):
    """Drive the ASGI app directly: starlette's TestClient waits for the app
    coroutine to finish, which an infinite SSE stream never does — so consume
    the ASGI messages ourselves and disconnect after the first body chunk."""
    import asyncio

    from doblarr import server

    # The stream notices a disconnect between heartbeats; don't wait 15 s for one.
    monkeypatch.setattr(server, "SSE_HEARTBEAT", 0.2)

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


def test_log_records_stream_to_bus(sse_client):
    import logging

    from doblarr.logging_setup import BusLogHandler

    app = sse_client.app
    root = logging.getLogger()
    bus_handlers = [h for h in root.handlers if isinstance(h, BusLogHandler)]
    assert len(bus_handlers) == 1 and bus_handlers[0].bus is app.state.events

    q = app.state.events.subscribe(replay=False)  # skip the fixture's buffered event
    logging.getLogger("doblarr.test").warning("hello from the test")
    evt = q.get(timeout=2)
    assert evt["topic"] == "log"
    assert evt["level"] == "WARNING" and evt["logger"] == "doblarr.test"
    assert "hello from the test" in evt["message"]

    # records from the bus module itself are muted (recursion guard)
    logging.getLogger("doblarr.events").warning("must not loop")
    assert q.empty()

    # a second app replaces the handler instead of stacking another
    app2 = create_app(Config.load("nope.yaml"))
    bus_handlers = [h for h in logging.getLogger().handlers
                    if isinstance(h, BusLogHandler)]
    assert len(bus_handlers) == 1 and bus_handlers[0].bus is app2.state.events


def test_api_keys_in_access_log_urls_are_masked():
    import logging

    from doblarr.logging_setup import RedactApiKey

    record = logging.LogRecord("uvicorn.access", logging.INFO, "", 0,
                               '%s - "%s %s HTTP/%s" %d', ("127.0.0.1:5", "GET",
                               "/api/events?api_key=s3cret&since=4", "1.1", 200), None)
    RedactApiKey().filter(record)
    assert "s3cret" not in record.getMessage()
    assert "api_key=***&since=4" in record.getMessage()


def test_a_burst_of_log_lines_keeps_job_events_in_the_replay():
    bus = EventBus()
    bus.publish("job", {"type": "started", "job_id": "j1"})
    for i in range(200):
        bus.publish("log", {"message": f"line {i}"})
    q = bus.subscribe()
    replayed = [q.get_nowait() for _ in range(q.qsize())]
    assert replayed[0] == {**replayed[0], "topic": "job", "type": "started"}
    assert sum(e["topic"] == "log" for e in replayed) == 50


def test_an_event_loop_subscriber_gets_events_from_any_thread():
    import asyncio
    import threading

    async def receive():
        bus = EventBus()
        q = bus.subscribe_async(replay=False)
        threading.Thread(target=bus.publish, args=("job", {"type": "done"})).start()
        return await q.get(timeout=2)

    assert asyncio.run(receive())["type"] == "done"
