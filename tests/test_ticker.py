"""The WebSocket ticker: the manager against a real WebSocket server on 127.0.0.1 (KiteServer below stands in for
wss://ws.kite.trade), then the tools end to end through `mcp.Client` with an in-process fake connection.

Nothing here waits on the clock for its result: tests wait for the condition they care about (`eventually`), with
millisecond timings, so they are fast when the code is right and fail with a message when it is not."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import datetime as dt
import functools
import json
import logging
import os
import random
import re
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from http import HTTPStatus
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest
import uvicorn
import websockets.asyncio.client
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from websockets.asyncio.server import ServerConnection, serve
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosed, ConnectionClosedOK, InvalidStatus
from websockets.http11 import Response

from conftest import ACCESS_TOKEN, API_KEY, API_SECRET, BEARER, FIXED_NOW, tool_json, tool_text
from kite_connect_mcp.errors import ConfigError, InstrumentsUnavailable, NetworkError, NotLoggedIn, ToolInputError
from kite_connect_mcp.server import build_http_app, build_runtime, build_server
from kite_connect_mcp.ticker.manager import (
    EVENT_BUFFER_SIZE,
    EventBuffer,
    Target,
    TickerError,
    TickerManager,
    Timings,
    clean_order,
    default_backoff,
    plan_messages,
)
from test_ticker_parser import (
    BIDS,
    INFY,
    NIFTY_50,
    OFFERS,
    frame,
    full_packet,
    index_packet,
    ltp_packet,
    quote_packet,
    token_for,
)

TATAMOTORS = token_for(3454, 1)  # 884737


# --- helpers ---------------------------------------------------------------------------------------------------------


async def eventually(check: Callable[[], Any], within: float = 5.0) -> Any:
    """Wait until check() is truthy and return its value; fail with the last value if it never is."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + within
    while True:
        value = check()
        if value:
            return value
        if loop.time() > deadline:
            raise AssertionError(f"condition not met within {within} s (last value: {value!r})")
        await asyncio.sleep(0.005)


async def nth(connections: list[Any], index: int) -> Any:
    """The index-th connection the server has accepted, once it exists."""
    return await eventually(lambda: connections[index] if len(connections) > index else None)


class Clock:
    """A settable wall clock (the manager's `clock`)."""

    def __init__(self) -> None:
        self.now = FIXED_NOW

    def __call__(self) -> dt.datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += dt.timedelta(seconds=seconds)


class FakeTokens:
    """Stands in for TokenProvider; the test changes `token` to simulate a new login (None = logged out)."""

    def __init__(self, token: str | None = "first-token-0001") -> None:
        self.token = token

    def current(self) -> Any:
        return None if self.token is None else SimpleNamespace(access_token=self.token)


FAST = Timings(
    connect_timeout_s=3.0,
    close_timeout_s=0.5,
    idle_timeout_s=5.0,
    poll_s=0.02,
    early_close_s=1.0,
    stable_after_s=0.3,
    connect_wait_s=3.0,
    backoff_base_s=0.01,
    backoff_cap_s=0.02,
)
direct = functools.partial(websockets.asyncio.client.connect, proxy=None, ping_interval=None)


@dataclass
class Conn:
    """One connection the fake Kite accepted."""

    ws: ServerConnection
    path: str
    received: list[Any] = field(default_factory=list)
    closed: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def query(self) -> dict[str, list[str]]:
        return parse_qs(urlsplit(self.path).query)

    def drop(self) -> None:
        """Cut the TCP connection without a closing handshake: what a network failure looks like."""
        self.ws.transport.abort()


class KiteServer:
    """A stand-in for wss://ws.kite.trade on 127.0.0.1. Records every handshake and every request a client sends;
    can push frames, drop links, close right after connecting, or refuse the handshake with an HTTP status."""

    def __init__(self) -> None:
        self.connections: list[Conn] = []
        self.attempts = 0  # handshakes seen, accepted or refused
        self.refuse_with: int | None = None
        self.close_on_connect = False
        self.url = ""
        self._server: Any = None

    async def start(self) -> None:
        self._server = await serve(
            self._handle, "127.0.0.1", 0, process_request=self._process, compression=None, ping_interval=None
        )
        self.url = f"ws://127.0.0.1:{self._server.sockets[0].getsockname()[1]}"

    async def stop(self) -> None:
        self._server.close()
        await self._server.wait_closed()

    def _process(self, connection: ServerConnection, request: Any) -> Response | None:
        self.attempts += 1
        if self.refuse_with is not None:
            return connection.respond(HTTPStatus(self.refuse_with), "refused\n")
        return None

    async def _handle(self, ws: ServerConnection) -> None:
        conn = Conn(ws, ws.request.path if ws.request else "")
        self.connections.append(conn)
        try:
            if self.close_on_connect:
                await ws.close()
                return
            async for message in ws:
                conn.received.append(json.loads(message))
        except ConnectionClosed:
            pass
        finally:
            conn.closed.set()


def replay(messages: list[dict[str, Any]]) -> dict[int, str]:
    """What Kite would believe is subscribed after these requests: subscribe adds (mode quote), mode sets the mode of
    subscribed tokens, unsubscribe removes."""
    state: dict[int, str] = {}
    for message in messages:
        action, value = message["a"], message["v"]
        if action == "subscribe":
            state.update({token: state.get(token, "quote") for token in value})
        elif action == "unsubscribe":
            for token in value:
                state.pop(token, None)
        elif action == "mode":
            mode, tokens = value
            state.update({token: mode for token in tokens if token in state})
    return state


@pytest.fixture
async def kite() -> AsyncIterator[KiteServer]:
    server = KiteServer()
    await server.start()
    yield server
    await server.stop()


@pytest.fixture
async def make_manager(
    kite: KiteServer, settings_factory: Callable[..., Any]
) -> AsyncIterator[Callable[..., TickerManager]]:
    made: list[TickerManager] = []

    def make(
        tokens: Any = None,
        *,
        timings: Timings = FAST,
        clock: Callable[[], dt.datetime] | None = None,
        connect: Any = direct,
        backoff: Callable[[int], float] | None = None,
        **env: str | None,
    ) -> TickerManager:
        settings = settings_factory(KITE_TICKER_URL=kite.url, **env)
        manager = TickerManager(
            settings,
            tokens or FakeTokens(),
            clock=clock or (lambda: FIXED_NOW),
            connect=connect,
            timings=timings,
            backoff=backoff or (lambda attempt: 0.01),
        )
        made.append(manager)
        return manager

    yield make
    await asyncio.gather(*(manager.stop() for manager in made))


INFY_T = Target(INFY, "NSE:INFY")
TATA_T = Target(TATAMOTORS, "NSE:TATAMOTORS")


# --- pure pieces -----------------------------------------------------------------------------------------------------


def test_plan_messages_subscribes_then_sets_modes_and_unsubscribes():
    assert plan_messages({}, {}) == []
    assert plan_messages({}, {1: "ltp", 2: "quote", 3: "ltp", 4: "full"}) == [
        {"a": "subscribe", "v": [1, 2, 3, 4]},
        {"a": "mode", "v": ["ltp", [1, 3]]},
        {"a": "mode", "v": ["quote", [2]]},
        {"a": "mode", "v": ["full", [4]]},
    ]
    applied = {1: "ltp", 2: "quote", 3: "full"}
    assert plan_messages(applied, {1: "ltp", 2: "full", 4: "quote"}) == [
        {"a": "unsubscribe", "v": [3]},
        {"a": "subscribe", "v": [4]},
        {"a": "mode", "v": ["quote", [4]]},
        {"a": "mode", "v": ["full", [2]]},
    ]
    assert plan_messages(applied, dict(applied)) == []  # nothing changed: nothing to send


