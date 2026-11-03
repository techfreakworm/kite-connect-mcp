"""Kite postbacks over HTTP: the checksum, the body cap, what reaches the order-update buffer, and who can call it.

The app is the real one: `build_server` registers the tools (which adds the postback route hook) and `build_http_app`
turns it into the ASGI app behind the bearer-token wrapper, driven here through httpx2's ASGITransport."""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx2
import pytest
from mcp import Client

from conftest import API_SECRET, BEARER, tool_json
from kite_connect_mcp import postback as postback_module
from kite_connect_mcp.postback import MAX_BODY_BYTES, PostbackRoute, compute_checksum, verify_checksum
from kite_connect_mcp.server import build_http_app, build_server
from kite_connect_mcp.ticker.manager import MAX_TEXT_CHARS

HTTP = {"KITE_MCP_TRANSPORT": "http", "KITE_MCP_BEARER_TOKEN": BEARER, "KITE_MCP_POSTBACK_PATH": "/postback"}
ORDER_ID = "220303000308932"
STAMP = "2022-03-03 09:24:25"
SAMPLE: dict[str, Any] = {  # the shape of the sample in Kite's postback documentation
    "user_id": "AB1234",
    "unfilled_quantity": 0,
    "app_id": 1234,
    "placed_by": "AB1234",
    "order_id": ORDER_ID,
    "exchange_order_id": "1000000001482421",
    "parent_order_id": None,
    "status": "COMPLETE",
    "status_message": None,
    "status_message_raw": None,
    "order_timestamp": STAMP,
    "exchange_update_timestamp": STAMP,
    "exchange_timestamp": STAMP,
    "variety": "regular",
    "exchange": "NSE",
    "tradingsymbol": "SBIN",
    "instrument_token": 779521,
    "order_type": "MARKET",
    "transaction_type": "BUY",
    "validity": "DAY",
    "product": "CNC",
    "quantity": 1,
    "disclosed_quantity": 0,
    "price": 0,
    "trigger_price": 0,
    "average_price": 470,
    "filled_quantity": 1,
    "pending_quantity": 0,
    "cancelled_quantity": 0,
    "market_protection": 0,
    "meta": {},
    "tag": None,
    "guid": "XXXXXX",
}


def checksum_of(order_id: str, timestamp: str, secret: str = API_SECRET) -> str:
    """Kite's rule, spelled out independently of the implementation: SHA-256 of order_id + order_timestamp + secret."""
    return hashlib.sha256((order_id + timestamp + secret).encode()).hexdigest()


def postback(**changes: Any) -> dict[str, Any]:
    payload = {**SAMPLE, **changes}
    if "checksum" not in changes:
        payload["checksum"] = checksum_of(payload["order_id"], payload["order_timestamp"])
    return payload


@pytest.fixture
def make_app(runtime_factory: Callable[..., Any]) -> Callable[..., tuple[Any, Any, Any]]:
    def make(**overrides: str | None):
        rt = runtime_factory(**{**HTTP, **overrides})
        server, _ = build_server(rt)
        return rt, server, build_http_app(server, rt)

    return make


@pytest.fixture
async def http() -> AsyncIterator[Callable[[Any], httpx2.AsyncClient]]:
    clients: list[httpx2.AsyncClient] = []

    def make(app: Any) -> httpx2.AsyncClient:
        client = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://testserver")
        clients.append(client)
        return client

    yield make
    for client in clients:
        await client.aclose()


async def send(client: httpx2.AsyncClient, payload: Any, path: str = "/postback") -> httpx2.Response:
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    return await client.post(path, content=body, headers={"Content-Type": "application/json"})


def buffered(rt: Any) -> list[dict[str, Any]]:
    return rt.ticker.events.recent(500)[0]


# --- the checksum ----------------------------------------------------------------------------------------------------


def test_the_checksum_is_sha256_of_order_id_timestamp_and_secret():
    assert compute_checksum("1", "2", "3") == hashlib.sha256(b"123").hexdigest()
    assert compute_checksum(ORDER_ID, STAMP, API_SECRET) == checksum_of(ORDER_ID, STAMP)
    assert len(checksum_of("a", "b")) == 64


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (postback(), True),
        (postback(checksum=checksum_of(ORDER_ID, STAMP).upper()), True),
        (postback(checksum="  " + checksum_of(ORDER_ID, STAMP) + "\n"), True),
        (postback(checksum=checksum_of(ORDER_ID, STAMP, "another-secret")), False),
        (postback(checksum=checksum_of("220303000308933", STAMP)), False),  # a different order
        (postback(checksum=checksum_of(ORDER_ID, "2022-03-03 09:24:26")), False),  # a different timestamp
        (postback(checksum=""), False),
        (postback(checksum=12345), False),
        (postback(checksum=None), False),
        (postback(order_id=123456, checksum="x"), False),
        (postback(order_timestamp=None, checksum="x"), False),
        ({k: v for k, v in postback().items() if k != "checksum"}, False),
        ({k: v for k, v in postback().items() if k != "order_id"}, False),
        ({}, False),
    ],
)
def test_verify_checksum(payload, expected):
    assert verify_checksum(payload, API_SECRET) is expected


