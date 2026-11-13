"""Postbacks with the event service on: a doorbell, never stored as they came; refusals counted; selftests recorded."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx2
import pytest
from mcp import Client

from conftest import API_SECRET, BEARER, tool_json
from events_helpers import ORDER_ID, filled_row, order_row
from kite_connect_mcp.postback import PostbackRoute, compute_checksum
from kite_connect_mcp.server import build_http_app, build_server

EVENTS = {
    "KITE_MCP_EVENTS": "true",
    "KITE_MCP_TRANSPORT": "http",
    "KITE_MCP_BEARER_TOKEN": BEARER,
    "KITE_MCP_POSTBACK_PATH": "/postback",
}
STAMP = "2026-11-04 09:30:00"


def signed(order_id: str = ORDER_ID, **fields: Any) -> dict[str, Any]:
    body = {"order_id": order_id, "order_timestamp": STAMP, "user_id": "AB1234", "status": "COMPLETE", **fields}
    body.setdefault("checksum", compute_checksum(order_id, STAMP, API_SECRET))
    return body


@pytest.fixture
def make_app(runtime_factory: Callable[..., Any]):
    made = []

    def make(**overrides: str | None):
        rt = runtime_factory(**{**EVENTS, **overrides})
        server, _ = build_server(rt)
        made.append(rt)
        return rt, build_http_app(server, rt)

    yield make
    for rt in made:
        if rt.events is not None:
            rt.events.store.close()


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


async def post(client: httpx2.AsyncClient, payload: Any) -> httpx2.Response:
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    return await client.post("/postback", content=body, headers={"Content-Type": "application/json"})


async def test_a_valid_postback_rings_and_kites_answer_is_what_is_stored(make_app, http, fake_kite):
    rt, app = make_app()
    response = await post(http(app), signed(status="COMPLETE", filled_quantity=25))
    assert response.status_code == 200
    assert rt.events.store.doorbells() == [(ORDER_ID, 0)]
    assert rt.events.store.events() == []  # nothing is stored from the payload itself
    fake_kite.on("GET", f"/orders/{ORDER_ID}", [order_row()])  # Kite says: still OPEN
    await rt.events.doorbells.process()
    (event,) = rt.events.store.events()
    assert event["order"]["status"] == "OPEN" and event["source"] == "postback"
    assert rt.events.store.counters()["postback_accepted"]["count"] == 1
    assert fake_kite.writes() == []


@pytest.mark.parametrize(
    ("payload", "status", "reason"),
    [
        (signed(checksum="0" * 64), 403, "bad checksum"),
        (b"not json", 400, "body not JSON"),
        (b"[1, 2]", 400, "body not a JSON object"),
        (signed(order_id="bad id!", checksum=compute_checksum("bad id!", STAMP, API_SECRET)), 400, "bad order id"),
        (signed(order_id="selftest-", checksum=compute_checksum("selftest-", STAMP, API_SECRET)), 400, "bad order id"),
        (signed(user_id="ZZ9999"), 403, "another account"),
    ],
)
async def test_refusals_are_bare_and_counted(make_app, http, payload, status, reason):
    rt, app = make_app(KITE_MCP_ALLOWED_USER_ID="AB1234")
    response = await post(http(app), payload)
    assert response.status_code == status and response.content == b""
    counter = rt.events.store.counters()["postback_refused"]
    assert counter["count"] == 1 and counter["note"] == reason
    assert rt.events.store.doorbells() == []


async def test_an_oversized_body_is_refused_and_counted(make_app, http):
    rt, app = make_app()
    response = await http(app).post("/postback", content=b"x" * (64 * 1024 + 1))
    assert response.status_code == 413
    assert rt.events.store.counters()["postback_refused"]["note"] == "body over the cap"


async def test_without_an_api_secret_everything_is_refused(make_app, http):
    rt, app = make_app(KITE_API_SECRET=None)
    assert (await post(http(app), signed())).status_code == 403


async def test_a_signed_selftest_is_recorded_without_asking_kite(make_app, http, fake_kite):
    rt, app = make_app()
    order_id = "selftest-0a1b2c3d4e5f"
    response = await post(http(app), signed(order_id=order_id, checksum=compute_checksum(order_id, STAMP, API_SECRET)))
    assert response.status_code == 200
    (event,) = rt.events.store.events()
    assert event["kind"] == "selftest" and event["order_id"] == order_id
    assert rt.events.store.doorbells() == [] and fake_kite.requests == []


async def test_a_store_that_cannot_write_answers_503(make_app, http):
    rt, app = make_app()
    rt.events.store.close()
    response = await post(http(app), signed())
    assert response.status_code == 503 and response.content == b""


@pytest.mark.parametrize("overrides", [{"KITE_MCP_TICKER_ENABLED": "false"}, {"KITE_MCP_TOOLSETS": "account"}, {}])
async def test_the_route_does_not_depend_on_the_ticker(make_app, http, overrides):
    rt, app = make_app(**overrides)
    assert (await post(http(app), signed())).status_code == 200
    assert len([h for h in rt.extras["http_routes"] if isinstance(h, PostbackRoute)]) == 1


async def test_with_the_ticker_off_its_status_does_not_call_the_route_off(runtime_factory):
    rt = runtime_factory(**EVENTS, KITE_MCP_TICKER_ENABLED="false")
    server, _ = build_server(rt)
    async with Client(server) as client:
        status = tool_json(await client.call_tool("ticker_status", {}))
    assert "order events" in status["postback"] and "off" not in status["postback"]


async def test_the_ticker_buffer_still_gets_the_update_when_its_updates_are_on(make_app, http):
    rt, app = make_app()
    await post(http(app), signed())
    (item,) = rt.ticker.events.recent(10)[0]
    assert item["source"] == "postback"
    rt2, app2 = make_app(KITE_MCP_EXCLUDED_TOOLS="ticker_order_updates")
    await post(http(app2), signed())
    assert rt2.ticker.events.recent(10)[0] == []


async def test_building_the_server_twice_registers_one_route(runtime_factory):
    rt = runtime_factory(**EVENTS)
    build_server(rt)
    build_server(rt)
    assert len([h for h in rt.extras["http_routes"] if isinstance(h, PostbackRoute)]) == 1
    rt.events.store.close()


async def test_nothing_from_the_payload_reaches_the_logs(make_app, http, caplog):
    caplog.set_level(logging.DEBUG)
    rt, app = make_app(KITE_MCP_ALLOWED_USER_ID="AB1234")
    await post(http(app), signed())
    await post(http(app), signed(checksum="f" * 64))
    await post(http(app), signed(user_id="ZZ9999"))
    text = caplog.text
    for secret in (ORDER_ID, "AB1234", "ZZ9999", API_SECRET, compute_checksum(ORDER_ID, STAMP, API_SECRET)):
        assert secret not in text
    assert filled_row  # imported for symmetry with the other event tests