def test_default_backoff_grows_exponentially_with_jitter_and_is_capped():
    backoff = default_backoff(Timings(backoff_base_s=1.0, backoff_cap_s=60.0))
    for attempt in range(12):
        ceiling = min(60.0, 2.0**attempt)
        samples = [backoff(attempt) for _ in range(200)]
        assert all(ceiling * 0.5 <= s <= ceiling for s in samples), attempt
        assert len(set(samples)) > 1  # jittered: retries from many servers do not line up
    assert backoff(50) <= 60.0 and backoff(-3) <= 1.0


def test_event_buffer_is_bounded_and_pages_forward():
    clock = Clock()
    buffer = EventBuffer(clock)
    for i in range(EVENT_BUFFER_SIZE + 25):
        clock.advance(1)
        buffer.add("message", f"m{i}", "ticker")
    assert len(buffer) == EVENT_BUFFER_SIZE == 500
    latest, more = buffer.recent(3)
    assert [e["data"] for e in latest] == [f"m{i}" for i in (522, 523, 524)] and more is False
    assert [e["seq"] for e in latest] == [523, 524, 525]  # sequence numbers keep counting past evicted entries
    first_page, more = buffer.recent(2, since=latest[0]["received_at"] - dt.timedelta(seconds=10))
    assert [e["data"] for e in first_page] == ["m513", "m514"] and more is True  # oldest first, forward paging
    after_last, more = buffer.recent(10, since=latest[-1]["received_at"])
    assert after_last == [] and more is False  # strictly after


def test_clean_order_keeps_documented_fields_and_drops_identifiers_and_secrets():
    cleaned = clean_order(
        {
            "order_id": "220303000308932",
            "status": "COMPLETE",
            "status_message": None,
            "status_message_raw": "raw text from the OMS",
            "tradingsymbol": "SBIN",
            "quantity": 1,
            "average_price": 470.5,
            "user_id": "AB1234",
            "placed_by": "AB1234",
            "app_id": 1234,
            "checksum": "deadbeef",
            "guid": "XXXXXX",
            "meta": {"x": 1},
            "tag": "x" * 5000,
            "price": float("nan"),
            "trigger_price": {"nested": 1},
            "unknown_field": "x",
        }
    )
    assert cleaned == {
        "order_id": "220303000308932",
        "status": "COMPLETE",
        "tradingsymbol": "SBIN",
        "quantity": 1,
        "average_price": 470.5,
        "tag": "x" * 500,  # free text is clipped
    }


def test_the_manager_is_cheap_to_construct_and_needs_no_event_loop(settings_factory):
    manager = TickerManager(settings_factory(), FakeTokens(), clock=lambda: FIXED_NOW)
    status = manager.status()
    assert status["state"] == "idle" and status["running"] is False and status["connected"] is False
    assert status["subscriptions"] == {"total": 0, "limit": 1000, "ltp": 0, "quote": 0, "full": 0}
    assert status["endpoint"] == "ws.kite.trade"
    assert asyncio.run(manager.stop()) is None  # stop() on a manager that never started


def test_the_limit_never_exceeds_kites_hard_limit(settings_factory):
    settings = dataclasses.replace(settings_factory(), ticker_max_subscriptions=5000)
    assert TickerManager(settings, FakeTokens()).max_subscriptions == 3000


# --- connecting and subscribing --------------------------------------------------------------------------------------


async def test_nothing_connects_until_the_first_subscribe(kite, make_manager):
    manager = make_manager()
    await asyncio.sleep(0.05)
    assert kite.attempts == 0 and manager.state == "idle" and not manager.status()["running"]
    await manager.subscribe([INFY_T])
    assert kite.attempts == 1
    assert manager.status()["connected"] and manager.state == "connected" and manager.status()["running"]


async def test_subscribe_sends_subscribe_then_mode_and_the_credentials_travel_in_the_query(kite, make_manager):
    manager = make_manager(FakeTokens("tok-abc-123"))
    outcome = await manager.subscribe([INFY_T, TATA_T], "full")
    assert outcome == {INFY: "subscribed", TATAMOTORS: "subscribed"}
    conn = await nth(kite.connections, 0)
    await eventually(lambda: len(conn.received) >= 2)
    assert conn.received == [
        {"a": "subscribe", "v": [INFY, TATAMOTORS]},
        {"a": "mode", "v": ["full", [INFY, TATAMOTORS]]},
    ]
    assert conn.query == {"api_key": [API_KEY], "access_token": ["tok-abc-123"]}


async def test_mode_changes_unsubscribes_and_repeats_send_only_what_changed(kite, make_manager):
    manager = make_manager()
    await manager.subscribe([INFY_T, TATA_T], "quote")
    conn = await nth(kite.connections, 0)
    await eventually(lambda: len(conn.received) == 2)

    assert await manager.subscribe([INFY_T], "full") == {INFY: "mode_changed"}
    assert await manager.subscribe([TATA_T], "quote") == {TATAMOTORS: "unchanged"}  # nothing to send for this one
    assert await manager.unsubscribe([TATAMOTORS, 999]) == [TATAMOTORS]  # 999 was never subscribed
    await eventually(lambda: len(conn.received) == 4)
    assert conn.received[2:] == [{"a": "mode", "v": ["full", [INFY]]}, {"a": "unsubscribe", "v": [TATAMOTORS]}]
    assert manager.subscriptions() == {INFY: "full"}
    assert manager.status()["subscriptions"] == {"total": 1, "limit": 1000, "ltp": 0, "quote": 0, "full": 1}


async def test_an_empty_subscribe_only_opens_the_connection(kite, make_manager):
    manager = make_manager()
    assert await manager.subscribe([]) == {}
    conn = await nth(kite.connections, 0)
    assert manager.status()["connected"]
    await asyncio.sleep(0.05)
    assert conn.received == []


async def test_unsubscribing_the_last_instrument_keeps_the_connection_for_order_updates(kite, make_manager):
    manager = make_manager()
    await manager.subscribe([INFY_T])
    await manager.unsubscribe([INFY])
    assert manager.status()["connected"] and manager.subscriptions() == {}


async def test_concurrent_subscribes_and_unsubscribes_converge_on_the_wanted_state(kite, make_manager):
    manager = make_manager()
    targets = [Target(token_for(n, 1), f"NSE:S{n}") for n in range(1, 9)]
    await manager.subscribe(targets[:2], "quote")
    await asyncio.gather(
        manager.subscribe(targets[:5], "full"),
        manager.subscribe(targets[3:8], "ltp"),
        manager.unsubscribe([targets[1].token, targets[6].token]),
        manager.subscribe(targets[:3], "quote"),
    )
    conn = await nth(kite.connections, 0)
    wanted = manager.subscriptions()
    assert wanted  # something is left
    await eventually(lambda: replay(conn.received) == wanted)


async def test_the_subscription_limit_counts_new_instruments_only_and_is_all_or_nothing(kite, make_manager):
    manager = make_manager(KITE_MCP_TICKER_MAX_SUBSCRIPTIONS="3")
    a, b, c, d, e = (Target(token_for(n, 1), f"NSE:S{n}") for n in range(1, 6))
    await manager.subscribe([a, b, c])
    with pytest.raises(ToolInputError, match=r"limit of 3.*KITE_MCP_TICKER_MAX_SUBSCRIPTIONS"):
        await manager.subscribe([d])
    assert await manager.subscribe([a, b, c], "full") == {
        a.token: "mode_changed",
        b.token: "mode_changed",
        c.token: "mode_changed",
    }
    await manager.unsubscribe([c.token])
    with pytest.raises(ToolInputError):
        await manager.subscribe([d, e])  # one free slot, two wanted: neither is added
    assert set(manager.subscriptions()) == {a.token, b.token}
    await manager.subscribe([d])
    assert len(manager.subscriptions()) == 3


