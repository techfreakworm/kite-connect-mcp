"""The event service's lifecycle: built by the runtime when KITE_MCP_EVENTS=true, started and stopped with the HTTP
server, one serving process per store."""

from __future__ import annotations

import asyncio
import fcntl
import json
import logging
from collections.abc import Callable
from typing import Any

import pytest
import uvicorn
from uvicorn.lifespan.on import LifespanOn

from conftest import BEARER
from events_helpers import PY, filled_row, write_consumers
from kite_connect_mcp.cli import main
from kite_connect_mcp.errors import ConfigError
from kite_connect_mcp.events.service import EventService
from kite_connect_mcp.events.store import StoreError
from kite_connect_mcp.server import RuntimeLifespan, build_http_app, build_runtime, build_server

SERVICE = {"KITE_MCP_EVENTS": "true", "KITE_MCP_TRANSPORT": "http", "KITE_MCP_BEARER_TOKEN": BEARER}


def _service_tasks() -> list[str]:
    return sorted(t.get_name() for t in asyncio.all_tasks() if t.get_name().startswith("kite-events-") and not t.done())


async def _until(check: Callable[[], Any], within: float = 5.0) -> bool:
    """Wait, in steps of 10 ms, until check() is true or `within` seconds have passed; returns what check() said."""
    for _ in range(int(within / 0.01)):
        if check():
            return True
        await asyncio.sleep(0.01)
    return bool(check())


def _errors(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.levelno >= logging.ERROR]


def _age_events(svc: EventService) -> None:
    with svc.store.write() as con:
        con.execute("UPDATE events SET received_at = '2026-01-01T00:00:00.000+05:30'")


def _stubborn_poller(svc: EventService, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Make the poller a part that does not notice the stop event; returns a list that gets "cancelled" when it is."""
    seen: list[str] = []

    async def run(stop: asyncio.Event) -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            seen.append("cancelled")
            raise

    monkeypatch.setattr(svc.poller, "run", run)
    return seen


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


async def test_start_and_stop(runtime_factory, fake_kite, caplog):
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
    assert _errors(caplog) == []  # a clean stop is not reported as parts that died
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
    # a real command: up to 10 s for it to start and write on a slow runner (the wait ends as soon as it has)
    await _until(lambda: out.exists() and out.read_text().endswith("\n"), within=10)
    await rt.aclose()
    assert out.exists(), "the consumer got no event within 10 s"
    (line,) = out.read_text().splitlines()
    assert json.loads(line)["seq"] == seq
    assert fake_kite.writes() == []


async def test_housekeeping_prunes_old_events(runtime_factory, fake_kite):
    rt = runtime_factory(**SERVICE, KITE_MCP_EVENTS_KEEP_DAYS="1")
    svc = rt.events
    svc.ingest_order(filled_row(), source="poll")
    _age_events(svc)
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
        return messages.pop(0)

    async def send(message):
        if message["type"] == "lifespan.startup.complete":
            assert rt.events.running  # started before the server says it is up
        sent.append(message["type"])

    await RuntimeLifespan(inner, rt)({"type": "lifespan"}, receive, send)
    assert sent == ["lifespan.startup.complete", "lifespan.shutdown.complete"]
    assert not rt.events.running
    assert fake_kite.writes() == []


def _uvicorn_lifespan(rt: Any, mode: str) -> LifespanOn:
    """uvicorn's own lifespan handler around the real HTTP app, as `serve` runs it."""
    server, _ = build_server(rt)
    return LifespanOn(uvicorn.Config(build_http_app(server, rt), lifespan=mode, log_config=None))


async def test_uvicorn_starts_the_service_before_the_server_is_up_and_stops_it_at_shutdown(runtime_factory, fake_kite):
    fake_kite.on("GET", "/orders", [])
    rt = runtime_factory(**SERVICE)
    lifespan = _uvicorn_lifespan(rt, "on")
    await lifespan.startup()
    assert not lifespan.should_exit
    assert rt.events.running  # when uvicorn starts to serve
    await lifespan.shutdown()
    assert not lifespan.should_exit
    assert not rt.events.running and _service_tasks() == []
    assert fake_kite.writes() == []


@pytest.mark.parametrize("mode", ["on", "auto"])
async def test_the_http_server_does_not_come_up_when_the_service_cannot_start(runtime_factory, fake_kite, mode):
    """`serve` runs uvicorn with lifespan="on"; even under "auto" (where an app that raises without reporting a failed
    startup counts as one without lifespan support, and is served) the server exits."""
    fake_kite.on("GET", "/orders", [])
    first = runtime_factory(**SERVICE)
    await first.events.start()
    second = runtime_factory(**SERVICE)
    lifespan = _uvicorn_lifespan(second, mode)
    await lifespan.startup()
    assert lifespan.should_exit  # uvicorn exits instead of serving with order events off
    assert lifespan.startup_failed  # reported as a failed startup, not as "lifespan unsupported"
    assert not second.events.running
    await first.aclose()
    await second.aclose()
    assert fake_kite.writes() == []


async def test_serve_runs_uvicorn_with_the_lifespan_on(cli_env, monkeypatch):
    """With lifespan="on", uvicorn exits when the app's startup fails in any way, not only when it says so."""
    monkeypatch.setenv("KITE_MCP_TRANSPORT", "http")
    monkeypatch.setenv("KITE_MCP_BEARER_TOKEN", BEARER)
    runs: list[tuple[Any, dict[str, Any]]] = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: runs.append((app, kwargs)))
    assert main(["serve"]) == 0
    ((app, kwargs),) = runs
    await app.rt.aclose()
    assert kwargs["lifespan"] == "on"