def test_the_comparison_is_constant_time(monkeypatch):
    calls: list[tuple[bytes, bytes]] = []
    real = postback_module.hmac.compare_digest

    def spy(a: bytes, b: bytes) -> bool:
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(postback_module.hmac, "compare_digest", spy)
    assert verify_checksum(postback(), API_SECRET) is True
    assert verify_checksum(postback(checksum="0" * 64), API_SECRET) is False
    assert len(calls) == 2


# --- the endpoint ----------------------------------------------------------------------------------------------------


async def test_a_valid_postback_is_accepted_and_buffered_without_identifiers_or_the_checksum(make_app, http):
    rt, _, app = make_app()
    response = await send(http(app), postback())
    assert response.status_code == 200 and response.json() == {"status": "ok"}
    [entry] = buffered(rt)
    assert entry["source"] == "postback" and entry["type"] == "order"
    assert entry["data"] == {
        "order_id": ORDER_ID,
        "exchange_order_id": "1000000001482421",
        "status": "COMPLETE",
        "order_timestamp": STAMP,
        "exchange_update_timestamp": STAMP,
        "exchange_timestamp": STAMP,
        "variety": "regular",
        "exchange": "NSE",
        "tradingsymbol": "SBIN",
        "instrument_token": 779521,
        "order_type": "MARKET",
        "transaction_type": "BUY",
        "validity": "DAY",
        "product": "CNC",
        "quantity": 1,
        "disclosed_quantity": 0,
        "price": 0,
        "trigger_price": 0,
        "average_price": 470,
        "filled_quantity": 1,
        "pending_quantity": 0,
        "cancelled_quantity": 0,
        "unfilled_quantity": 0,
        "market_protection": 0,
    }
    for private in ("checksum", "user_id", "placed_by", "app_id", "guid", "meta"):
        assert private not in entry["data"]


async def test_postbacks_and_websocket_updates_share_one_buffer_that_the_tool_reads(make_app, http):
    rt, server, app = make_app()
    rt.ticker.record_order_update({"order_id": "1", "status": "OPEN"}, source="ticker")
    assert (await send(http(app), postback())).status_code == 200
    async with Client(server) as client:
        answer = tool_json(await client.call_tool("ticker_order_updates", {}))
        status = tool_json(await client.call_tool("ticker_status", {}))
    assert [(i["source"], i["data"]["status"]) for i in answer["items"]] == [
        ("ticker", "OPEN"),
        ("postback", "COMPLETE"),
    ]
    assert status["postback_path"] == "/postback" and status["order_updates_buffered"] == 2


@pytest.mark.parametrize(
    ("label", "body", "status"),
    [
        ("wrong checksum", postback(checksum="0" * 64), 403),
        ("missing checksum", {k: v for k, v in postback().items() if k != "checksum"}, 403),
        ("checksum of another order", postback(checksum=checksum_of("1", STAMP)), 403),
        ("number as checksum", postback(checksum=123), 403),
        ("lone surrogate in the checksum", b'{"order_id": "1", "order_timestamp": "t", "checksum": "\\ud800"}', 403),
        ("not an object", [1, 2, 3], 400),
        ("a bare string", "order", 400),
        ("not json", b"order_id=1&status=COMPLETE", 400),
        ("empty body", b"", 400),
        ("deeply nested", b"[" * 60000, 400),
    ],
)
async def test_rejections_are_bare_and_leave_the_buffer_alone(make_app, http, label, body, status):
    rt, _, app = make_app()
    response = await send(http(app), body)
    assert response.status_code == status, label
    assert response.content == b""  # nothing for a prober to learn from
    assert buffered(rt) == []


async def test_without_an_api_secret_nothing_can_be_verified_so_everything_is_refused(make_app, http):
    rt, _, app = make_app(KITE_API_SECRET=None)
    forged = postback(checksum=checksum_of(ORDER_ID, STAMP, ""))  # what an attacker would compute with no secret
    response = await send(http(app), forged)
    assert response.status_code == 403 and response.content == b""
    assert buffered(rt) == []


async def test_a_postback_needs_no_login(make_app, http, tmp_path):
    """Kite documents that postbacks arrive even when the user is not logged in."""
    rt, _, app = make_app(KITE_TOKEN_FILE=str(tmp_path / "no-such-token.json"))
    assert rt.tokens.current() is None
    assert (await send(http(app), postback())).status_code == 200