async def test_subscribe_rejects_a_bad_mode(make_manager):
    manager = make_manager()
    with pytest.raises(ToolInputError, match="mode"):
        await manager.subscribe([INFY_T], "everything")  # type: ignore[arg-type]
    assert manager.state == "idle"


async def test_subscribe_without_a_token_raises_not_logged_in_and_never_connects(kite, make_manager):
    manager = make_manager(FakeTokens(None))
    with pytest.raises(NotLoggedIn):
        await manager.subscribe([INFY_T])
    assert kite.attempts == 0 and manager.state == "idle" and manager.subscriptions() == {}


async def test_subscribe_without_an_api_key_is_a_config_error(make_manager):
    manager = make_manager(KITE_API_KEY=None)
    with pytest.raises(ConfigError, match="KITE_API_KEY"):
        await manager.subscribe([INFY_T])


# --- ticks and text frames -------------------------------------------------------------------------------------------


async def test_ticks_are_cached_replaced_and_aged_by_the_clock(kite, make_manager):
    clock = Clock()
    manager = make_manager(clock=clock)
    await manager.subscribe([INFY_T, Target(NIFTY_50, "NSE:NIFTY 50")], "full")
    conn = await nth(kite.connections, 0)
    await conn.ws.send(
        frame(
            quote_packet(INFY, last=150005, volume=10, close=148000),
            index_packet(NIFTY_50, last=2_450_050, high=2, low=1, open_=1, close=2_430_000),
            ltp_packet(token_for(99, 1), 100),  # a token nobody subscribed
        )
    )
    cached = await eventually(lambda: len(manager.latest()) == 2 and {c.token: c for c in manager.latest()})
    assert cached[INFY].tick["last_price"] == 1500.05 and cached[INFY].tick["volume_traded"] == 10
    assert cached[NIFTY_50].tick["tradable"] is False
    assert cached[INFY].received_at == FIXED_NOW
    assert manager.latest([token_for(99, 1)]) == []  # never cached: not subscribed

    clock.advance(30)
    assert manager.age_s(cached[INFY]) == 30.0
    await conn.ws.send(frame(quote_packet(INFY, last=150100, volume=11, close=148000)))
    await eventually(lambda: manager.latest([INFY])[0].tick["last_price"] == 1501.0)
    assert manager.latest([INFY])[0].received_at == clock.now  # replaced by the newer tick
    status = manager.status()
    assert status["ticks_cached"] == 2 and status["ticks_received"] == 3
    assert status["last_tick_at"] == clock.now


async def test_a_frame_in_flight_after_an_unsubscribe_does_not_bring_the_tick_back(kite, make_manager):
    manager = make_manager()
    await manager.subscribe([INFY_T, TATA_T])
    conn = await nth(kite.connections, 0)
    await conn.ws.send(frame(ltp_packet(INFY, 1), ltp_packet(TATAMOTORS, 2)))
    await eventually(lambda: len(manager.latest()) == 2)
    await manager.unsubscribe([INFY])
    assert [c.token for c in manager.latest()] == [TATAMOTORS]
    await conn.ws.send(frame(ltp_packet(INFY, 3)))
    await conn.ws.send(frame(ltp_packet(TATAMOTORS, 4)))
    await eventually(lambda: manager.latest([TATAMOTORS])[0].tick["last_price"] == 0.04)
    assert manager.latest([INFY]) == []


async def test_heartbeats_and_garbage_do_not_disturb_the_stream(kite, make_manager):
    clock = Clock()
    manager = make_manager(clock=clock)
    await manager.subscribe([INFY_T])
    conn = await nth(kite.connections, 0)
    clock.advance(5)
    await conn.ws.send(b"\x00")  # heartbeat
    await eventually(lambda: manager.status()["last_message_at"] == clock.now)
    assert manager.status()["ticks_received"] == 0 and manager.status()["packets_skipped"] == 0

    await conn.ws.send(b"\x00\x01\x00\x03abc")  # one packet of an unknown size
    await conn.ws.send(b"\xff\xff\xff\xff\xff\xff")  # a frame that makes no sense
    await conn.ws.send("this is not json")  # text that is not a frame either
    await conn.ws.send(frame(ltp_packet(INFY, 12345)))
    await eventually(lambda: manager.latest([INFY]))
    status = manager.status()
    assert status["packets_skipped"] == 2 and status["ticks_received"] == 1
    assert status["state"] == "connected" and status["last_error"] is None


async def test_order_updates_and_broker_messages_are_buffered_with_their_source(kite, make_manager):
    clock = Clock()
    manager = make_manager(clock=clock)
    await manager.subscribe([])
    conn = await nth(kite.connections, 0)
    clock.advance(1)
    order = {
        "order_id": "220303000308932",
        "order_timestamp": "2022-03-03 09:24:25",
        "status": "COMPLETE",
        "status_message": None,
        "tradingsymbol": "SBIN",
        "filled_quantity": 1,
        "average_price": 470,
        "user_id": "AB1234",
        "checksum": "abc123",
    }
    await conn.ws.send(json.dumps({"type": "order", "data": order}))
    await conn.ws.send(json.dumps({"type": "error", "data": "Invalid instrument token"}))
    await conn.ws.send(json.dumps({"type": "message", "data": "Market closes at 15:30"}))
    await conn.ws.send(json.dumps({"type": "instruments_meta", "data": {"count": 1}}))  # not one we keep
    await conn.ws.send(json.dumps({"type": "order", "data": "not an object"}))
    await eventually(lambda: len(manager.events) == 3)

    items, more = manager.events.recent(10)
    assert more is False
    assert [(e["type"], e["source"]) for e in items] == [
        ("order", "ticker"),
        ("error", "ticker"),
        ("message", "ticker"),
    ]
    assert items[0]["data"] == {
        "order_id": "220303000308932",
        "order_timestamp": "2022-03-03 09:24:25",
        "status": "COMPLETE",
        "tradingsymbol": "SBIN",
        "filled_quantity": 1,
        "average_price": 470,
    }
    assert items[1]["data"] == "Invalid instrument token" and items[2]["data"] == "Market closes at 15:30"
    assert all(e["received_at"] == clock.now for e in items)
    assert manager.status()["order_updates_buffered"] == 3


async def test_order_updates_from_postbacks_share_the_buffer(make_manager):
    manager = make_manager()
    manager.record_order_update({"order_id": "1", "status": "OPEN"}, source="ticker")
    manager.record_order_update({"order_id": "1", "status": "COMPLETE"}, source="postback")
    assert [(e["source"], e["data"]["status"]) for e in manager.events.recent(5)[0]] == [
        ("ticker", "OPEN"),
        ("postback", "COMPLETE"),
    ]


# --- dropped links, reconnects, new tokens ---------------------------------------------------------------------------


async def test_reconnect_resubscribes_every_token_with_its_mode(kite, make_manager):
    manager = make_manager()
    a, b, c = (Target(token_for(n, 1), f"NSE:S{n}") for n in (1, 2, 3))
    await manager.subscribe([a], "ltp")
    await manager.subscribe([b], "full")
    await manager.subscribe([c], "quote")
    first = await nth(kite.connections, 0)
    await eventually(lambda: len(first.received) == 6)

    first.drop()
    second = await nth(kite.connections, 1)
    await eventually(lambda: len(second.received) == 4)
    assert second.received == [
        {"a": "subscribe", "v": [a.token, b.token, c.token]},
        {"a": "mode", "v": ["ltp", [a.token]]},
        {"a": "mode", "v": ["quote", [c.token]]},
        {"a": "mode", "v": ["full", [b.token]]},
    ]
    await eventually(lambda: manager.status()["connected"])
    assert manager.status()["reconnects"] == 1
    await second.ws.send(frame(ltp_packet(a.token, 777)))
    await eventually(lambda: manager.latest([a.token]) and manager.latest([a.token])[0].tick["last_price"] == 7.77)