async def test_a_stopped_service_cannot_be_started_again(runtime_factory, fake_kite):
    fake_kite.on("GET", "/orders", [])
    rt = runtime_factory(**SERVICE)
    await rt.events.start()
    await rt.events.stop()  # closes the store
    with pytest.raises(ConfigError, match="cannot be restarted"):
        await rt.events.start()
    assert not rt.events.running and _service_tasks() == []
    other = runtime_factory(**SERVICE)
    await other.events.start()  # the refused start took no lock
    await other.aclose()
    await rt.aclose()
    assert fake_kite.writes() == []


async def test_a_part_that_does_not_stop_in_time_is_cancelled(runtime_factory, fake_kite, monkeypatch, caplog):
    fake_kite.on("GET", "/orders", [])
    rt = runtime_factory(**SERVICE)
    svc = rt.events
    seen = _stubborn_poller(svc, monkeypatch)
    monkeypatch.setattr(svc, "STOP_TIMEOUT_S", 0.05)
    await svc.start()
    await svc.stop()
    assert seen == ["cancelled"]
    assert not svc.running and _service_tasks() == []
    assert _errors(caplog) == []
    await rt.aclose()


async def test_a_cancelled_stop_still_stops_the_parts_and_frees_the_store(runtime_factory, fake_kite, monkeypatch):
    fake_kite.on("GET", "/orders", [])
    rt = runtime_factory(**SERVICE)
    svc = rt.events
    seen = _stubborn_poller(svc, monkeypatch)
    await svc.start()
    stopping = asyncio.create_task(svc.stop())
    assert await _until(lambda: _service_tasks() == ["kite-events-poller"])  # stop() now waits for the poller
    stopping.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stopping
    assert seen == ["cancelled"]
    assert not svc.running and _service_tasks() == []
    with pytest.raises(StoreError):
        svc.store.events()  # closed
    other = runtime_factory(**SERVICE)
    await other.events.start()  # the lock was released
    await other.aclose()
    await rt.aclose()
    assert fake_kite.writes() == []


async def test_a_lock_file_that_cannot_be_opened(runtime_factory):
    rt = runtime_factory(**SERVICE)
    rt.events.store.path.with_name(rt.events.store.path.name + ".lock").mkdir()
    with pytest.raises(ConfigError, match=r"cannot open the lock file .*\.lock: Is a directory$"):
        await rt.events.start()
    assert not rt.events.running


async def test_a_lock_error_without_a_text_is_named_by_its_kind(runtime_factory, monkeypatch):
    rt = runtime_factory(**SERVICE)

    def flock(fd: int, operation: int) -> None:
        raise OSError()  # strerror None

    monkeypatch.setattr(fcntl, "flock", flock)
    with pytest.raises(ConfigError, match=r"cannot lock .*\.lock: OSError$"):
        await rt.events.start()
    assert not rt.events.running


@pytest.mark.parametrize("ending", ["raises", "is cancelled", "returns"])
async def test_a_part_that_ends_while_the_service_runs_is_logged(
    runtime_factory, fake_kite, monkeypatch, caplog, ending
):
    fake_kite.on("GET", "/orders", [])
    rt = runtime_factory(**SERVICE)
    svc = rt.events
    go = asyncio.Event()
    boom = RuntimeError("boom")

    async def run(stop: asyncio.Event) -> None:
        await go.wait()
        if ending == "raises":
            raise boom

    monkeypatch.setattr(svc.poller, "run", run)
    await svc.start()
    (task,) = [t for t in asyncio.all_tasks() if t.get_name() == "kite-events-poller"]
    if ending == "is cancelled":
        task.cancel()
    else:
        go.set()
    await asyncio.wait([task])
    await asyncio.sleep(0)
    (record,) = _errors(caplog)
    assert "kite-events-poller" in record.getMessage() and "running" in record.getMessage()
    if ending == "raises":
        assert record.exc_info is not None and record.exc_info[1] is boom
    caplog.clear()
    await rt.aclose()
    assert _errors(caplog) == []  # the others stop quietly
    assert fake_kite.writes() == []


async def test_the_housekeeping_task_prunes_at_start_and_then_every_round(runtime_factory, fake_kite, monkeypatch):
    fake_kite.on("GET", "/orders", [])
    rt = runtime_factory(**SERVICE, KITE_MCP_EVENTS_KEEP_DAYS="1")
    svc = rt.events
    svc.ingest_order(filled_row(), source="poll")
    _age_events(svc)
    rounds: list[int] = []
    housekeep = svc.housekeep

    def counted() -> int:
        rounds.append(1)
        if len(rounds) == 2:
            raise StoreError("cannot write the event store: disk I/O error")  # a bad round does not end the task
        return housekeep()

    monkeypatch.setattr(svc, "housekeep", counted)
    monkeypatch.setattr(svc, "HOUSEKEEPING_S", 0.01)
    await svc.start()
    assert await _until(lambda: svc.store.events() == [])  # the round at start
    svc.ingest_order(filled_row(order_id="250101000000002"), source="poll")
    _age_events(svc)
    assert len(svc.store.events()) == 1
    assert await _until(lambda: len(rounds) >= 3 and svc.store.events() == [])  # a later round, after the bad one
    await rt.aclose()
    assert fake_kite.writes() == []