async def test_without_a_ticker_manager_it_is_a_503(make_app, http):
    rt, _, app = make_app()
    rt.ticker = None
    response = await send(http(app), postback())
    assert response.status_code == 503 and response.content == b""


# --- the body cap ----------------------------------------------------------------------------------------------------


def test_the_cap_is_64_kib():
    assert MAX_BODY_BYTES == 64 * 1024


async def test_a_body_over_the_cap_is_refused_by_its_declared_length(make_app, http):
    rt, _, app = make_app()
    response = await send(http(app), b"x" * 70_000)  # over 64 KiB (65,536)
    assert response.status_code == 413 and response.content == b""
    assert buffered(rt) == []


async def test_a_body_without_a_length_is_cut_off_while_it_is_read(make_app, http):
    rt, _, app = make_app()
    sent = 0

    async def endless():
        nonlocal sent
        for _ in range(100_000):
            sent += 1
            yield b"x" * 1024

    response = await http(app).post("/postback", content=endless())
    assert response.status_code == 413
    assert sent * 1024 < 4 * 65_536  # it stopped reading soon after the cap, not after 100 MB
    assert buffered(rt) == []


async def test_a_large_but_allowed_postback_is_accepted_and_its_free_text_clipped(make_app, http):
    rt, _, app = make_app()
    big = postback(status_message="x" * 60_000)
    body = json.dumps(big).encode()
    assert 60_000 < len(body) < 65_536  # big, but under 64 KiB
    assert (await send(http(app), body)).status_code == 200
    [entry] = buffered(rt)
    assert entry["data"]["status_message"] == "x" * MAX_TEXT_CHARS


# --- who can call it, and where --------------------------------------------------------------------------------------


@pytest.mark.parametrize("method", ["GET", "PUT", "DELETE", "PATCH"])
async def test_only_post_is_allowed(make_app, http, method):
    _, _, app = make_app()
    assert (await http(app).request(method, "/postback")).status_code == 405


async def test_the_route_is_public_while_mcp_still_needs_the_bearer_token(make_app, http):
    rt, _, app = make_app()
    client = http(app)
    assert (await send(client, postback())).status_code == 200  # no Authorization header
    assert (await client.post("/mcp", json={})).status_code == 401
    assert (await client.get("/health")).status_code == 200
    assert (await client.post("/mcp", json={}, headers={"Authorization": "Bearer wrong"})).status_code == 401


async def test_a_custom_path_replaces_the_default(make_app, http):
    _, _, app = make_app(KITE_MCP_POSTBACK_PATH="/hooks/kite-orders")
    client = http(app)
    assert (await send(client, postback(), "/hooks/kite-orders")).status_code == 200
    assert (await send(client, postback())).status_code == 404


@pytest.mark.parametrize(
    "overrides",
    [
        {"KITE_MCP_POSTBACK_PATH": None},  # not configured
        {"KITE_MCP_TICKER_ENABLED": "false"},  # the order updates could not be read
        {"KITE_MCP_TOOLSETS": "account"},
        {"KITE_MCP_EXCLUDED_TOOLS": "ticker_order_updates"},
    ],
)
async def test_the_route_exists_only_when_the_updates_can_be_read(make_app, http, overrides):
    rt, _, app = make_app(**overrides)
    assert (await send(http(app), postback())).status_code == 404
    assert not [h for h in rt.extras.get("http_routes", []) if isinstance(h, PostbackRoute)]


async def test_building_the_server_twice_registers_the_hook_once(runtime_factory):
    rt = runtime_factory(**HTTP)
    build_server(rt)
    build_server(rt)
    assert len([h for h in rt.extras["http_routes"] if isinstance(h, PostbackRoute)]) == 1


# --- logs ------------------------------------------------------------------------------------------------------------


async def test_the_payload_never_reaches_the_logs(make_app, http, caplog):
    caplog.set_level(logging.DEBUG)
    _, _, app = make_app()
    client = http(app)
    marker = "MARKER-STATUS-TEXT"
    good = postback(status_message=marker)
    await send(client, good)
    await send(client, postback(status_message=marker, checksum="f" * 64))  # rejected
    await send(client, b'{"user_id": "AB1234", "oops": ' + marker.encode())  # not JSON
    ours = "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("kite_connect_mcp"))
    assert "postback accepted" in ours and "postback refused" in ours  # it does log what happened...
    for sensitive in (marker, good["checksum"], "f" * 64, ORDER_ID, "AB1234", "XXXXXX", API_SECRET, "SBIN"):
        assert sensitive not in ours  # ...but nothing from the payload