async def test_a_clean_close_by_the_server_reconnects_too(kite, make_manager):
    manager = make_manager()
    await manager.subscribe([INFY_T])
    first = await nth(kite.connections, 0)
    await first.ws.close()
    second = await nth(kite.connections, 1)
    await eventually(lambda: len(second.received) == 2)
    assert manager.status()["reconnects"] == 1


async def test_a_new_access_token_reconnects_with_it(kite, make_manager):
    tokens = FakeTokens("day-one-token-1")
    manager = make_manager(tokens)
    await manager.subscribe([INFY_T], "full")
    first = await nth(kite.connections, 0)
    assert first.query["access_token"] == ["day-one-token-1"]

    tokens.token = "day-two-token-2"
    second = await nth(kite.connections, 1)
    assert second.query["access_token"] == ["day-two-token-2"]
    await asyncio.wait_for(first.closed.wait(), 3)  # the old link was closed by us
    await eventually(lambda: len(second.received) == 2)
    assert second.received == [{"a": "subscribe", "v": [INFY]}, {"a": "mode", "v": ["full", [INFY]]}]
    assert manager.status()["reconnects"] == 1 and manager.state == "connected"
    assert manager.status()["last_error"] is None  # a new login is not an error


async def test_transient_failures_back_off_retry_and_say_what_went_wrong(kite, make_manager):
    attempts: list[int] = []
    backoffs: list[int] = []
    failures = [OSError("network is unreachable"), TimeoutError(), OSError("connection refused")]

    async def flaky(url: str) -> Any:
        attempts.append(1)
        if failures:
            raise failures.pop(0)
        return await direct(url)

    def backoff(attempt: int) -> float:
        backoffs.append(attempt)
        return 0.01

    manager = make_manager(connect=flaky, backoff=backoff)
    await manager.subscribe([INFY_T])  # returns as soon as the first verdict is in: not connected, but retrying
    assert manager.state in ("retrying", "connected")
    await eventually(lambda: manager.status()["connected"])
    assert len(attempts) == 4 and backoffs[:3] == [0, 1, 2]  # the delay grows with each consecutive failure
    assert "refused" in manager.status()["last_error"]
    conn = await nth(kite.connections, 0)
    await eventually(lambda: len(conn.received) == 2)  # and the subscription made meanwhile was applied
    assert manager.status()["reconnects"] == 0  # the first connection that worked is not a "re"connect


async def test_transient_failure_is_not_a_subscribe_error(make_manager):
    async def down(url: str) -> Any:
        raise OSError("connection refused")

    manager = make_manager(connect=down)
    assert await manager.subscribe([INFY_T]) == {INFY: "subscribed"}
    assert manager.subscriptions() == {INFY: "quote"}  # kept; it will be applied when Kite is reachable
    assert manager.state == "retrying"


async def test_without_a_token_after_a_drop_it_waits_for_a_login_then_resumes(kite, make_manager):
    tokens = FakeTokens("expiring-token-1")
    manager = make_manager(tokens)
    await manager.subscribe([INFY_T])
    first = await nth(kite.connections, 0)
    tokens.token = None
    first.drop()
    await eventually(lambda: manager.state == "waiting_for_login")
    await asyncio.sleep(0.1)
    assert kite.attempts == 1  # it does not knock on Kite's door with a token it does not have

    tokens.token = "fresh-login-token-2"
    second = await nth(kite.connections, 1)
    assert second.query["access_token"] == ["fresh-login-token-2"]
    await eventually(lambda: len(second.received) == 2)


async def test_a_silent_link_is_replaced_but_a_heartbeat_keeps_it(kite, make_manager):
    timings = dataclasses.replace(FAST, idle_timeout_s=0.12, early_close_s=5.0)
    manager = make_manager(timings=timings)
    await manager.subscribe([INFY_T])
    first = await nth(kite.connections, 0)
    for _ in range(15):  # heartbeats every 30 ms for ~0.45 s, far longer than the idle limit
        await first.ws.send(b"\x00")
        await asyncio.sleep(0.03)
    assert manager.status()["reconnects"] == 0 and len(kite.connections) == 1

    # now silence: it gives up on each silent link in turn. Four of those in a row (each one shorter than
    # early_close_s and without a frame) would block the ticker if they counted as "Kite hung up on us".
    await eventually(lambda: manager.status()["reconnects"] >= 4)
    assert "no data" in manager.status()["last_error"]
    assert manager.state != "blocked"


# --- refusals --------------------------------------------------------------------------------------------------------


async def test_a_refused_handshake_stops_the_retries_and_subscribe_reports_it(kite, make_manager):
    kite.refuse_with = 403
    manager = make_manager(FakeTokens("rejected-token-1"))
    with pytest.raises(TickerError) as excinfo:
        await manager.subscribe([INFY_T])
    message = str(excinfo.value)
    assert "HTTP 403" in message and "paid Kite Connect plan" in message and "ticker_stop" in message
    assert manager.subscriptions() == {}  # nothing from the failed call stays
    assert manager.state == "blocked" and kite.attempts == 1

    await asyncio.sleep(0.25)  # a retry loop with a 10 ms backoff would have knocked a dozen times by now
    assert kite.attempts == 1
    status = manager.status()
    assert status["blocked"] == message and "403" in status["last_error"]
    assert status["connected"] is False and status["running"] is True
    with pytest.raises(TickerError):
        await manager.subscribe([INFY_T])  # same token: refused up front, without another attempt
    assert kite.attempts == 1


async def test_a_different_token_ends_the_block_and_subscribe_works_again(kite, make_manager):
    kite.refuse_with = 403
    tokens = FakeTokens("rejected-token-1")
    manager = make_manager(tokens)
    with pytest.raises(TickerError):
        await manager.subscribe([INFY_T])

    kite.refuse_with = None
    tokens.token = "fresh-login-token-2"
    assert await manager.subscribe([INFY_T]) == {INFY: "subscribed"}  # no waiting for the next poll
    assert manager.state == "connected" and manager.status()["blocked"] is None
    conn = await nth(kite.connections, 0)
    assert conn.query["access_token"] == ["fresh-login-token-2"]


async def test_a_block_mid_session_keeps_the_subscriptions_for_the_next_login(kite, make_manager):
    tokens = FakeTokens("morning-token-001")
    manager = make_manager(tokens)
    await manager.subscribe([INFY_T], "full")
    first = await nth(kite.connections, 0)

    kite.refuse_with = 403
    first.drop()
    await eventually(lambda: manager.state == "blocked")
    attempts = kite.attempts
    await asyncio.sleep(0.2)
    assert kite.attempts == attempts and manager.subscriptions() == {INFY: "full"}

    kite.refuse_with = None
    tokens.token = "evening-token-002"
    second = await nth(kite.connections, 1)
    await eventually(lambda: len(second.received) == 2)
    assert second.received[1] == {"a": "mode", "v": ["full", [INFY]]}
    assert manager.state == "connected"


async def test_ticker_stop_clears_a_refusal_so_the_same_token_can_be_tried_again(kite, make_manager):
    kite.refuse_with = 403
    manager = make_manager()
    with pytest.raises(TickerError):
        await manager.subscribe([INFY_T])
    await manager.stop()
    kite.refuse_with = None
    assert await manager.subscribe([INFY_T]) == {INFY: "subscribed"}
    assert manager.state == "connected"


async def test_other_http_errors_are_retried_not_treated_as_a_refusal(kite, make_manager):
    kite.refuse_with = 502
    manager = make_manager()
    await manager.subscribe([INFY_T])
    await eventually(lambda: kite.attempts >= 3)
    assert manager.state in ("retrying", "connecting") and "502" in manager.status()["last_error"]
    kite.refuse_with = None
    await eventually(lambda: manager.status()["connected"])


