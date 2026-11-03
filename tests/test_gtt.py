"""GTT tools: the exact requests (dry run and live), validation, caps on the legs, modify and delete."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import httpx2
import pytest
from mcp import Client

from conftest import tool_json, tool_text
from kite_connect_mcp.instruments import Instrument

LIVE = {"KITE_MCP_TRADING_MODE": "live"}

SINGLE: dict[str, Any] = {
    "type": "single",
    "exchange": "NSE",
    "tradingsymbol": "INFY",
    "trigger_values": [1400.0],
    "last_price": 1500.0,
    "orders": [{"transaction_type": "BUY", "quantity": 2, "price": 1400.5, "product": "CNC"}],
}
# an OCO: a stop-loss below the price (5598.00) and a target above it (6402.00); only one can execute
OCO: dict[str, Any] = {
    "type": "two-leg",
    "exchange": "NSE",
    "tradingsymbol": "INFY",
    "trigger_values": [1400.0, 1600.0],
    "last_price": 1500.0,
    "orders": [
        {"transaction_type": "SELL", "quantity": 4, "price": 1399.5, "product": "CNC"},
        {"transaction_type": "SELL", "quantity": 4, "price": 1600.5, "product": "CNC"},
    ],
}


def order_json(side: str, quantity: int, price: float, product: str = "CNC") -> dict[str, Any]:
    return {
        "exchange": "NSE",
        "tradingsymbol": "INFY",
        "transaction_type": side,
        "quantity": quantity,
        "order_type": "LIMIT",
        "product": product,
        "price": price,
    }


def stored_gtt(*, status: str = "active", quantity: int = 2, price: float = 1400.5, product: str = "CNC") -> dict:
    """GET /gtt/triggers/123 as the docs show it."""
    return {
        "id": 123,
        "user_id": "XX0000",
        "parent_trigger": None,
        "type": "single",
        "created_at": "2026-11-03 10:00:00",
        "updated_at": "2026-11-03 10:00:00",
        "expires_at": "2027-11-03 10:00:00",
        "status": status,
        "condition": {
            "exchange": "NSE",
            "last_price": 1500,
            "tradingsymbol": "INFY",
            "trigger_values": [1400.0],
            "instrument_token": 408065,
        },
        "orders": [
            {
                "exchange": "NSE",
                "tradingsymbol": "INFY",
                "product": product,
                "order_type": "LIMIT",
                "transaction_type": "BUY",
                "quantity": quantity,
                "price": price,
                "result": None,
            }
        ],
        "meta": {},
    }


def kite(fake):
    fake.on("POST", "/gtt/triggers", {"trigger_id": 123})
    fake.on("PUT", "/gtt/triggers/123", {"trigger_id": 123})
    fake.on("DELETE", "/gtt/triggers/123", {"trigger_id": 123})
    fake.on("POST", "/margins/orders", [{"type": "equity", "total": 2801.0}])
    fake.on("GET", "/quote/ltp", {"NSE:INFY": {"instrument_token": 408065, "last_price": 1500.0}})
    return fake


async def call(server, name, args):
    async with Client(server) as client:
        return await client.call_tool(name, args)


async def go_live(server, name, args):
    """The two-step flow: a dry run for the token, then the identical call with dry_run=false."""
    preview = tool_json(await call(server, name, args))
    return tool_json(
        await call(server, name, {**args, "dry_run": False, "confirmation_token": preview["confirmation_token"]})
    )


def sent_form(fake, index: int = -1) -> dict[str, str]:
    return dict(fake.form(fake.writes()[index]))


class FakeStore:
    """An instrument master with just the instruments a test needs."""

    def __init__(self, *instruments: Instrument):
        self.table = {i.key: i for i in instruments}

    async def lookup(self, exchange: str, tradingsymbol: str) -> Instrument | None:
        return self.table.get(f"{exchange}:{tradingsymbol}")


def instrument(symbol: str = "INFY", exchange: str = "NSE", tick: str = "0.05", lot: int = 1) -> Instrument:
    return Instrument(
        instrument_token=408065,
        exchange_token=1594,
        tradingsymbol=symbol,
        name=symbol,
        last_price=0.0,
        expiry=None,
        strike=0.0,
        tick_size=Decimal(tick),
        lot_size=lot,
        instrument_type="EQ",
        segment=exchange,
        exchange=exchange,
    )


# --- reads ----------------------------------------------------------------------------------------------------------


async def test_get_gtts_filters_by_status_and_paginates_newest_first(mcp_factory, fake_kite):
    rows = [
        {"id": 1, "status": "triggered", "created_at": "2026-11-01 09:00:00"},
        {"id": 2, "status": "active", "created_at": "2026-11-02 09:00:00"},
        {"id": 3, "status": "active", "created_at": "2026-11-03 09:00:00"},
        {"id": 4, "status": "deleted", "created_at": "2026-11-04 09:00:00"},
        {"id": 5, "status": "active", "created_at": "2026-11-03 09:00:00"},
    ]
    fake_kite.on("GET", "/gtt/triggers", rows)
    server, _, _ = mcp_factory()
    everything = tool_json(await call(server, "get_gtts", {}))
    assert [g["id"] for g in everything["items"]] == [4, 5, 3, 2, 1]  # newest first, id breaks the tie
    page = tool_json(await call(server, "get_gtts", {"status": "active", "limit": 2}))
    assert page["total"] == 3 and page["count"] == 2 and page["next_offset"] == 2
    assert [g["id"] for g in page["items"]] == [5, 3]
    last = tool_json(await call(server, "get_gtts", {"status": "active", "limit": 2, "offset": 2}))
    assert [g["id"] for g in last["items"]] == [2] and "next_offset" not in last
    assert fake_kite.calls("GET", "/gtt/triggers")[0].url.query == b""  # the status filter is applied here, not by Kite


async def test_get_gtt_reads_one_trigger_and_rejects_bad_ids(mcp_factory, fake_kite):
    fake_kite.on("GET", "/gtt/triggers/123", stored_gtt(status="triggered"))
    server, _, _ = mcp_factory()
    assert tool_json(await call(server, "get_gtt", {"trigger_id": 123}))["status"] == "triggered"
    for bad in (0, -5, "12/../orders", "abc"):
        result = await call(server, "get_gtt", {"trigger_id": bad})
        assert result.is_error, bad
    assert len(fake_kite.requests) == 1  # only the good id reached Kite


async def test_read_errors_are_mapped(mcp_factory, fake_kite):
    fake_kite.on("GET", "/gtt/triggers", status=403, error_type="TokenException", message="Incorrect api_key")
    server, _, _ = mcp_factory()
    result = await call(server, "get_gtts", {})
    assert result.is_error and "rejected the session" in tool_text(result) and "login" in tool_text(result)


# --- place: the request ---------------------------------------------------------------------------------------------


async def test_place_single_gtt_dry_run_shows_the_exact_request(mcp_factory, fake_kite):
    kite(fake_kite)
    server, _, rt = mcp_factory()
    answer = tool_json(await call(server, "place_gtt_order", {**SINGLE, "tradingsymbol": "infy"}))
    assert answer["dry_run"] is True and answer["sent"] is False
    assert answer["would_send"]["method"] == "POST" and answer["would_send"]["path"] == "/gtt/triggers"
    form = answer["would_send"]["form"]
    assert set(form) == {"type", "condition", "orders"}
    assert form["type"] == "single"
    assert json.loads(form["condition"]) == {
        "exchange": "NSE",
        "tradingsymbol": "INFY",
        "trigger_values": [1400.0],
        "last_price": 1500.0,
    }
    assert json.loads(form["orders"]) == [order_json("BUY", 2, 1400.5)]
    assert answer["checks"]["ok"] is True
    assert [leg["value_inr"] for leg in answer["legs"]] == ["2801.00"]
    assert answer["legs"][0]["variety"] == "regular" and answer["legs"][0]["instrument"] == "NSE:INFY"
    assert answer["margins"][0]["total"] == 2801.0  # Kite's own margin calculation for the order
    assert "confirmation_token" not in answer and "disabled" in answer["live_trading"]
    assert fake_kite.writes() == []
    assert [e["event"] for e in rt.audit.read()] == ["dry_run"]


async def test_place_single_gtt_live_sends_the_documented_form_once(mcp_factory, fake_kite):
    kite(fake_kite)
    server, _, rt = mcp_factory(**LIVE)
    preview = tool_json(await call(server, "place_gtt_order", SINGLE))
    token = preview["confirmation_token"]
    assert fake_kite.writes() == []

    no_token = await call(server, "place_gtt_order", {**SINGLE, "dry_run": False})
    assert no_token.is_error and "confirmation_token is required" in tool_text(no_token)

    sent = tool_json(await call(server, "place_gtt_order", {**SINGLE, "dry_run": False, "confirmation_token": token}))
    assert sent["sent"] is True and sent["response"] == {"trigger_id": 123}
    [request] = fake_kite.writes()
    assert request.method == "POST" and request.url.path == "/gtt/triggers"
    assert request.headers["content-type"] == "application/x-www-form-urlencoded"
    form = sent_form(fake_kite)
    assert set(form) == {"type", "condition", "orders"}
    assert form["type"] == "single"
    assert json.loads(form["condition"]) == {
        "exchange": "NSE",
        "tradingsymbol": "INFY",
        "trigger_values": [1400.0],
        "last_price": 1500.0,
    }
    assert json.loads(form["orders"]) == [order_json("BUY", 2, 1400.5)]

    replay = await call(server, "place_gtt_order", {**SINGLE, "dry_run": False, "confirmation_token": token})
    assert replay.is_error and "already used" in tool_text(replay)
    assert len(fake_kite.writes()) == 1
    events = [e["event"] for e in rt.audit.read()]
    assert events == ["confirmation_issued", "refused", "live_intent", "live_result", "refused"]
    usage = rt.audit.usage()
    assert usage.orders == 1 and usage.value == Decimal("2801")  # one GTT is one order slot
    assert rt.audit.read()[3]["order_id"] == "123"  # the trigger_id is audited like an order id


async def test_place_two_leg_gtt_sends_both_triggers_and_both_orders(mcp_factory, fake_kite):
    kite(fake_kite)
    server, _, _ = mcp_factory(**LIVE)
    sent = await go_live(server, "place_gtt_order", OCO)
    assert sent["sent"] is True
    form = sent_form(fake_kite)
    assert form["type"] == "two-leg"
    assert json.loads(form["condition"])["trigger_values"] == [1400.0, 1600.0]
    assert json.loads(form["orders"]) == [order_json("SELL", 4, 1399.5), order_json("SELL", 4, 1600.5)]


async def test_place_gtt_is_refused_live_while_trading_mode_is_dry_run(mcp_factory, fake_kite):
    kite(fake_kite)
    server, _, rt = mcp_factory()
    result = await call(server, "place_gtt_order", {**SINGLE, "dry_run": False})
    assert result.is_error and "KITE_MCP_TRADING_MODE=dry_run" in tool_text(result)
    assert fake_kite.writes() == [] and rt.audit.read()[-1]["event"] == "refused"


async def test_token_is_bound_to_the_exact_gtt(mcp_factory, fake_kite):
    kite(fake_kite)
    server, _, _ = mcp_factory(**LIVE)
    token = tool_json(await call(server, "place_gtt_order", SINGLE))["confirmation_token"]
    changed = {**SINGLE, "trigger_values": [1390.0], "dry_run": False, "confirmation_token": token}
    result = await call(server, "place_gtt_order", changed)
    assert result.is_error and "does not match" in tool_text(result)
    assert fake_kite.writes() == []


# --- place: caps and allow-lists ------------------------------------------------------------------------------------


async def test_oco_adds_the_larger_leg_to_the_day_not_the_sum(mcp_factory, fake_kite):
    kite(fake_kite)
    # the legs are 5598.00 and 6402.00: the sum (12000) would break the 7000 daily cap, the larger leg does not
    server, _, rt = mcp_factory(**LIVE, KITE_MCP_MAX_DAILY_VALUE="7000")
    preview = tool_json(await call(server, "place_gtt_order", OCO))
    assert preview["checks"]["ok"] is True
    assert [leg["value_inr"] for leg in preview["legs"]] == ["5598.00", "6402.00"]
    tool_json(
        await call(
            server, "place_gtt_order", {**OCO, "dry_run": False, "confirmation_token": preview["confirmation_token"]}
        )
    )
    usage = rt.audit.usage()
    assert usage.orders == 1 and usage.value == Decimal("6402")
    second = tool_json(await call(server, "place_gtt_order", OCO))  # 6402 + 6402 > 7000
    assert any("daily cap" in v for v in second["checks"]["violations"])
    assert "confirmation_token" not in second


async def test_every_leg_of_an_oco_meets_the_per_order_cap(mcp_factory, fake_kite):
    kite(fake_kite)
    server, _, _ = mcp_factory(**LIVE, KITE_MCP_MAX_ORDER_VALUE="6000")
    answer = tool_json(await call(server, "place_gtt_order", OCO))
    violations = answer["checks"]["violations"]
    assert len(violations) == 1 and "₹6,402.00" in violations[0] and "per-order cap" in violations[0]
    assert "confirmation_token" not in answer
    refused = await call(server, "place_gtt_order", {**OCO, "dry_run": False})
    assert refused.is_error and "per-order cap" in tool_text(refused)
    assert fake_kite.writes() == []


async def test_a_gtt_uses_a_slot_of_the_daily_order_count(mcp_factory, fake_kite):
    kite(fake_kite)
    server, _, _ = mcp_factory(**LIVE, KITE_MCP_MAX_ORDERS_PER_DAY="1")
    await go_live(server, "place_gtt_order", SINGLE)
    second = tool_json(await call(server, "place_gtt_order", SINGLE))
    assert any("daily limit of 1" in v for v in second["checks"]["violations"])
    assert len(fake_kite.writes()) == 1


async def test_allow_lists_judge_every_leg(mcp_factory, fake_kite):
    kite(fake_kite)
    server, _, _ = mcp_factory(
        **LIVE,
        KITE_MCP_ALLOWED_EXCHANGES="BSE",
        KITE_MCP_ALLOWED_PRODUCTS="MIS",
        KITE_MCP_ALLOWED_TRANSACTION_TYPES="SELL",
        KITE_MCP_MAX_QUANTITY="1",
    )
    violations = " ".join(tool_json(await call(server, "place_gtt_order", SINGLE))["checks"]["violations"])
    for expected in ("exchange NSE", "product CNC", "transaction_type BUY", "KITE_MCP_MAX_QUANTITY"):
        assert expected in violations


async def test_allowlist_confirmation_mode_applies_to_gtt_instruments(mcp_factory, fake_kite):
    kite(fake_kite)
    server, _, _ = mcp_factory(**LIVE, KITE_MCP_LIVE_CONFIRMATION="allowlist", KITE_MCP_ALLOWED_INSTRUMENTS="NSE:INFY")
    sent = tool_json(await call(server, "place_gtt_order", {**SINGLE, "dry_run": False}))
    assert sent["sent"] is True
    other = await call(server, "place_gtt_order", {**SINGLE, "tradingsymbol": "TCS", "dry_run": False})
    assert other.is_error and "ALLOWED_INSTRUMENTS" in tool_text(other)
    assert len(fake_kite.writes()) == 1


async def test_commodity_gtt_cannot_be_valued_so_cannot_go_live(mcp_factory, fake_kite):
    kite(fake_kite)
    server, _, _ = mcp_factory(**LIVE)
    mcx = {
        **SINGLE,
        "exchange": "MCX",
        "tradingsymbol": "GOLDM25DECFUT",
        "orders": [{**SINGLE["orders"][0], "product": "NRML"}],
    }
    answer = tool_json(await call(server, "place_gtt_order", mcx))
    assert answer["checks"]["ok"] is False and "lots" in answer["legs"][0]["value_note"]
    assert "confirmation_token" not in answer


# --- place: validation ----------------------------------------------------------------------------------------------


def with_(base: dict[str, Any], **changes: Any) -> dict[str, Any]:
    return {**base, **changes}


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (with_(SINGLE, trigger_values=[1400.0, 1450.0]), "exactly 1 trigger value"),
        (with_(SINGLE, orders=SINGLE["orders"] * 2), "exactly 1 order"),
        (with_(OCO, trigger_values=[1400.0]), "exactly 2 trigger value"),
        (with_(OCO, orders=OCO["orders"][:1]), "exactly 2 order"),
        (with_(SINGLE, trigger_values=[1500.0]), "equals last_price"),
        (with_(SINGLE, tradingsymbol="   "), "tradingsymbol must not be blank"),
        (with_(OCO, trigger_values=[1400.0, 1450.0]), "last_price between the two triggers"),
        (with_(OCO, trigger_values=[1550.0, 1600.0]), "last_price between the two triggers"),
        (with_(OCO, trigger_values=[1600.0, 1400.0]), "lower trigger first"),
        (with_(OCO, trigger_values=[1400.0, 1500.0]), "last_price between the two triggers"),
        (with_(SINGLE, exchange="NFO", tradingsymbol="NIFTY25NOVFUT"), "CNC is for equity delivery"),
    ],
)
async def test_impossible_gtts_are_rejected_before_anything(mcp_factory, fake_kite, args, message):
    kite(fake_kite)
    server, _, rt = mcp_factory()
    result = await call(server, "place_gtt_order", args)
    assert result.is_error and message in tool_text(result)
    assert fake_kite.requests == [] and rt.audit.read() == []


@pytest.mark.parametrize(
    "args",
    [
        with_(SINGLE, type="triple"),
        with_(SINGLE, trigger_values=[]),
        with_(SINGLE, trigger_values=[-1.0]),
        with_(SINGLE, trigger_values=[1.0, 2.0, 3.0]),
        with_(SINGLE, last_price=0),
        with_(SINGLE, exchange="INDICES"),
        with_(SINGLE, orders=[]),
        with_(SINGLE, orders=[{**SINGLE["orders"][0], "order_type": "MARKET"}]),
        with_(SINGLE, orders=[{**SINGLE["orders"][0], "quantity": 0}]),
        with_(SINGLE, orders=[{**SINGLE["orders"][0], "price": 0}]),
        with_(SINGLE, orders=[{**SINGLE["orders"][0], "trigger_price": 1.0}]),
        with_(SINGLE, orders=[{**SINGLE["orders"][0], "exchange": "NSE"}]),
        with_(SINGLE, orders=[{"transaction_type": "BUY", "quantity": 1, "price": 1.0}]),
    ],
)
async def test_the_schema_refuses_malformed_gtts(mcp_factory, fake_kite, args):
    kite(fake_kite)
    server, _, rt = mcp_factory()
    result = await call(server, "place_gtt_order", args)
    assert result.is_error
    assert fake_kite.requests == [] and rt.audit.read() == []


async def test_tick_and_lot_size_come_from_the_instrument_master(mcp_factory, fake_kite):
    kite(fake_kite)
    server, _, rt = mcp_factory()
    rt.instruments = FakeStore(instrument(), instrument("NIFTY25NOVFUT", "NFO", lot=75))
    good = tool_json(await call(server, "place_gtt_order", SINGLE))
    assert good["checks"]["ok"] is True
    assert all("instrument master" not in w for w in good["checks"]["warnings"])  # the store answered: no warning

    off_tick = with_(SINGLE, orders=[{**SINGLE["orders"][0], "price": 1400.03}])
    result = await call(server, "place_gtt_order", off_tick)
    assert result.is_error and "1400.03 is not a multiple of the tick size 0.05" in tool_text(result)

    trigger_off_tick = with_(SINGLE, trigger_values=[1400.02])
    result = await call(server, "place_gtt_order", trigger_off_tick)
    assert result.is_error and "1400.02 is not a multiple of the tick size" in tool_text(result)

    future = with_(
        SINGLE,
        exchange="NFO",
        tradingsymbol="NIFTY25NOVFUT",
        orders=[{"transaction_type": "BUY", "quantity": 50, "price": 24000.0, "product": "NRML"}],
        trigger_values=[23900.0],
        last_price=24100.0,
    )
    result = await call(server, "place_gtt_order", future)
    assert result.is_error and "not a multiple of the lot size 75" in tool_text(result)

    unknown = await call(server, "place_gtt_order", with_(SINGLE, tradingsymbol="NOPE"))
    assert unknown.is_error and "not in today's instrument list" in tool_text(unknown)
    assert fake_kite.writes() == []


async def test_an_unavailable_instrument_master_is_only_a_warning(mcp_factory, fake_kite):
    kite(fake_kite)
    server, _, _ = mcp_factory()  # the default store cannot be read in this test setup
    answer = tool_json(await call(server, "place_gtt_order", SINGLE))
    assert answer["checks"]["ok"] is True
    assert any("instrument master" in w and "not checked" in w for w in answer["checks"]["warnings"])


async def test_mis_gtt_is_allowed_with_a_warning(mcp_factory, fake_kite):
    kite(fake_kite)
    server, _, _ = mcp_factory()
    mis = with_(SINGLE, orders=[{**SINGLE["orders"][0], "product": "MIS"}])
    answer = tool_json(await call(server, "place_gtt_order", mis))
    assert answer["checks"]["ok"] is True and any("MIS is intraday" in w for w in answer["checks"]["warnings"])


# --- place: last_price against the live price -----------------------------------------------------------------------


def ltp(fake, price: float):
    fake.on("GET", "/quote/ltp", {"NSE:INFY": {"instrument_token": 408065, "last_price": price}})


async def test_last_price_matching_the_live_price_raises_no_warning(mcp_factory, fake_kite):
    kite(fake_kite)
    ltp(fake_kite, 1502.0)
    server, _, _ = mcp_factory()
    warnings = tool_json(await call(server, "place_gtt_order", SINGLE))["checks"]["warnings"]
    assert all("live price" not in w for w in warnings)


async def test_a_stale_last_price_is_flagged(mcp_factory, fake_kite):
    kite(fake_kite)
    ltp(fake_kite, 1560.0)  # 4% above what the model passed
    server, _, _ = mcp_factory(**LIVE)
    answer = tool_json(await call(server, "place_gtt_order", SINGLE))
    assert any("more than 2% away from the live price 1560" in w for w in answer["checks"]["warnings"])
    assert answer["checks"]["ok"] is True  # a warning, not a refusal


async def test_a_trigger_already_past_the_live_price_is_flagged(mcp_factory, fake_kite):
    kite(fake_kite)
    ltp(fake_kite, 1390.0)  # already below the 1400 trigger that last_price 1500 says is further down
    server, _, _ = mcp_factory()
    warnings = " ".join(tool_json(await call(server, "place_gtt_order", SINGLE))["checks"]["warnings"])
    assert "other side of the live price 1390" in warnings and "may fire at once" in warnings


async def test_without_market_data_the_last_price_cannot_be_checked(mcp_factory, fake_kite):
    kite(fake_kite)
    fake_kite.on("GET", "/quote/ltp", status=403, error_type="PermissionException", message="Insufficient permission")
    server, _, rt = mcp_factory(**LIVE)
    answer = tool_json(await call(server, "place_gtt_order", SINGLE))
    assert (
        "last_price was not compared with the live price: no market data on this Kite plan (Personal)"
        in (answer["checks"]["warnings"])
    )  # the reason, without the order-sizing advice the shared helper appends
    assert answer["checks"]["ok"] is True and "confirmation_token" in answer
    assert rt.tokens.current() is not None  # a PermissionException is not a dead session

    fake_kite.on("GET", "/quote/ltp", {})  # Kite knows no such instrument
    unknown = tool_json(await call(server, "place_gtt_order", SINGLE))
    assert any("Kite returned no LTP for NSE:INFY" in w for w in unknown["checks"]["warnings"])


async def test_the_live_call_does_not_fetch_the_quote_again(mcp_factory, fake_kite):
    kite(fake_kite)
    server, _, _ = mcp_factory(**LIVE)
    await go_live(server, "place_gtt_order", SINGLE)
    assert len(fake_kite.calls("GET", "/quote/ltp")) == 1  # only the dry run looks at the market


# --- modify ---------------------------------------------------------------------------------------------------------


def modify_args(**changes: Any) -> dict[str, Any]:
    order = {**SINGLE["orders"][0], **changes.pop("order", {})}
    return {"trigger_id": 123, **SINGLE, "orders": [order], **changes}


async def test_modify_gtt_puts_the_complete_new_trigger(mcp_factory, fake_kite):
    kite(fake_kite).on("GET", "/gtt/triggers/123", stored_gtt())
    server, _, rt = mcp_factory(**LIVE)
    args = modify_args(order={"price": 1405.0}, trigger_values=[1401.0])
    preview = tool_json(await call(server, "modify_gtt_order", args))
    assert preview["would_send"]["method"] == "PUT" and preview["would_send"]["path"] == "/gtt/triggers/123"
    assert preview["current_gtt"]["status"] == "active"  # the stored trigger is shown next to the change
    assert preview["checks"]["ok"] is True and preview["legs"][0]["value_inr"] == "2810.00"
    assert fake_kite.writes() == []

    sent = tool_json(
        await call(
            server, "modify_gtt_order", {**args, "dry_run": False, "confirmation_token": preview["confirmation_token"]}
        )
    )
    assert sent["sent"] is True and sent["response"] == {"trigger_id": 123}
    [request] = fake_kite.writes()
    assert request.method == "PUT" and request.url.path == "/gtt/triggers/123"
    form = sent_form(fake_kite)
    assert set(form) == {"type", "condition", "orders"}
    assert json.loads(form["condition"])["trigger_values"] == [1401.0]
    assert json.loads(form["orders"]) == [order_json("BUY", 2, 1405.0)]
    assert rt.audit.read()[-1]["order_id"] == "123"


async def test_modify_counts_only_the_increase_and_takes_no_new_slot(mcp_factory, fake_kite):
    kite(fake_kite).on("GET", "/gtt/triggers/123", stored_gtt())  # worth 2 x 1400.5 = 2801.00
    server, _, rt = mcp_factory(**LIVE, KITE_MCP_MAX_ORDERS_PER_DAY="1")
    await go_live(server, "place_gtt_order", SINGLE)
    assert rt.audit.usage().orders == 1 and rt.audit.usage().value == Decimal("2801")

    await go_live(server, "modify_gtt_order", modify_args(order={"price": 1405.0}))  # +9.00
    usage = rt.audit.usage()
    assert usage.orders == 1 and usage.value == Decimal("2810")  # the day's order count did not move

    await go_live(server, "modify_gtt_order", modify_args(order={"price": 1395.0}))  # cheaper: adds nothing
    usage = rt.audit.usage()
    assert usage.orders == 1 and usage.value == Decimal("2810")
    assert len(fake_kite.writes()) == 3


async def test_modify_is_held_to_the_daily_value_cap_and_the_per_order_cap(mcp_factory, fake_kite):
    kite(fake_kite).on("GET", "/gtt/triggers/123", stored_gtt())
    server, _, _ = mcp_factory(**LIVE, KITE_MCP_MAX_DAILY_VALUE="5")
    answer = tool_json(await call(server, "modify_gtt_order", modify_args(order={"price": 1405.0})))  # +9.00 > 5
    assert any("daily cap" in v and "₹9.00" in v for v in answer["checks"]["violations"])
    assert "confirmation_token" not in answer

    capped, _, _ = mcp_factory(**LIVE, KITE_MCP_MAX_ORDER_VALUE="2000")
    answer = tool_json(await call(capped, "modify_gtt_order", modify_args(order={"price": 1405.0})))
    assert any("per-order cap" in v for v in answer["checks"]["violations"])
    assert fake_kite.writes() == []


async def test_modify_warns_about_an_inactive_or_unreadable_trigger(mcp_factory, fake_kite):
    kite(fake_kite).on("GET", "/gtt/triggers/123", stored_gtt(status="triggered"))
    server, _, rt = mcp_factory(**LIVE)
    answer = tool_json(await call(server, "modify_gtt_order", modify_args()))
    assert any("status is triggered" in w and "only modifies active" in w for w in answer["checks"]["warnings"])

    fake_kite.routes.pop(("GET", "/gtt/triggers/123"))  # Kite no longer knows it
    answer = tool_json(await call(server, "modify_gtt_order", modify_args()))
    assert any("GTT 123 could not be read" in w for w in answer["checks"]["warnings"])
    assert "current_gtt" not in answer
    await go_live(server, "modify_gtt_order", modify_args())  # the whole new value counts: it cannot be netted
    assert rt.audit.usage().value == Decimal("2801")


async def test_modify_validates_like_place(mcp_factory, fake_kite):
    kite(fake_kite).on("GET", "/gtt/triggers/123", stored_gtt())
    server, _, _ = mcp_factory()
    result = await call(server, "modify_gtt_order", modify_args(trigger_values=[1500.0]))
    assert result.is_error and "equals last_price" in tool_text(result)
    assert (await call(server, "modify_gtt_order", {**modify_args(), "trigger_id": 0})).is_error
    assert fake_kite.requests == []


# --- delete ---------------------------------------------------------------------------------------------------------


async def test_delete_is_a_confirmed_write_even_though_it_reduces_risk(mcp_factory, fake_kite):
    # worth 140,000: far over the per-order cap, which must not stand in the way of a deletion
    kite(fake_kite).on("GET", "/gtt/triggers/123", stored_gtt(quantity=100, price=1400.0))
    server, _, rt = mcp_factory(**LIVE)
    preview = tool_json(await call(server, "delete_gtt_order", {"trigger_id": 123}))
    assert preview["would_send"] == {"method": "DELETE", "path": "/gtt/triggers/123"}
    assert preview["checks"]["ok"] is True
    assert "delete GTT 123" in preview["summary"] and "BUY 100 @ 1400.0" in preview["summary"]
    assert preview["current_gtt"]["orders"][0]["quantity"] == 100
    assert fake_kite.writes() == []

    refused = await call(server, "delete_gtt_order", {"trigger_id": 123, "dry_run": False})
    assert refused.is_error and "confirmation_token is required" in tool_text(refused)
    assert fake_kite.writes() == []

    args = {"trigger_id": 123, "dry_run": False, "confirmation_token": preview["confirmation_token"]}
    sent = tool_json(await call(server, "delete_gtt_order", args))
    assert sent["sent"] is True and sent["response"] == {"trigger_id": 123}
    [request] = fake_kite.writes()
    assert request.method == "DELETE" and request.url.path == "/gtt/triggers/123" and request.content == b""
    usage = rt.audit.usage()
    assert usage.orders == 0 and usage.value == 0  # a deletion counts toward nothing
    assert [e["event"] for e in rt.audit.read()][-2:] == ["live_intent", "live_result"]


async def test_delete_is_judged_by_the_allow_lists_on_the_stored_orders(mcp_factory, fake_kite):
    # a delete is risk-reducing: only the exchange and instrument allow-lists judge it (a product list does not)
    kite(fake_kite).on("GET", "/gtt/triggers/123", stored_gtt())
    server, _, _ = mcp_factory(**LIVE, KITE_MCP_ALLOWED_EXCHANGES="BSE", KITE_MCP_ALLOWED_PRODUCTS="MIS")
    answer = tool_json(await call(server, "delete_gtt_order", {"trigger_id": 123}))
    violations = " ".join(answer["checks"]["violations"])
    assert answer["checks"]["ok"] is False and "exchange NSE" in violations
    assert "product" not in violations
    assert "confirmation_token" not in answer


async def test_delete_of_an_unreadable_gtt_still_needs_confirmation(mcp_factory, fake_kite):
    kite(fake_kite)  # no stored trigger: GET answers 404
    server, _, _ = mcp_factory(**LIVE)
    preview = tool_json(await call(server, "delete_gtt_order", {"trigger_id": 123}))
    assert any("could not be read" in w for w in preview["checks"]["warnings"])
    assert preview["legs"] == [] and "confirmation_token" in preview
    sent = tool_json(
        await call(
            server,
            "delete_gtt_order",
            {"trigger_id": 123, "dry_run": False, "confirmation_token": preview["confirmation_token"]},
        )
    )
    assert sent["sent"] is True and len(fake_kite.writes()) == 1


async def test_delete_warns_when_the_trigger_is_not_active(mcp_factory, fake_kite):
    kite(fake_kite).on("GET", "/gtt/triggers/123", stored_gtt(status="deleted"))
    server, _, _ = mcp_factory()
    warnings = tool_json(await call(server, "delete_gtt_order", {"trigger_id": 123}))["checks"]["warnings"]
    assert any("only deletes active" in w for w in warnings)


async def test_delete_stays_a_dry_run_unless_live_and_confirmed(mcp_factory, fake_kite):
    kite(fake_kite).on("GET", "/gtt/triggers/123", stored_gtt())
    server, _, _ = mcp_factory()
    refused = await call(server, "delete_gtt_order", {"trigger_id": 123, "dry_run": False})
    assert refused.is_error and "KITE_MCP_TRADING_MODE=dry_run" in tool_text(refused)
    assert fake_kite.writes() == []


# --- kite's errors --------------------------------------------------------------------------------------------------


async def test_kites_rejection_is_reported_audited_and_not_retried(mcp_factory, fake_kite):
    kite(fake_kite).on("POST", "/gtt/triggers", status=400, error_type="InputException", message="invalid trigger")
    server, _, rt = mcp_factory(**LIVE)
    token = tool_json(await call(server, "place_gtt_order", SINGLE))["confirmation_token"]
    result = await call(server, "place_gtt_order", {**SINGLE, "dry_run": False, "confirmation_token": token})
    assert result.is_error and "InputException" in tool_text(result) and "invalid trigger" in tool_text(result)
    assert len(fake_kite.writes()) == 1
    assert [e["event"] for e in rt.audit.read()][-2:] == ["live_intent", "live_error"]
    assert rt.audit.usage().orders == 0  # a failed GTT is not counted


async def test_an_unanswered_write_is_outcome_unknown(mcp_factory, fake_kite):
    def timeout(request):
        raise httpx2.ReadTimeout("slow", request=request)

    kite(fake_kite).on("DELETE", "/gtt/triggers/123", handler=timeout)
    fake_kite.on("GET", "/gtt/triggers/123", stored_gtt())
    server, _, rt = mcp_factory(**LIVE)
    preview = tool_json(await call(server, "delete_gtt_order", {"trigger_id": 123}))
    args = {"trigger_id": 123, "dry_run": False, "confirmation_token": preview["confirmation_token"]}
    result = await call(server, "delete_gtt_order", args)
    assert result.is_error and "get_gtts" in tool_text(result)
    assert len(fake_kite.writes()) == 1
    assert [e["event"] for e in rt.audit.read()][-2:] == ["live_intent", "live_unknown"]


# --- exposure -------------------------------------------------------------------------------------------------------


async def test_read_only_hides_the_gtt_write_tools(mcp_factory):
    server, reg, _ = mcp_factory(KITE_MCP_READ_ONLY="true")
    async with Client(server) as client:
        names = {t.name for t in (await client.list_tools()).tools}
    assert {"get_gtts", "get_gtt"} <= names
    assert not {"place_gtt_order", "modify_gtt_order", "delete_gtt_order"} & names
    assert reg.skipped["place_gtt_order"] == "read-only mode"


async def test_the_gtt_toolset_can_be_switched_off(mcp_factory):
    _, reg, _ = mcp_factory(KITE_MCP_TOOLSETS="orders,safety")
    assert not [i for i in reg.registered if i.toolset == "gtt"]
    assert "get_gtts" in reg.skipped and "KITE_MCP_TOOLSETS" in reg.skipped["get_gtts"]


async def test_write_tools_say_dry_run_by_default(mcp_factory):
    server, reg, _ = mcp_factory()
    by_name = {info.name: info for info in reg.registered}
    for name in ("place_gtt_order", "modify_gtt_order", "delete_gtt_order"):
        assert by_name[name].kind == "write" and "DRY RUN BY DEFAULT" in " ".join(by_name[name].description.split())
    async with Client(server) as client:
        tool = next(t for t in (await client.list_tools()).tools if t.name == "place_gtt_order")
    assert tool.input_schema["properties"]["dry_run"]["default"] is True
    assert set(tool.input_schema["required"]) == {
        "type",
        "exchange",
        "tradingsymbol",
        "trigger_values",
        "last_price",
        "orders",
    }
