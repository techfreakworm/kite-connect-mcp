"""Alert tools: simple and ATO alerts, the basket format, caps on basket orders, modify and delete."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qs

import pytest
from mcp import Client

from conftest import tool_json, tool_text
from kite_connect_mcp.instruments import Instrument

LIVE = {"KITE_MCP_TRADING_MODE": "live"}
UUID = "aaaaaaaa-1111-4111-8111-bbbbbbbbbbbb"
OTHER = "cccccccc-2222-4222-8222-dddddddddddd"

SIMPLE: dict[str, Any] = {
    "name": "NIFTY 50",
    "type": "simple",
    "lhs_exchange": "INDICES",
    "lhs_tradingsymbol": "NIFTY 50",
    "operator": ">=",
    "rhs_type": "constant",
    "rhs_constant": 27000,
}
SIMPLE_FORM = {
    "name": "NIFTY 50",
    "type": "simple",
    "lhs_exchange": "INDICES",
    "lhs_tradingsymbol": "NIFTY 50",
    "lhs_attribute": "LastTradedPrice",
    "operator": ">=",
    "rhs_type": "constant",
    "rhs_constant": "27000",
}
BUY_LIMIT: dict[str, Any] = {
    "exchange": "NSE",
    "tradingsymbol": "RELIANCE",
    "transaction_type": "BUY",
    "quantity": 2,
    "product": "CNC",
    "order_type": "LIMIT",
    "price": 1190.0,
}  # worth 2380.00
BUY_MARKET: dict[str, Any] = {
    "exchange": "NSE",
    "tradingsymbol": "RELIANCE",
    "transaction_type": "BUY",
    "quantity": 1,
    "product": "CNC",
    "order_type": "MARKET",
}
ATO: dict[str, Any] = {**SIMPLE, "type": "ato", "orders": [BUY_LIMIT]}
# the params of one basket item, key for key as in the docs' sample
SAMPLE_PARAMS = {
    "transaction_type",
    "product",
    "order_type",
    "validity",
    "validity_ttl",
    "quantity",
    "price",
    "trigger_price",
    "disclosed_quantity",
    "last_price",
    "variety",
    "tags",
    "squareoff",
    "stoploss",
    "trailing_stoploss",
    "iceberg_legs",
    "market_protection",
}


def kite(fake):
    fake.on("POST", "/alerts", {"type": "simple", "uuid": UUID, "status": "enabled"})
    fake.on("PUT", f"/alerts/{UUID}", {"type": "simple", "uuid": UUID, "status": "enabled"})
    fake.on("DELETE", "/alerts", None)
    fake.on("POST", "/margins/orders", [{"type": "equity", "total": 2380.0}])
    fake.on("POST", "/margins/basket", {"initial": {"total": 3640.0}})
    fake.on("GET", "/quote/ltp", {"NSE:RELIANCE": {"instrument_token": 738561, "last_price": 1200.0}})
    return fake


async def call(server, name, args):
    async with Client(server) as client:
        return await client.call_tool(name, args)


async def go_live(server, name, args):
    preview = tool_json(await call(server, name, args))
    return tool_json(
        await call(server, name, {**args, "dry_run": False, "confirmation_token": preview["confirmation_token"]})
    )


def form_of(fake, index: int = -1) -> dict[str, str]:
    pairs = fake.form(fake.writes()[index])
    assert len({k for k, _ in pairs}) == len(pairs)  # no repeated keys in an alert form
    return dict(pairs)


def alert_row(uuid: str, name: str, status: str, created: str, kind: str = "simple") -> dict[str, Any]:
    return {"uuid": uuid, "name": name, "status": status, "created_at": created, "type": kind, "alert_count": 0}


# --- reads ----------------------------------------------------------------------------------------------------------


async def test_get_alerts_sends_the_status_filter_and_paginates_newest_first(mcp_factory, fake_kite):
    rows = [
        alert_row("00000000-0000-4000-8000-000000000001", "old", "disabled", "2025-02-17 08:24:10"),
        alert_row("00000000-0000-4000-8000-000000000002", "newest", "enabled", "2025-05-26 12:09:26"),
        alert_row("00000000-0000-4000-8000-000000000003", "middle", "enabled", "2025-05-26 12:07:50", "ato"),
    ]
    fake_kite.on("GET", "/alerts", rows)
    server, _, _ = mcp_factory()
    page = tool_json(await call(server, "get_alerts", {"limit": 2}))
    assert [a["name"] for a in page["items"]] == ["newest", "middle"]
    assert page["total"] == 3 and page["next_offset"] == 2
    rest = tool_json(await call(server, "get_alerts", {"limit": 2, "offset": 2}))
    assert [a["name"] for a in rest["items"]] == ["old"] and "next_offset" not in rest
    assert [r.url.query for r in fake_kite.calls("GET", "/alerts")] == [b"", b""]  # no filter, none sent

    tool_json(await call(server, "get_alerts", {"status": "enabled"}))
    assert fake_kite.calls("GET", "/alerts")[-1].url.query == b"status=enabled"  # the API filters by status
    assert (await call(server, "get_alerts", {"status": "archived"})).is_error
    assert len(fake_kite.calls("GET", "/alerts")) == 3


async def test_get_alert_and_its_history(mcp_factory, fake_kite):
    fake_kite.on("GET", f"/alerts/{UUID}", alert_row(UUID, "NIFTY 50", "enabled", "2025-05-26 12:07:50"))
    history = [
        {"uuid": UUID, "type": "simple", "created_at": "2025-02-17 09:16:46", "condition": "old"},
        {"uuid": UUID, "type": "simple", "created_at": "2025-03-17 09:16:46", "condition": "new"},
        {"uuid": UUID, "type": "simple", "created_at": "2025-03-01 09:16:46", "condition": "mid"},
    ]
    fake_kite.on("GET", f"/alerts/{UUID}/history", history)
    server, _, _ = mcp_factory()
    assert tool_json(await call(server, "get_alert", {"uuid": UUID.upper()}))["name"] == "NIFTY 50"
    page = tool_json(await call(server, "get_alert_history", {"uuid": UUID, "limit": 2}))
    assert [h["condition"] for h in page["items"]] == ["new", "mid"] and page["next_offset"] == 2
    assert fake_kite.requests[0].url.path == f"/alerts/{UUID}"  # the uuid is normalised to lower case


@pytest.mark.parametrize(
    "bad", ["", "../orders", "aaaaaaaa", "aaaaaaaa-1111-4111-8111-bbbbbbbbbbbg", UUID + "/history"]
)
async def test_alert_uuids_are_checked_before_any_request(mcp_factory, fake_kite, bad):
    server, _, _ = mcp_factory()
    for name, args in (
        ("get_alert", {"uuid": bad}),
        ("get_alert_history", {"uuid": bad}),
        ("delete_alerts", {"uuids": [UUID, bad]}),
        ("modify_alert", {"uuid": bad, **SIMPLE}),
    ):
        assert (await call(server, name, args)).is_error, name
    assert fake_kite.requests == []


async def test_read_errors_are_mapped(mcp_factory, fake_kite):
    fake_kite.on("GET", f"/alerts/{UUID}", status=400, error_type="InputException", message="alert not found")
    fake_kite.on("GET", "/alerts", status=403, error_type="TokenException", message="Incorrect api_key")
    server, _, rt = mcp_factory()
    missing = await call(server, "get_alert", {"uuid": UUID})
    assert missing.is_error and "alert not found" in tool_text(missing) and "InputException" in tool_text(missing)
    assert rt.tokens.current() is not None  # a bad request is not a dead session
    result = await call(server, "get_alerts", {})
    assert result.is_error and "rejected the session" in tool_text(result) and "login" in tool_text(result)
    assert rt.tokens.current() is None  # a TokenException is: the client dropped the rejected token


# --- simple alerts --------------------------------------------------------------------------------------------------


async def test_create_simple_alert_dry_run_shows_the_exact_request(mcp_factory, fake_kite):
    kite(fake_kite)
    server, _, rt = mcp_factory()
    answer = tool_json(await call(server, "create_alert", {**SIMPLE, "lhs_tradingsymbol": " nifty 50 "}))
    assert answer["dry_run"] is True and answer["sent"] is False
    assert answer["would_send"] == {"method": "POST", "path": "/alerts", "form": SIMPLE_FORM}
    assert answer["checks"]["ok"] is True and answer["legs"] == []
    assert "margins" not in answer and "confirmation_token" not in answer
    assert fake_kite.requests == [] and [e["event"] for e in rt.audit.read()] == ["dry_run"]
    assert "simple alert 'NIFTY 50': INDICES:NIFTY 50 LastTradedPrice >= 27000" in answer["summary"]


async def test_create_simple_alert_live_sends_the_documented_form_once(mcp_factory, fake_kite):
    kite(fake_kite)
    server, _, rt = mcp_factory(**LIVE)
    preview = tool_json(await call(server, "create_alert", SIMPLE))
    refused = await call(server, "create_alert", {**SIMPLE, "dry_run": False})
    assert refused.is_error and "confirmation_token is required" in tool_text(refused)
    assert fake_kite.writes() == []

    args = {**SIMPLE, "dry_run": False, "confirmation_token": preview["confirmation_token"]}
    sent = tool_json(await call(server, "create_alert", args))
    assert sent["sent"] is True and sent["response"]["uuid"] == UUID
    [request] = fake_kite.writes()
    assert request.method == "POST" and request.url.path == "/alerts"
    assert request.headers["content-type"] == "application/x-www-form-urlencoded"
    assert form_of(fake_kite) == SIMPLE_FORM
    assert b"operator=%3E%3D" in request.content  # ">=" survives the form encoding
    replay = await call(server, "create_alert", args)
    assert replay.is_error and "already used" in tool_text(replay)
    assert len(fake_kite.writes()) == 1
    usage = rt.audit.usage()
    assert usage.orders == 0 and usage.value == 0  # a simple alert places no order
    assert [e["event"] for e in rt.audit.read()] == [
        "confirmation_issued",
        "refused",
        "live_intent",
        "live_result",
        "refused",
    ]


async def test_alert_against_another_instrument(mcp_factory, fake_kite):
    kite(fake_kite)
    server, _, _ = mcp_factory(**LIVE)
    args = {
        "name": "gold below silver",
        "type": "simple",
        "lhs_exchange": "NSE",
        "lhs_tradingsymbol": "goldbees",
        "operator": "<",
        "rhs_type": "instrument",
        "rhs_exchange": "NSE",
        "rhs_tradingsymbol": "silverbees",
    }
    answer = await go_live(server, "create_alert", args)
    assert answer["sent"] is True
    assert form_of(fake_kite) == {
        "name": "gold below silver",
        "type": "simple",
        "lhs_exchange": "NSE",
        "lhs_tradingsymbol": "GOLDBEES",
        "lhs_attribute": "LastTradedPrice",
        "operator": "<",
        "rhs_type": "instrument",
        "rhs_exchange": "NSE",
        "rhs_tradingsymbol": "SILVERBEES",
        "rhs_attribute": "LastTradedPrice",  # the only attribute Kite documents is the default
    }


@pytest.mark.parametrize("operator", ["<=", ">=", "<", ">", "=="])
async def test_every_documented_operator_is_sent_as_is(mcp_factory, fake_kite, operator):
    kite(fake_kite)
    server, _, _ = mcp_factory(**LIVE)
    await go_live(server, "create_alert", {**SIMPLE, "operator": operator})
    assert form_of(fake_kite)["operator"] == operator


async def test_a_fractional_constant_keeps_its_digits(mcp_factory, fake_kite):
    kite(fake_kite)
    server, _, _ = mcp_factory(**LIVE)
    await go_live(server, "create_alert", {**SIMPLE, "rhs_constant": 71.8})
    assert form_of(fake_kite)["rhs_constant"] == "71.8"


async def test_simple_alert_has_no_caps_but_is_still_a_confirmed_write(mcp_factory, fake_kite):
    kite(fake_kite)
    server, _, _ = mcp_factory(
        **LIVE,
        KITE_MCP_MAX_ORDER_VALUE="1",
        KITE_MCP_MAX_DAILY_VALUE="1",
        KITE_MCP_MAX_ORDERS_PER_DAY="0",
        KITE_MCP_ALLOWED_PRODUCTS="MIS",
        KITE_MCP_LIVE_CONFIRMATION="allowlist",
        KITE_MCP_ALLOWED_INSTRUMENTS="NSE:INFY",
    )
    preview = tool_json(await call(server, "create_alert", SIMPLE))
    assert preview["checks"]["ok"] is True and "confirmation_token" in preview  # even in allowlist mode
    refused = await call(server, "create_alert", {**SIMPLE, "dry_run": False})
    assert refused.is_error and "confirmation_token is required" in tool_text(refused)
    assert fake_kite.writes() == []


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"rhs_constant": None}, "rhs_type=constant needs rhs_constant"),
        ({"name": "   "}, "must not be blank"),
        ({"lhs_tradingsymbol": "  "}, "must not be blank"),
        ({"rhs_exchange": "NSE"}, "only for rhs_type=instrument"),
        ({"rhs_attribute": "LastTradedPrice"}, "only for rhs_type=instrument"),
        ({"rhs_type": "instrument"}, "rhs_type=instrument needs rhs_exchange and rhs_tradingsymbol"),
        (
            {"rhs_type": "instrument", "rhs_exchange": "NSE", "rhs_tradingsymbol": "INFY"},
            "rhs_constant is only for rhs_type=constant",
        ),
        ({"type": "ato"}, "type=ato needs at least one order"),
        ({"orders": [BUY_LIMIT]}, "only for type=ato"),
    ],
)
async def test_inconsistent_alerts_are_rejected_before_anything(mcp_factory, fake_kite, changes, message):
    kite(fake_kite)
    server, _, rt = mcp_factory()
    args = {k: v for k, v in {**SIMPLE, **changes}.items() if v is not None}
    for name, extra in (("create_alert", {}), ("modify_alert", {"uuid": UUID})):
        result = await call(server, name, {**args, **extra})
        assert result.is_error and message in tool_text(result), name
    assert fake_kite.requests == [] and rt.audit.read() == []


@pytest.mark.parametrize(
    "changes",
    [
        {"operator": "="},
        {"operator": "!="},
        {"lhs_exchange": "BFO"},
        {"lhs_exchange": "nse"},
        {"lhs_attribute": "Volume"},
        {"rhs_type": "variable"},
        {"type": "oco"},
        {"name": ""},
        {"name": "x" * 101},
        {"rhs_constant": "high"},
        {"lhs_tradingsymbol": ""},
        {"type": "ato", "orders": [{**BUY_LIMIT, "stoploss": 1}]},
        {"type": "ato", "orders": [{**BUY_LIMIT, "quantity": 0}]},
        {"type": "ato", "orders": [{**BUY_LIMIT, "price": -1}]},
        {"type": "ato", "orders": [{**BUY_LIMIT, "product": "BO"}]},
        {"type": "ato", "orders": [BUY_LIMIT] * 21},
    ],
)
async def test_the_schema_refuses_malformed_alerts(mcp_factory, fake_kite, changes):
    kite(fake_kite)
    server, _, rt = mcp_factory()
    result = await call(server, "create_alert", {**SIMPLE, **changes})
    assert result.is_error
    assert fake_kite.requests == [] and rt.audit.read() == []


# --- ATO alerts -----------------------------------------------------------------------------------------------------


async def test_ato_alert_sends_the_basket_in_the_documented_shape(mcp_factory, fake_kite):
    kite(fake_kite)
    server, _, _ = mcp_factory(**LIVE)
    preview = tool_json(await call(server, "create_alert", ATO))
    would_send = preview["would_send"]["form"]
    assert would_send["type"] == "ato" and isinstance(would_send["basket"], str)  # a JSON string, as the docs say
    assert preview["margins"][0]["total"] == 2380.0  # one basket order: Kite's order margin calculator
    sent = tool_json(
        await call(
            server, "create_alert", {**ATO, "dry_run": False, "confirmation_token": preview["confirmation_token"]}
        )
    )
    assert sent["sent"] is True
    form = form_of(fake_kite)
    assert {k: v for k, v in form.items() if k != "basket"} == {**SIMPLE_FORM, "type": "ato"}
    assert form["basket"] == would_send["basket"]
    assert json.loads(form["basket"]) == {
        "name": "alerts-basket",
        "type": "alert",
        "tags": [],
        "items": [
            {
                "type": "insert",
                "tradingsymbol": "RELIANCE",
                "exchange": "NSE",
                "weight": 10000,
                "params": {
                    "transaction_type": "BUY",
                    "product": "CNC",
                    "order_type": "LIMIT",
                    "validity": "DAY",
                    "validity_ttl": 0,
                    "quantity": 2,
                    "price": 1190.0,
                    "trigger_price": 0,
                    "disclosed_quantity": 0,
                    "last_price": 0,
                    "variety": "regular",
                    "tags": [],
                    "squareoff": 0,
                    "stoploss": 0,
                    "trailing_stoploss": 0,
                    "iceberg_legs": 0,
                    "market_protection": 0,
                },
            }
        ],
    }
    assert set(json.loads(form["basket"])["items"][0]["params"]) == SAMPLE_PARAMS


async def test_ato_basket_items_carry_each_orders_own_parameters(mcp_factory, fake_kite):
    kite(fake_kite)
    server, _, _ = mcp_factory()
    sl = {**BUY_LIMIT, "tradingsymbol": "infy", "order_type": "SL", "trigger_price": 1195.0}
    ttl = {**BUY_LIMIT, "validity": "TTL", "validity_ttl": 15, "disclosed_quantity": 1}
    slm = {**BUY_MARKET, "order_type": "SL-M", "trigger_price": 1180.0, "market_protection": 3}
    args = {**ATO, "orders": [BUY_MARKET, sl, ttl, slm]}
    answer = tool_json(await call(server, "create_alert", args))
    items = json.loads(answer["would_send"]["form"]["basket"])["items"]
    assert [i["weight"] for i in items] == [10000, 20000, 30000, 40000]  # distinct and increasing
    assert [i["tradingsymbol"] for i in items] == ["RELIANCE", "INFY", "RELIANCE", "RELIANCE"]
    market, stop_limit, ttl_item, stop_market = (i["params"] for i in items)
    assert market["market_protection"] == -1 and market["price"] == 0  # the docs' sample: automatic protection
    assert stop_limit["order_type"] == "SL" and stop_limit["trigger_price"] == 1195.0 and stop_limit["price"] == 1190.0
    assert ttl_item["validity"] == "TTL" and ttl_item["validity_ttl"] == 15 and ttl_item["disclosed_quantity"] == 1
    assert stop_market["market_protection"] == 3 and stop_market["trigger_price"] == 1180.0
    assert all(set(i["params"]) == SAMPLE_PARAMS and i["params"]["variety"] == "regular" for i in items)
    assert "basket" in answer["would_send"]["form"] and "BUY 1 NSE:RELIANCE MARKET" in answer["summary"]


async def test_ato_alert_adds_up_its_orders_in_the_daily_caps(mcp_factory, fake_kite):
    kite(fake_kite)
    second = {**BUY_LIMIT, "quantity": 1, "price": 1200.0}  # 2380.00 + 1200.00 = 3580.00
    args = {**ATO, "orders": [BUY_LIMIT, second]}
    server, _, rt = mcp_factory(**LIVE)
    preview = tool_json(await call(server, "create_alert", args))
    assert [leg["value_inr"] for leg in preview["legs"]] == ["2380.00", "1200.00"]
    assert preview["margins"] == {"initial": {"total": 3640.0}}  # several orders: the basket margin calculator
    sent = tool_json(
        await call(
            server, "create_alert", {**args, "dry_run": False, "confirmation_token": preview["confirmation_token"]}
        )
    )
    assert sent["sent"] is True
    usage = rt.audit.usage()
    assert usage.orders == 2 and usage.value == Decimal("3580")  # every basket order counts, unlike an OCO GTT

    capped, _, _ = mcp_factory(**LIVE, KITE_MCP_MAX_ORDERS_PER_DAY="1")
    answer = tool_json(await call(capped, "create_alert", args))
    assert any("daily limit of 1" in v for v in answer["checks"]["violations"])
    daily, _, _ = mcp_factory(**LIVE, KITE_MCP_MAX_DAILY_VALUE="3000")
    answer = tool_json(await call(daily, "create_alert", args))
    assert any("daily cap" in v and "₹3,580.00" in v for v in answer["checks"]["violations"])


async def test_ato_orders_meet_the_per_order_cap_and_the_allow_lists(mcp_factory, fake_kite):
    kite(fake_kite)
    server, _, _ = mcp_factory(**LIVE, KITE_MCP_MAX_ORDER_VALUE="2000")
    answer = tool_json(await call(server, "create_alert", ATO))
    assert any("₹2,380.00" in v and "per-order cap" in v for v in answer["checks"]["violations"])
    assert "confirmation_token" not in answer
    refused = await call(server, "create_alert", {**ATO, "dry_run": False})
    assert refused.is_error and "per-order cap" in tool_text(refused)

    restricted, _, _ = mcp_factory(
        **LIVE,
        KITE_MCP_ALLOWED_EXCHANGES="BSE",
        KITE_MCP_ALLOWED_PRODUCTS="MIS",
        KITE_MCP_ALLOWED_ORDER_TYPES="MARKET",
        KITE_MCP_ALLOWED_TRANSACTION_TYPES="SELL",
        KITE_MCP_ALLOWED_VARIETIES="amo",
        KITE_MCP_MAX_QUANTITY="1",
    )
    violations = " ".join(tool_json(await call(restricted, "create_alert", ATO))["checks"]["violations"])
    for expected in ("exchange NSE", "product CNC", "order_type LIMIT", "transaction_type BUY", "variety regular"):
        assert expected in violations
    assert "KITE_MCP_MAX_QUANTITY" in violations
    assert fake_kite.writes() == []


async def test_ato_allowlist_mode_skips_the_token_only_for_listed_instruments(mcp_factory, fake_kite):
    kite(fake_kite)
    server, _, _ = mcp_factory(
        **LIVE, KITE_MCP_LIVE_CONFIRMATION="allowlist", KITE_MCP_ALLOWED_INSTRUMENTS="NSE:RELIANCE"
    )
    assert tool_json(await call(server, "create_alert", {**ATO, "dry_run": False}))["sent"] is True
    other = {**ATO, "orders": [{**BUY_LIMIT, "tradingsymbol": "TCS"}], "dry_run": False}
    result = await call(server, "create_alert", other)
    assert result.is_error and "ALLOWED_INSTRUMENTS" in tool_text(result)
    assert len(fake_kite.writes()) == 1


async def test_ato_market_order_is_valued_at_the_live_price_or_cannot_go_live(mcp_factory, fake_kite):
    kite(fake_kite)
    server, _, _ = mcp_factory(**LIVE)
    market = {**ATO, "orders": [{**BUY_MARKET, "market_protection": 2}]}
    leg = tool_json(await call(server, "create_alert", market))["legs"][0]
    assert leg["value_inr"] == "1224.00"  # 1 x 1200 x 1.02

    fake_kite.on("GET", "/quote/ltp", status=403, error_type="PermissionException", message="Insufficient permission")
    answer = tool_json(await call(server, "create_alert", market))
    assert answer["checks"]["ok"] is False and "Personal" in answer["legs"][0]["value_note"]
    refused = await call(server, "create_alert", {**market, "dry_run": False})
    assert refused.is_error and "cannot be verified" in tool_text(refused)
    assert fake_kite.writes() == []


async def test_ato_commodity_order_cannot_be_valued(mcp_factory, fake_kite):
    kite(fake_kite)
    server, _, _ = mcp_factory(**LIVE)
    mcx = {**ATO, "orders": [{**BUY_LIMIT, "exchange": "MCX", "tradingsymbol": "GOLDM25DECFUT", "product": "NRML"}]}
    answer = tool_json(await call(server, "create_alert", mcx))
    assert answer["checks"]["ok"] is False and "lots" in answer["legs"][0]["value_note"]


@pytest.mark.parametrize(
    ("order", "message"),
    [
        ({**BUY_MARKET, "price": 1200.0}, "MARKET orders take no price"),
        ({**BUY_LIMIT, "price": None}, "LIMIT orders need a price"),
        ({**BUY_LIMIT, "order_type": "SL"}, "SL orders need a trigger_price"),
        ({**BUY_LIMIT, "exchange": "NFO", "tradingsymbol": "NIFTY25NOVFUT"}, "CNC is for equity delivery"),
        ({**BUY_LIMIT, "validity": "TTL"}, "validity TTL needs validity_ttl"),
        ({**BUY_LIMIT, "market_protection": 5}, "market_protection only applies to MARKET"),
        ({**BUY_LIMIT, "disclosed_quantity": 5}, "disclosed_quantity cannot exceed quantity"),
    ],
)
async def test_basket_orders_follow_the_order_rules(mcp_factory, fake_kite, order, message):
    kite(fake_kite)
    server, _, rt = mcp_factory()
    order = {k: v for k, v in order.items() if v is not None}
    result = await call(server, "create_alert", {**ATO, "orders": [order]})
    assert result.is_error and message in tool_text(result)
    assert fake_kite.requests == [] and rt.audit.read() == []


class FakeStore:
    def __init__(self, tick: str):
        self.tick = Decimal(tick)

    async def lookup(self, exchange: str, tradingsymbol: str) -> Instrument | None:
        if (exchange, tradingsymbol) != ("NSE", "RELIANCE"):
            return None
        return Instrument(
            instrument_token=738561,
            exchange_token=2885,
            tradingsymbol="RELIANCE",
            name="RELIANCE",
            last_price=0.0,
            expiry=None,
            strike=0.0,
            tick_size=self.tick,
            lot_size=1,
            instrument_type="EQ",
            segment="NSE",
            exchange="NSE",
        )


async def test_basket_prices_follow_the_tick_size(mcp_factory, fake_kite):
    kite(fake_kite)
    server, _, rt = mcp_factory()
    rt.instruments = FakeStore("0.1")
    result = await call(server, "create_alert", {**ATO, "orders": [{**BUY_LIMIT, "price": 1190.05}]})
    assert result.is_error and "1190.05 is not a multiple of the tick size 0.1" in tool_text(result)
    assert fake_kite.requests == []
    good = tool_json(await call(server, "create_alert", ATO))
    assert good["checks"]["ok"] is True
    assert all("instrument master" not in w for w in good["checks"]["warnings"])
    unknown = await call(server, "create_alert", {**ATO, "orders": [{**BUY_LIMIT, "tradingsymbol": "NOPE"}]})
    assert unknown.is_error and "not in today's instrument list" in tool_text(unknown)


async def test_an_unavailable_instrument_master_is_only_a_warning(mcp_factory, fake_kite):
    kite(fake_kite)
    server, _, _ = mcp_factory()
    answer = tool_json(await call(server, "create_alert", ATO))
    assert answer["checks"]["ok"] is True and any("instrument master" in w for w in answer["checks"]["warnings"])


# --- modify ---------------------------------------------------------------------------------------------------------


async def test_modify_alert_puts_the_whole_alert(mcp_factory, fake_kite):
    kite(fake_kite)
    server, _, rt = mcp_factory(**LIVE)
    args = {"uuid": UUID.upper(), **SIMPLE, "rhs_constant": 27500}
    preview = tool_json(await call(server, "modify_alert", args))
    assert preview["would_send"]["method"] == "PUT" and preview["would_send"]["path"] == f"/alerts/{UUID}"
    assert preview["would_send"]["form"]["rhs_constant"] == "27500" and preview["legs"] == []
    sent = tool_json(
        await call(
            server, "modify_alert", {**args, "dry_run": False, "confirmation_token": preview["confirmation_token"]}
        )
    )
    assert sent["sent"] is True
    [request] = fake_kite.writes()
    assert request.method == "PUT" and request.url.path == f"/alerts/{UUID}"
    assert form_of(fake_kite) == {**SIMPLE_FORM, "rhs_constant": "27500"}
    assert rt.audit.read()[-1]["order_id"] == UUID
    assert rt.audit.usage().orders == 0


async def test_modify_ato_alert_takes_no_slot_but_its_value_counts(mcp_factory, fake_kite):
    kite(fake_kite)
    # no slot of the daily order count is left, so a new ATO alert is refused, yet an existing one can be modified
    server, _, rt = mcp_factory(**LIVE, KITE_MCP_MAX_ORDERS_PER_DAY="0")
    created = tool_json(await call(server, "create_alert", ATO))
    assert any("daily limit of 0" in v for v in created["checks"]["violations"])

    args = {"uuid": UUID, **ATO}
    sent = await go_live(server, "modify_alert", args)
    assert sent["sent"] is True
    form = form_of(fake_kite)
    assert form["type"] == "ato" and json.loads(form["basket"])["items"][0]["params"]["quantity"] == 2
    usage = rt.audit.usage()
    assert usage.orders == 0 and usage.value == Decimal("2380")

    capped, _, _ = mcp_factory(**LIVE, KITE_MCP_MAX_ORDER_VALUE="2000")
    answer = tool_json(await call(capped, "modify_alert", args))
    assert any("per-order cap" in v for v in answer["checks"]["violations"])


async def test_modify_shows_the_stored_alert_and_warns_when_it_already_has_a_basket(mcp_factory, fake_kite):
    kite(fake_kite)
    stored_ato = {
        **alert_row(UUID, "buy gold", "disabled", "2025-02-17 08:24:10", "ato"),
        "basket": {"items": [{"id": 275218517, "tradingsymbol": "GOLDBEES", "exchange": "NSE", "weight": 10000}]},
    }
    fake_kite.on("GET", f"/alerts/{UUID}", stored_ato)
    server, _, _ = mcp_factory(**LIVE)
    answer = tool_json(await call(server, "modify_alert", {"uuid": UUID, **ATO}))
    assert answer["current_alert"]["name"] == "buy gold"
    warning = next(w for w in answer["checks"]["warnings"] if "basket" in w)
    assert "already has 1 basket order(s)" in warning and "replaces them or is added to them" in warning
    assert answer["checks"]["ok"] is True  # advice only: the semantics are unknown, not known to be wrong

    fake_kite.on("GET", f"/alerts/{UUID}", alert_row(UUID, "NIFTY 50", "enabled", "2025-05-26 12:07:50"))
    simple = tool_json(await call(server, "modify_alert", {"uuid": UUID, **SIMPLE}))
    assert simple["checks"]["warnings"] == [] and simple["current_alert"]["type"] == "simple"

    fake_kite.routes.pop(("GET", f"/alerts/{UUID}"))  # Kite does not answer for this alert
    unreadable = tool_json(await call(server, "modify_alert", {"uuid": UUID, **SIMPLE}))
    assert any("could not be read" in w for w in unreadable["checks"]["warnings"])
    assert "current_alert" not in unreadable


async def test_the_live_modify_does_not_read_the_stored_alert_again(mcp_factory, fake_kite):
    kite(fake_kite).on("GET", f"/alerts/{UUID}", alert_row(UUID, "NIFTY 50", "enabled", "2025-05-26 12:07:50"))
    server, _, _ = mcp_factory(**LIVE)
    await go_live(server, "modify_alert", {"uuid": UUID, **SIMPLE})
    assert len(fake_kite.calls("GET", f"/alerts/{UUID}")) == 1  # only the dry run looks
    assert len(fake_kite.writes()) == 1


# --- delete ---------------------------------------------------------------------------------------------------------


async def test_delete_alerts_repeats_the_uuid_parameter_and_needs_confirmation(mcp_factory, fake_kite):
    kite(fake_kite).on(
        "GET",
        "/alerts",
        [
            alert_row(UUID, "NIFTY 50", "enabled", "2025-05-26 12:07:50"),
            alert_row(OTHER, "buy gold", "disabled", "2025-02-17", "ato"),
        ],
    )
    server, _, rt = mcp_factory(**LIVE)
    args = {"uuids": [UUID.upper(), OTHER, UUID]}  # a repeat and a different case collapse into one uuid
    preview = tool_json(await call(server, "delete_alerts", args))
    assert preview["would_send"] == {"method": "DELETE", "path": "/alerts", "query": [["uuid", UUID], ["uuid", OTHER]]}
    assert preview["summary"] == "delete 2 alert(s): 'NIFTY 50', 'buy gold'"
    assert [a["name"] for a in preview["alerts"]] == ["NIFTY 50", "buy gold"]
    assert preview["checks"]["ok"] is True and preview["legs"] == []
    assert fake_kite.writes() == []

    refused = await call(server, "delete_alerts", {**args, "dry_run": False})
    assert refused.is_error and "confirmation_token is required" in tool_text(refused)
    assert fake_kite.writes() == []

    sent = tool_json(
        await call(
            server, "delete_alerts", {**args, "dry_run": False, "confirmation_token": preview["confirmation_token"]}
        )
    )
    assert sent["sent"] is True and sent["response"] is None
    [request] = fake_kite.writes()
    assert request.method == "DELETE" and request.url.path == "/alerts" and request.content == b""
    assert parse_qs(request.url.query.decode()) == {"uuid": [UUID, OTHER]}
    assert request.url.query.decode().count("uuid=") == 2  # repeated, not comma-joined
    assert rt.audit.usage().orders == 0


async def test_delete_alerts_ignores_caps_and_allow_lists_but_not_confirmation(mcp_factory, fake_kite):
    kite(fake_kite).on("GET", "/alerts", [alert_row(UUID, "NIFTY 50", "enabled", "2025-05-26 12:07:50")])
    server, _, _ = mcp_factory(
        **LIVE,
        KITE_MCP_MAX_ORDER_VALUE="1",
        KITE_MCP_MAX_ORDERS_PER_DAY="0",
        KITE_MCP_ALLOWED_PRODUCTS="MIS",
        KITE_MCP_LIVE_CONFIRMATION="allowlist",
        KITE_MCP_ALLOWED_INSTRUMENTS="NSE:INFY",
    )
    preview = tool_json(await call(server, "delete_alerts", {"uuids": [UUID]}))
    assert preview["checks"]["ok"] is True and "confirmation_token" in preview
    refused = await call(server, "delete_alerts", {"uuids": [UUID], "dry_run": False})
    assert refused.is_error and "confirmation_token is required" in tool_text(refused)


async def test_delete_alerts_warns_about_unknown_uuids_and_an_unreadable_list(mcp_factory, fake_kite):
    kite(fake_kite).on("GET", "/alerts", [alert_row(UUID, "NIFTY 50", "enabled", "2025-05-26 12:07:50")])
    server, _, _ = mcp_factory()
    answer = tool_json(await call(server, "delete_alerts", {"uuids": [UUID, OTHER]}))
    assert answer["checks"]["warnings"] == [f"no alert with uuid {OTHER} in your alert list"]

    fake_kite.routes.pop(("GET", "/alerts"))  # the list cannot be read (404)
    answer = tool_json(await call(server, "delete_alerts", {"uuids": [UUID]}))
    assert any("alert list could not be read" in w for w in answer["checks"]["warnings"])
    assert "alerts" not in answer and answer["summary"] == "delete 1 alert(s)"


async def test_delete_alerts_is_limited_to_fifty_uuids(mcp_factory, fake_kite):
    server, _, _ = mcp_factory()
    many = [f"00000000-0000-4000-8000-{i:012d}" for i in range(51)]
    assert (await call(server, "delete_alerts", {"uuids": many})).is_error
    assert (await call(server, "delete_alerts", {"uuids": []})).is_error
    assert fake_kite.requests == []


# --- kite's errors --------------------------------------------------------------------------------------------------


async def test_kites_rejection_is_reported_audited_and_not_retried(mcp_factory, fake_kite):
    kite(fake_kite).on("POST", "/alerts", status=400, error_type="InputException", message="too many active alerts")
    server, _, rt = mcp_factory(**LIVE)
    token = tool_json(await call(server, "create_alert", SIMPLE))["confirmation_token"]
    result = await call(server, "create_alert", {**SIMPLE, "dry_run": False, "confirmation_token": token})
    assert result.is_error and "too many active alerts" in tool_text(result) and "InputException" in tool_text(result)
    assert len(fake_kite.writes()) == 1
    assert [e["event"] for e in rt.audit.read()][-2:] == ["live_intent", "live_error"]


async def test_live_write_is_refused_while_trading_mode_is_dry_run(mcp_factory, fake_kite):
    kite(fake_kite)
    server, _, _ = mcp_factory()
    for name, args in (
        ("create_alert", SIMPLE),
        ("modify_alert", {"uuid": UUID, **SIMPLE}),
        ("delete_alerts", {"uuids": [UUID]}),
    ):
        result = await call(server, name, {**args, "dry_run": False})
        assert result.is_error and "KITE_MCP_TRADING_MODE=dry_run" in tool_text(result), name
    assert fake_kite.writes() == []


# --- exposure -------------------------------------------------------------------------------------------------------


async def test_read_only_hides_the_alert_write_tools(mcp_factory):
    server, reg, _ = mcp_factory(KITE_MCP_READ_ONLY="true")
    async with Client(server) as client:
        names = {t.name for t in (await client.list_tools()).tools}
    assert {"get_alerts", "get_alert", "get_alert_history"} <= names
    assert not {"create_alert", "modify_alert", "delete_alerts"} & names
    assert reg.skipped["create_alert"] == "read-only mode"


async def test_the_alerts_toolset_can_be_switched_off(mcp_factory):
    _, reg, _ = mcp_factory(KITE_MCP_TOOLSETS="orders,safety")
    assert not [i for i in reg.registered if i.toolset == "alerts"]
    _, reg, _ = mcp_factory(KITE_MCP_EXCLUDED_TOOLS="delete_alerts")
    assert "delete_alerts" in reg.skipped and "create_alert" not in reg.skipped


async def test_descriptions_state_the_limit_and_the_dry_run_default(mcp_factory):
    _, reg, _ = mcp_factory()
    by_name = {info.name: info for info in reg.registered}
    assert "at most 500 active alerts" in " ".join(by_name["create_alert"].description.split())
    assert "at most 500 active alerts" in " ".join(by_name["get_alerts"].description.split())
    for name in ("create_alert", "modify_alert", "delete_alerts"):
        assert by_name[name].kind == "write" and "DRY RUN BY DEFAULT" in " ".join(by_name[name].description.split())
    # what an ATO alert does later, and what that costs the user in confirmations
    assert "no confirmation at that moment" in " ".join(by_name["create_alert"].description.split())