async def test_kite_closing_right_after_connecting_twice_counts_as_a_refusal(kite, make_manager):
    kite.close_on_connect = True
    manager = make_manager()
    await manager.subscribe([INFY_T])
    await eventually(lambda: manager.state == "blocked")
    assert kite.attempts == 2
    assert "right after connecting" in manager.status()["blocked"]
    await asyncio.sleep(0.2)
    assert kite.attempts == 2


async def test_one_early_close_is_only_a_glitch(kite, make_manager):
    kite.close_on_connect = True
    manager = make_manager(backoff=lambda attempt: 0.2)  # time enough to fix the server before the second attempt
    await manager.subscribe([INFY_T])
    await eventually(lambda: kite.attempts >= 1)
    kite.close_on_connect = False
    await eventually(lambda: manager.status()["connected"] and len(kite.connections) >= 2)
    await asyncio.sleep(0.1)
    assert manager.state == "connected"


async def test_a_bad_ticker_url_fails_for_good_and_says_so(settings_factory):
    settings = settings_factory(KITE_TICKER_URL="https://not-a-websocket.example")
    manager = TickerManager(settings, FakeTokens(), clock=lambda: FIXED_NOW, timings=FAST)
    try:
        with pytest.raises(TickerError, match="KITE_TICKER_URL"):
            await manager.subscribe([INFY_T])
        assert manager.state == "failed" and manager.status()["running"] is False
        with pytest.raises(TickerError, match="KITE_TICKER_URL"):
            await manager.subscribe([INFY_T])  # still failed: needs a config fix and a restart
    finally:
        await manager.stop()


# --- stop() ----------------------------------------------------------------------------------------------------------


async def test_stop_closes_the_socket_clears_state_and_leaves_no_task_behind(kite, make_manager):
    baseline = set(asyncio.all_tasks())
    manager = make_manager()
    await manager.subscribe([INFY_T, TATA_T], "full")
    conn = await nth(kite.connections, 0)
    await conn.ws.send(frame(ltp_packet(INFY, 1)))
    await eventually(lambda: manager.latest())
    manager.record_order_update({"order_id": "1"}, source="ticker")
    task = manager._task
    assert task is not None and not task.done()

    await manager.stop()
    assert task.done()
    await asyncio.wait_for(conn.closed.wait(), 3)
    status = manager.status()
    assert status["state"] == "idle" and status["running"] is False and status["connected"] is False
    assert manager.subscriptions() == {} and manager.latest() == [] and status["reconnects"] == 0
    assert len(manager.events) == 1  # the order-update history survives a stop
    await eventually(lambda: set(asyncio.all_tasks()) == baseline)  # nothing of ours (or websockets') left running

    await manager.stop()  # twice is fine
    assert await manager.subscribe([INFY_T]) == {INFY: "subscribed"}  # and it starts afresh
    assert len(kite.connections) == 2 and manager.status()["reconnects"] == 0


async def test_stop_on_a_manager_that_never_started_is_a_no_op(make_manager):
    manager = make_manager()
    await manager.stop()
    await manager.stop()
    assert manager.state == "idle"


async def test_stop_while_connecting_does_not_hang(kite, make_manager):
    async def never(url: str) -> Any:
        await asyncio.sleep(3600)

    manager = make_manager(connect=never, timings=dataclasses.replace(FAST, connect_wait_s=0.05))
    await manager.subscribe([INFY_T])  # waits 50 ms, then returns: still connecting
    assert manager.state == "connecting"
    await asyncio.wait_for(manager.stop(), 2)
    assert manager.state == "idle" and manager.status()["running"] is False


def test_stop_from_another_event_loop_after_the_server_loop_is_gone(settings_factory):
    """`kite-connect-mcp serve` closes the runtime with a fresh asyncio.run() once the server's loop has ended."""

    async def server_loop() -> TickerManager:
        server = KiteServer()
        await server.start()
        manager = TickerManager(
            settings_factory(KITE_TICKER_URL=server.url),
            FakeTokens(),
            clock=lambda: FIXED_NOW,
            connect=direct,
            timings=FAST,
            backoff=lambda attempt: 0.01,
        )
        await manager.subscribe([INFY_T])
        assert manager.status()["connected"]
        await server.stop()
        return manager  # the loop ends with the connection task still running

    manager = asyncio.run(server_loop())

    async def shutdown() -> None:
        await manager.stop()

    asyncio.run(shutdown())
    assert manager.state == "idle" and manager.status()["running"] is False


# --- secrets and the real client -------------------------------------------------------------------------------------


async def test_the_access_token_never_reaches_status_errors_or_logs(kite, make_manager, caplog):
    caplog.set_level(logging.DEBUG)
    token = "SECRET-ACCESS-TOKEN-9f3a7c"
    seen: list[str] = []

    async def failing(url: str) -> Any:
        seen.append(url)
        raise OSError(f"cannot reach {url}")  # an exception message that carries the whole URL

    manager = make_manager(FakeTokens(token), connect=failing)
    await manager.subscribe([INFY_T])
    await eventually(lambda: len(seen) >= 2)
    assert token in seen[0]  # the token was in the URL that the connection got...
    status = json.dumps(manager.status(), default=str)
    assert token not in status and API_KEY not in status and API_SECRET not in status
    assert "access_token=[redacted]" in manager.status()["last_error"]

    # ...and a real connection, with logging at DEBUG everywhere, logs it nowhere
    working = make_manager(FakeTokens(token))
    await working.subscribe([INFY_T])
    await eventually(lambda: kite.connections)
    kite.refuse_with = 403
    kite.connections[0].drop()
    await eventually(lambda: working.state == "blocked")
    ours = "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("kite_connect_mcp"))
    assert ours  # it did log something
    for secret in (token, API_KEY, API_SECRET, ACCESS_TOKEN):
        assert secret not in ours
    assert not re.search(r"access_token=(?!\[redacted\])", ours)  # no URL query with a real value


async def test_the_default_connector_works_against_a_real_server(kite, make_manager, monkeypatch):
    for name in list(os.environ):
        if name.lower().endswith("_proxy"):
            monkeypatch.delenv(name)
    manager = make_manager(connect=None)
    await manager.subscribe([INFY_T], "ltp")
    conn = await nth(kite.connections, 0)
    await eventually(lambda: len(conn.received) == 2)
    await conn.ws.send(frame(ltp_packet(INFY, 4242)))
    await eventually(lambda: manager.latest())
    assert manager.latest()[0].tick["last_price"] == 42.42
    await manager.stop()
    await asyncio.wait_for(conn.closed.wait(), 3)


# --- the tools, end to end -------------------------------------------------------------------------------------------


_HANG_UP = object()


class FakeWS:
    """An in-process connection: the test pushes what Kite would send and reads what the server sent."""

    def __init__(self) -> None:
        self.incoming: asyncio.Queue[Any] = asyncio.Queue()
        self.sent: list[dict[str, Any]] = []
        self.closed = False

    async def recv(self) -> Any:
        item = await self.incoming.get()
        if item is _HANG_UP:
            self.incoming.put_nowait(_HANG_UP)
            raise ConnectionClosedOK(None, None)
        return item

    async def send(self, message: str) -> None:
        if self.closed:
            raise ConnectionClosedOK(None, None)
        self.sent.append(json.loads(message))

    async def close(self) -> None:
        self.closed = True
        self.incoming.put_nowait(_HANG_UP)

    def push(self, data: bytes | str) -> None:
        self.incoming.put_nowait(data)


