"""The event service's lifecycle: built by the runtime when KITE_MCP_EVENTS=true, started and stopped with the HTTP
server, one serving process per store."""

from __future__ import annotations

import asyncio
import json

import pytest

from conftest import BEARER
from events_helpers import ORDER_ID, PY, filled_row, write_consumers
from kite_connect_mcp.errors import ConfigError
from kite_connect_mcp.events.service import EventService
from kite_connect_mcp.server import RuntimeLifespan, build_runtime

SERVICE = {"KITE_MCP_EVENTS": "true", "KITE_MCP_TRANSPORT": "http", "KITE_MCP_BEARER_TOKEN": BEARER}


def _service_tasks() -> list[str]:
    return sorted(t.get_name() for t in asyncio.all_tasks() if t.get_name().startswith("kite-events-") and not t.done())


def test_off_by_default(runtime_factory):
    assert runtime_factory().events is None


def test_the_runtime_builds_the_service_with_its_parts(runtime_factory):
    rt = runtime_factory(**SERVICE)
    svc = rt.events
    assert isinstance(svc, EventService)
    assert svc.doorbells is not None and svc.poller is not None and svc.router is not None
    assert svc.store.path == rt.settings.events_db


def test_a_bad_consumers_file_stops_the_server_from_starting(settings_factory, tmp_path):
    bad = write_consumers(tmp_path, [{"name": "x", "command": ["relative/path"]}])
    with pytest.raises(ConfigError, match="absolute"):
        build_runtime(settings_factory(**SERVICE, KITE_MCP_EVENTS_CONSUMERS_FILE=str(bad)))


async def test_start_and_stop(runtime_factory, fake_kite):
    fake_kite.on("GET", "/orders", [])
    rt = runtime_factory(**SERVICE)
    await rt.events.start()
    assert rt.events.running
    for _ in range(100):
        if fake_kite.calls("GET", "/orders"):
            break
        await asyncio.sleep(0.01)
    assert fake_kite.calls("GET", "/orders")  # the start-up poll
    await rt.aclose()  # stops the service, then closes the client
    assert not rt.events.running
    assert _service_tasks() == []
    assert fake_kite.writes() == []


async def test_start_and_stop_twice_are_harmless(runtime_factory, fake_kite):
    fake_kite.on("GET", "/orders", [])
    rt = runtime_factory(**SERVICE)
    await rt.events.start()
    await rt.events.start()  # already running: nothing more is started
    names = ["kite-events-doorbells", "kite-events-housekeeping", "kite-events-poller", "kite-events-router"]
    assert _service_tasks() == names
    await rt.events.stop()
    await rt.events.stop()
    assert not rt.events.running and _service_tasks() == []
    await rt.aclose()
    assert fake_kite.writes() == []


async def test_a_second_service_on_the_same_store_cannot_start(runtime_factory, fake_kite):
    fake_kite.on("GET", "/orders", [])
    first = runtime_factory(**SERVICE)
    second = runtime_factory(**SERVICE)
    await first.events.start()
    with pytest.raises(ConfigError, match="already"):
        await second.events.start()
    assert not second.events.running
    await first.aclose()
    await second.aclose()


async def test_stopping_releases_the_store_for_the_next_server(runtime_factory, fake_kite):
    fake_kite.on("GET", "/orders", [])
    first = runtime_factory(**SERVICE)
    await first.events.start()
    await first.aclose()
    second = runtime_factory(**SERVICE)  # a restart on the same store
    await second.events.start()
    assert second.events.running
    await second.aclose()
    assert fake_kite.writes() == []


async def test_the_running_service_delivers_to_its_consumers(runtime_factory, fake_kite, tmp_path):
    fake_kite.on("GET", "/orders", [])
    out = tmp_path / "notify.out"
    script = f"import sys\nopen({str(out)!r}, 'a').write(sys.stdin.read())\n"
    consumers = write_consumers(tmp_path, [{"name": "notify", "command": [PY, "-c", script]}])
    rt = runtime_factory(**SERVICE, KITE_MCP_EVENTS_CONSUMERS_FILE=str(consumers))
    await rt.events.start()
    seq = rt.events.ingest_order(filled_row(), source="poll")  # on the loop's thread, as the poll does
    for _ in range(300):
        if out.exists() and out.read_text().endswith("\n"):
            break
        await asyncio.sleep(0.01)
    await rt.aclose()
    (line,) = out.read_text().splitlines()
    assert json.loads(line)["seq"] == seq
    assert fake_kite.writes() == []


async def test_housekeeping_prunes_old_events(runtime_factory, fake_kite):
    rt = runtime_factory(**SERVICE, KITE_MCP_EVENTS_KEEP_DAYS="1")
    svc = rt.events
    svc.ingest_order(filled_row(), source="poll")
    with svc.store.write() as con:
        con.execute("UPDATE events SET received_at = '2026-01-01T00:00:00.000+05:30'")
    assert svc.housekeep() == 1
    assert svc.store.events() == []
    await rt.aclose()


async def test_the_http_lifespan_starts_and_stops_the_service(runtime_factory, fake_kite):
    fake_kite.on("GET", "/orders", [])
    rt = runtime_factory(**SERVICE)
    sent = []
    messages = [{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}]

    async def inner(scope, receive, send):  # a minimal ASGI app that speaks the lifespan protocol
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return

    async def receive():
        if messages[0]["type"] == "lifespan.shutdown":
            assert rt.events.running  # started before the server said it was up
        return messages.pop(0)

    async def send(message):
        sent.append(message["type"])

    await RuntimeLifespan(inner, rt)({"type": "lifespan"}, receive, send)
    assert sent == ["lifespan.startup.complete", "lifespan.shutdown.complete"]
    assert not rt.events.running
    assert ORDER_ID  # (the helper import is used by other tests in this file)
    assert fake_kite.writes() == []


async def test_the_http_server_does_not_come_up_when_the_service_cannot_start(runtime_factory, fake_kite):
    fake_kite.on("GET", "/orders", [])
    first = runtime_factory(**SERVICE)
    await first.events.start()
    second = runtime_factory(**SERVICE)
    sent = []

    async def inner(scope, receive, send):
        await receive()
        await send({"type": "lifespan.startup.complete"})

    async def receive():
        return {"type": "lifespan.startup"}

    async def send(message):
        sent.append(message["type"])

    with pytest.raises(ConfigError, match="already"):
        await RuntimeLifespan(inner, second)({"type": "lifespan"}, receive, send)
    assert sent == []  # the server never said it was up
    await first.aclose()
    await second.aclose()