class FakeConnect:
    def __init__(self, fail_with: Exception | None = None) -> None:
        self.urls: list[str] = []
        self.connections: list[FakeWS] = []
        self.fail_with = fail_with

    async def __call__(self, url: str) -> FakeWS:
        self.urls.append(url)
        if self.fail_with is not None:
            raise self.fail_with
        ws = FakeWS()
        self.connections.append(ws)
        return ws


@dataclass(frozen=True)
class StubInstrument:
    instrument_token: int
    exchange: str
    tradingsymbol: str

    @property
    def key(self) -> str:
        return f"{self.exchange}:{self.tradingsymbol}"


class StubInstruments:
    """The two methods of InstrumentStore that the ticker tools use."""

    def __init__(self, available: bool = True) -> None:
        self.available = available
        self.items = [
            StubInstrument(INFY, "NSE", "INFY"),
            StubInstrument(TATAMOTORS, "NSE", "TATAMOTORS"),
            StubInstrument(NIFTY_50, "NSE", "NIFTY 50"),
        ]

    async def lookup(self, exchange: str, tradingsymbol: str) -> StubInstrument | None:
        if not self.available:
            raise InstrumentsUnavailable("no instrument list")
        return next((i for i in self.items if (i.exchange, i.tradingsymbol) == (exchange, tradingsymbol)), None)

    async def by_token(self, token: int) -> StubInstrument | None:
        if not self.available:
            raise InstrumentsUnavailable("no instrument list")
        return next((i for i in self.items if i.instrument_token == token), None)


async def no_sleep(_: float) -> None:
    return None


@pytest.fixture
async def make_tool_server(settings_factory: Callable[..., Any], fake_kite: Any) -> AsyncIterator[Callable[..., Any]]:
    runtimes = []

    def make(connect: Any = None, *, clock: Callable[[], dt.datetime] | None = None, instruments: Any = "stub", **env):
        rt = build_runtime(
            settings_factory(**env),
            transport=fake_kite.transport(),
            clock=clock or (lambda: FIXED_NOW),
            sleep=no_sleep,
            ticker_connect=connect or FakeConnect(),
        )
        rt.instruments = StubInstruments() if instruments == "stub" else instruments
        runtimes.append(rt)
        server, reg = build_server(rt)
        return server, reg, rt

    yield make
    for rt in runtimes:
        await rt.aclose()


async def test_the_ticker_tools_and_their_kinds(make_tool_server):
    server, reg, _ = make_tool_server()
    kinds = {info.name: (info.kind, info.toolset) for info in reg.registered if info.toolset == "ticker"}
    assert kinds == {
        "ticker_subscribe": ("local", "ticker"),
        "ticker_unsubscribe": ("local", "ticker"),
        "ticker_get_ticks": ("read", "ticker"),
        "ticker_status": ("read", "ticker"),
        "ticker_order_updates": ("read", "ticker"),
        "ticker_stop": ("local", "ticker"),
    }
    async with Client(server) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools if t.name.startswith("ticker_")}
    assert all(t.annotations.read_only_hint is False for n, t in tools.items() if kinds[n][0] == "local")
    assert all(t.annotations.read_only_hint is True for n, t in tools.items() if kinds[n][0] == "read")
    assert all(t.annotations.destructive_hint is False for t in tools.values())
    subscribe_schema = tools["ticker_subscribe"].input_schema
    assert subscribe_schema["required"] == ["instruments"]
    assert subscribe_schema["properties"]["mode"]["enum"] == ["ltp", "quote", "full"]
    assert subscribe_schema["properties"]["mode"]["default"] == "quote"
    assert "EXCHANGE:TRADINGSYMBOL" in subscribe_schema["properties"]["instruments"]["description"]


async def test_subscribe_read_ticks_unsubscribe_and_stop(make_tool_server):
    clock = Clock()
    connect = FakeConnect()
    server, _, rt = make_tool_server(connect, clock=clock)
    async with Client(server) as client:
        answer = tool_json(
            await client.call_tool("ticker_subscribe", {"instruments": ["NSE:INFY", TATAMOTORS], "mode": "full"})
        )
        assert answer["connected"] is True and answer["state"] == "connected" and answer["mode"] == "full"
        assert answer["subscribed_total"] == 2 and answer["limit"] == 1000
        assert answer["instruments"] == [
            {"instrument": "NSE:INFY", "instrument_token": INFY, "result": "subscribed"},
            {"instrument": "NSE:TATAMOTORS", "instrument_token": TATAMOTORS, "result": "subscribed"},  # found by token
        ]
        ws = connect.connections[0]
        await eventually(lambda: len(ws.sent) == 2)
        assert ws.sent == [
            {"a": "subscribe", "v": [INFY, TATAMOTORS]},
            {"a": "mode", "v": ["full", [INFY, TATAMOTORS]]},
        ]
        assert "api_key=" in connect.urls[0] and "access_token=" in connect.urls[0]

        before = tool_json(await client.call_tool("ticker_get_ticks", {}))  # connected, but nothing arrived yet
        assert before["count"] == 0 and before["ticks"] == []
        assert before["no_tick_yet"] == ["NSE:INFY", "NSE:TATAMOTORS"]

        ws.push(
            frame(
                full_packet(
                    INFY,
                    last=150005,
                    last_qty=3,
                    average=149900,
                    volume=42,
                    buy_qty=10,
                    sell_qty=20,
                    open_=149000,
                    high=151000,
                    low=148500,
                    close=148000,
                    oi=5000,
                    stamp=int(FIXED_NOW.timestamp()),
                    buy=BIDS,
                    sell=OFFERS,
                )
            )
        )
        await eventually(lambda: rt.ticker.latest())
        clock.advance(2.5)
        got = tool_json(await client.call_tool("ticker_get_ticks", {}))
        assert got["count"] == 1 and got["connected"] is True and got["no_tick_yet"] == ["NSE:TATAMOTORS"]
        tick = got["ticks"][0]
        assert tick["instrument"] == "NSE:INFY" and tick["instrument_token"] == INFY and tick["mode"] == "full"
        assert tick["last_price"] == 1500.05 and tick["volume_traded"] == 42 and tick["oi"] == 5000
        assert tick["ohlc"] == {"open": 1490.0, "high": 1510.0, "low": 1485.0, "close": 1480.0}
        assert tick["change"] == round((1500.05 - 1480.0) * 100 / 1480.0, 4)
        assert tick["depth"]["buy"][0] == {"quantity": 100, "price": 1500.0, "orders": 1}
        assert tick["age_s"] == 2.5 and tick["received_at"] == FIXED_NOW.isoformat()
        assert tick["exchange_timestamp"] == FIXED_NOW.replace(microsecond=0).isoformat()

        named = tool_json(await client.call_tool("ticker_get_ticks", {"instruments": ["NSE:INFY", "NSE:TCS", 12345]}))
        assert [t["instrument"] for t in named["ticks"]] == ["NSE:INFY"]
        assert named["not_subscribed"] == ["12345", "NSE:TCS"]  # a token and a name the instrument list does not know
        assert "no_tick_yet" not in named

        clock.advance(100)
        fresh_only = tool_json(await client.call_tool("ticker_get_ticks", {"max_age_s": 10}))
        assert fresh_only["count"] == 0 and fresh_only["stale"] == {"NSE:INFY": 102.5}

        status = tool_json(await client.call_tool("ticker_status", {}))
        assert status["enabled"] is True and status["connected"] and status["logged_in"]
        assert status["subscriptions"] == {"total": 2, "limit": 1000, "ltp": 0, "quote": 0, "full": 2}
        assert status["ticks_cached"] == 1 and status["reconnects"] == 0 and status["postback_path"] is None
        assert "access_token" not in json.dumps(status) and ACCESS_TOKEN not in json.dumps(status)

        gone = tool_json(await client.call_tool("ticker_unsubscribe", {"instruments": ["NSE:INFY", "NSE:TCS"]}))
        assert gone == {"unsubscribed": ["NSE:INFY"], "not_subscribed": ["NSE:TCS"], "subscribed_total": 1}
        await eventually(lambda: ws.sent[-1] == {"a": "unsubscribe", "v": [INFY]})
        assert tool_json(await client.call_tool("ticker_get_ticks", {}))["ticks"] == []

        stopped = tool_json(await client.call_tool("ticker_stop", {}))
        assert stopped == {"stopped": True, "subscriptions_dropped": 1, "state": "idle"}
        assert ws.closed
        assert tool_json(await client.call_tool("ticker_status", {}))["state"] == "idle"
        again = tool_json(await client.call_tool("ticker_stop", {}))  # harmless when nothing runs
        assert again["subscriptions_dropped"] == 0


async def test_empty_subscribe_opens_the_stream_and_order_updates_are_paged(make_tool_server):
    clock = Clock()
    connect = FakeConnect()
    server, _, rt = make_tool_server(connect, clock=clock)
    async with Client(server) as client:
        empty = tool_json(await client.call_tool("ticker_order_updates", {}))
        assert empty["count"] == 0 and "ticker_subscribe" in empty["note"]

        opened = tool_json(await client.call_tool("ticker_subscribe", {"instruments": []}))
        assert opened["connected"] is True and opened["instruments"] == []
        ws = connect.connections[0]
        for n in range(1, 6):
            clock.advance(1)  # received_at is when the frame is processed: let each one land before the next tick
            update = {"order_id": str(n), "status": "COMPLETE", "tradingsymbol": "SBIN", "user_id": "AB1234"}
            ws.push(json.dumps({"type": "order", "data": update}))
            await eventually(lambda n=n: len(rt.ticker.events) == n)
        clock.advance(1)
        ws.push(json.dumps({"type": "error", "data": "Token expired"}))
        await eventually(lambda: len(rt.ticker.events) == 6)

        latest = tool_json(await client.call_tool("ticker_order_updates", {"limit": 3}))
        assert latest["count"] == 3 and latest["buffered"] == 6 and "more" not in latest
        assert [(i["seq"], i["type"]) for i in latest["items"]] == [(4, "order"), (5, "order"), (6, "error")]
        assert latest["items"][0]["source"] == "ticker" and latest["items"][2]["data"] == "Token expired"
        assert latest["items"][0]["data"] == {"order_id": "4", "status": "COMPLETE", "tradingsymbol": "SBIN"}

        page = tool_json(
            await client.call_tool("ticker_order_updates", {"limit": 2, "since": "2026-11-04T10:00:00+05:30"})
        )
        assert [i["seq"] for i in page["items"]] == [1, 2] and page["more"] is True  # oldest first
        nxt = tool_json(
            await client.call_tool("ticker_order_updates", {"limit": 10, "since": page["items"][-1]["received_at"]})
        )
        assert [i["seq"] for i in nxt["items"]] == [3, 4, 5, 6] and "more" not in nxt
        naive = tool_json(await client.call_tool("ticker_order_updates", {"since": "2026-11-04 10:00:03"}))  # IST
        assert [i["seq"] for i in naive["items"]] == [4, 5, 6]

        bad = await client.call_tool("ticker_order_updates", {"since": "yesterday"})
        assert bad.is_error and "ISO 8601" in tool_text(bad)


async def test_names_need_the_instrument_list_but_tokens_do_not(make_tool_server):
    connect = FakeConnect()
    server, _, _ = make_tool_server(connect, instruments=StubInstruments(available=False))
    async with Client(server) as client:
        refused = await client.call_tool("ticker_subscribe", {"instruments": ["NSE:INFY"]})
        assert refused.is_error and "numeric instrument tokens" in tool_text(refused)
        assert connect.urls == []  # nothing was started for a call that could not work

        ok = tool_json(await client.call_tool("ticker_subscribe", {"instruments": [INFY, str(TATAMOTORS)]}))
        assert [i["instrument"] for i in ok["instruments"]] == [str(INFY), str(TATAMOTORS)]  # no name known
        again = tool_json(await client.call_tool("ticker_get_ticks", {"instruments": [INFY]}))
        assert again["no_tick_yet"] == [str(INFY)]
        gone = tool_json(await client.call_tool("ticker_unsubscribe", {"instruments": [f" {INFY} "]}))
        assert gone["unsubscribed"] == [str(INFY)]


async def test_a_failing_label_lookup_never_stops_a_subscribe_by_token(make_tool_server):
    class Flaky(StubInstruments):
        async def by_token(self, token: int) -> StubInstrument | None:
            raise NetworkError("the instrument download failed")  # not even "unavailable": still just a missing label

    server, _, _ = make_tool_server(instruments=Flaky())
    async with Client(server) as client:
        answer = tool_json(await client.call_tool("ticker_subscribe", {"instruments": [INFY]}))
    assert answer["instruments"] == [{"instrument": str(INFY), "instrument_token": INFY, "result": "subscribed"}]


async def test_names_learned_from_a_subscribe_work_for_later_calls_without_the_list(make_tool_server):
    server, _, rt = make_tool_server()
    async with Client(server) as client:
        tool_json(await client.call_tool("ticker_subscribe", {"instruments": ["nse:infy"]}))
        rt.instruments.available = False  # the instrument list goes away
        got = tool_json(await client.call_tool("ticker_get_ticks", {"instruments": ["NSE:INFY"]}))
        assert got["no_tick_yet"] == ["NSE:INFY"]  # the manager remembered the name
        gone = tool_json(await client.call_tool("ticker_unsubscribe", {"instruments": ["NSE:INFY"]}))
        assert gone["unsubscribed"] == ["NSE:INFY"]


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ({"instruments": ["NSE:NOSUCH"]}, "not in the instrument list"),
        ({"instruments": ["INFY"]}, "EXCHANGE:TRADINGSYMBOL"),
        ({"instruments": [0]}, "positive 32-bit"),
        ({"instruments": [2**32]}, "positive 32-bit"),
        ({"instruments": ["NSE:INFY"], "mode": "everything"}, "mode"),
    ],
)
async def test_bad_subscribe_arguments_are_refused_before_anything_starts(make_tool_server, args, message):
    connect = FakeConnect()
    server, _, rt = make_tool_server(connect)
    async with Client(server) as client:
        result = await client.call_tool("ticker_subscribe", args)
    assert result.is_error and message in tool_text(result)
    assert connect.urls == [] and rt.ticker.state == "idle"


async def test_subscribe_over_the_limit_is_an_error_and_changes_nothing(make_tool_server):
    server, _, rt = make_tool_server(KITE_MCP_TICKER_MAX_SUBSCRIPTIONS="1")
    async with Client(server) as client:
        tool_json(await client.call_tool("ticker_subscribe", {"instruments": ["NSE:INFY"]}))
        result = await client.call_tool("ticker_subscribe", {"instruments": ["NSE:INFY", "NSE:TATAMOTORS"]})
    assert result.is_error and "limit of 1" in tool_text(result)
    assert list(rt.ticker.subscriptions()) == [INFY]


async def test_not_logged_in_is_a_clear_tool_error(make_tool_server, tmp_path):
    connect = FakeConnect()
    server, _, _ = make_tool_server(connect, KITE_TOKEN_FILE=str(tmp_path / "no-such-token.json"))
    async with Client(server) as client:
        result = await client.call_tool("ticker_subscribe", {"instruments": ["NSE:INFY"]})
        assert result.is_error and "not logged in" in tool_text(result)
        status = tool_json(await client.call_tool("ticker_status", {}))
        assert status["logged_in"] is False and status["state"] == "idle"
    assert connect.urls == []


async def test_a_refused_connection_is_reported_as_an_error_with_the_plan_requirement(make_tool_server):
    refusal = InvalidStatus(Response(403, "Forbidden", Headers()))
    server, _, rt = make_tool_server(FakeConnect(fail_with=refusal))
    async with Client(server) as client:
        result = await client.call_tool("ticker_subscribe", {"instruments": ["NSE:INFY"]})
        assert result.is_error
        text = tool_text(result)
        assert "HTTP 403" in text and "paid Kite Connect plan" in text and "Not retrying" in text
        assert ACCESS_TOKEN not in text and API_KEY not in text
        status = tool_json(await client.call_tool("ticker_status", {}))
        assert status["state"] == "blocked" and status["subscriptions"]["total"] == 0
        assert "paid Kite Connect plan" in status["requirements"] and "new login" in status["help"]
        assert status["blocked"] and "HTTP 403" in status["last_error"]
    assert rt.ticker.state == "blocked"


async def test_a_transient_failure_is_reported_in_the_answer_not_as_an_error(make_tool_server):
    server, _, _ = make_tool_server(FakeConnect(fail_with=OSError("connection refused")))
    async with Client(server) as client:
        answer = tool_json(await client.call_tool("ticker_subscribe", {"instruments": ["NSE:INFY"]}))
        assert answer["connected"] is False and answer["state"] == "retrying"
        assert "connection refused" in answer["last_error"] and "retries by itself" in answer["note"]
        ticks = tool_json(await client.call_tool("ticker_get_ticks", {}))
        assert ticks["count"] == 0 and ticks["connected"] is False and "no_tick_yet" in ticks


async def test_disabled_ticker_exposes_only_a_status_tool_that_explains(make_tool_server):
    server, reg, _ = make_tool_server(KITE_MCP_TICKER_ENABLED="false", KITE_MCP_POSTBACK_PATH="/postback")
    assert [i.name for i in reg.registered if i.toolset == "ticker"] == ["ticker_status"]
    async with Client(server) as client:
        names = {t.name for t in (await client.list_tools()).tools}
        assert "ticker_subscribe" not in names and "ticker_status" in names
        status = tool_json(await client.call_tool("ticker_status", {}))
    assert status["enabled"] is False
    assert "KITE_MCP_TICKER_ENABLED=false" in status["message"] and "paid Kite Connect plan" in status["message"]
    assert "postback" in status and "/postback" in status["postback"]


async def test_ticker_tools_are_available_in_read_only_mode_and_hidden_by_toolsets(make_tool_server):
    _, reg, _ = make_tool_server(KITE_MCP_READ_ONLY="true")  # they never change the account
    assert {i.name for i in reg.registered if i.toolset == "ticker"} >= {"ticker_subscribe", "ticker_stop"}
    _, reg, _ = make_tool_server(KITE_MCP_TOOLSETS="account")
    assert not [i for i in reg.registered if i.toolset == "ticker"]
    _, reg, _ = make_tool_server(KITE_MCP_EXCLUDED_TOOLS="ticker_stop")
    assert "ticker_stop" not in {i.name for i in reg.registered} and "ticker_status" in {i.name for i in reg.registered}


async def test_closing_the_runtime_stops_the_ticker(make_tool_server):
    connect = FakeConnect()
    server, _, rt = make_tool_server(connect)
    async with Client(server) as client:
        tool_json(await client.call_tool("ticker_subscribe", {"instruments": ["NSE:INFY"]}))
    ws = connect.connections[0]
    task = rt.ticker._task
    await rt.aclose()
    assert ws.closed and task.done() and rt.ticker.state == "idle"
    leftovers = [t for t in asyncio.all_tasks() if t.get_name() == "kite-ticker" and not t.done()]
    assert leftovers == []


async def test_the_connection_outlives_the_tool_call_that_started_it(make_tool_server):
    """Each `async with Client(...)` runs the server in its own task group, as an HTTP request would."""
    connect = FakeConnect()
    server, _, rt = make_tool_server(connect)
    async with Client(server) as client:
        tool_json(await client.call_tool("ticker_subscribe", {"instruments": ["NSE:INFY"]}))
    await asyncio.sleep(0.05)
    assert rt.ticker.status()["connected"] and not connect.connections[0].closed
    connect.connections[0].push(frame(ltp_packet(INFY, 100)))
    await eventually(lambda: rt.ticker.latest())
    async with Client(server) as client:
        got = tool_json(await client.call_tool("ticker_get_ticks", {}))
    assert got["ticks"][0]["last_price"] == 1.0


def test_random_frames_through_a_live_manager_never_kill_it(settings_factory):
    """Fuzz: whatever Kite sends, the connection task survives and keeps serving good ticks afterwards."""

    async def scenario() -> None:
        fake = FakeConnect()
        manager = TickerManager(settings_factory(), FakeTokens(), clock=lambda: FIXED_NOW, connect=fake, timings=FAST)
        try:
            await manager.subscribe([INFY_T])
            ws = fake.connections[0]
            rng = random.Random(7)
            for _ in range(300):
                ws.push(rng.randbytes(rng.randrange(0, 300)) if rng.random() < 0.7 else rng.randbytes(40).hex())
            ws.push(frame(ltp_packet(INFY, 31337)))
            await eventually(lambda: manager.latest() and manager.latest()[0].tick["last_price"] == 313.37)
            assert manager.state == "connected" and len(fake.connections) == 1
        finally:
            await manager.stop()

    asyncio.run(scenario())


class QuietUvicorn(uvicorn.Server):
    """uvicorn without its signal handlers: the test runner owns those."""

    @contextlib.contextmanager
    def capture_signals(self):
        yield


async def test_the_connection_survives_stateless_http_requests(kite, settings_factory, fake_kite):
    """The real HTTP app under uvicorn: every MCP request runs in its own task group and ends, yet the connection
    opened by the first one must still be there for the next ones."""
    settings = settings_factory(
        KITE_TICKER_URL=kite.url, KITE_MCP_TRANSPORT="http", KITE_MCP_BEARER_TOKEN=BEARER, KITE_MCP_POSTBACK_PATH="/pb"
    )
    rt = build_runtime(
        settings, transport=fake_kite.transport(), clock=lambda: FIXED_NOW, sleep=no_sleep, ticker_connect=direct
    )
    server, _ = build_server(rt)
    http_server = QuietUvicorn(
        uvicorn.Config(build_http_app(server, rt), host="127.0.0.1", port=0, log_level="warning", lifespan="on")
    )
    serving = asyncio.create_task(http_server.serve())
    try:
        await eventually(lambda: http_server.started)
        port = http_server.servers[0].sockets[0].getsockname()[1]

        async def call(name: str, args: dict[str, Any]) -> Any:
            url = f"http://127.0.0.1:{port}/mcp"
            async with (
                httpx2.AsyncClient(headers={"Authorization": f"Bearer {BEARER}"}) as http,
                Client(streamable_http_client(url, http_client=http)) as client,
            ):
                return tool_json(await client.call_tool(name, args))

        opened = await call("ticker_subscribe", {"instruments": [INFY], "mode": "ltp"})
        assert opened["connected"] is True
        conn = await nth(kite.connections, 0)
        await eventually(lambda: len(conn.received) == 2)
        await conn.ws.send(frame(ltp_packet(INFY, 123456)))
        await eventually(lambda: rt.ticker.latest())

        later = await call("ticker_get_ticks", {})  # another request, after the first one has long ended
        assert [t["last_price"] for t in later["ticks"]] == [1234.56]
        status = await call("ticker_status", {})
        assert status["connected"] is True and status["reconnects"] == 0 and len(kite.connections) == 1
        assert status["postback_path"] == "/pb"
    finally:
        http_server.should_exit = True
        await serving
        await rt.aclose()
    assert rt.ticker.state == "idle"
