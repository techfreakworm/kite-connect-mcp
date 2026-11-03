"""The write pipeline through the order tools: dry run by default, confirmation, caps, allow-lists, kill switch."""

from __future__ import annotations

import httpx2
import pytest
from mcp import Client

from conftest import tool_json, tool_text

LIMIT_BUY = {
    "exchange": "NSE",
    "tradingsymbol": "INFY",
    "transaction_type": "BUY",
    "quantity": 2,
    "product": "CNC",
    "order_type": "LIMIT",
    "price": 1500.0,
}
LIVE = {"KITE_MCP_TRADING_MODE": "live"}


def kite_with_margins(fake):
    fake.on("POST", "/margins/orders", [{"type": "equity", "total": 3000.0, "charges": {"total": 3.1}}])
    fake.on("POST", "/orders/regular", {"order_id": "250101000000001"})
    fake.on("GET", "/quote/ltp", {"NSE:INFY": {"instrument_token": 408065, "last_price": 1500.0}})
    return fake


async def call(server, name, args):
    async with Client(server) as client:
        return await client.call_tool(name, args)


async def test_place_order_is_a_dry_run_by_default(mcp_factory, fake_kite):
    kite_with_margins(fake_kite)
    server, _, rt = mcp_factory()
    answer = tool_json(await call(server, "place_order", LIMIT_BUY))
    assert answer["dry_run"] is True and answer["sent"] is False
    assert answer["would_send"]["path"] == "/orders/regular"
    assert answer["would_send"]["form"]["tradingsymbol"] == "INFY"
    assert answer["checks"]["ok"] is True
    assert answer["legs"][0]["value_inr"] == "3000.00"
    assert answer["margins"][0]["total"] == 3000.0
    assert "confirmation_token" not in answer  # live trading is off by default
    assert "disabled" in answer["live_trading"]
    assert fake_kite.writes() == []
    assert [e["event"] for e in rt.audit.read()] == ["dry_run"]


async def test_live_send_refused_when_trading_mode_is_dry_run(mcp_factory, fake_kite):
    kite_with_margins(fake_kite)
    server, _, rt = mcp_factory()
    result = await call(server, "place_order", {**LIMIT_BUY, "dry_run": False})
    assert result.is_error
    assert "KITE_MCP_TRADING_MODE=dry_run" in tool_text(result)
    assert fake_kite.writes() == []
    assert rt.audit.read()[-1]["event"] == "refused"


async def test_two_step_confirmation_sends_exactly_once(mcp_factory, fake_kite):
    kite_with_margins(fake_kite)
    server, _, rt = mcp_factory(**LIVE)
    preview = tool_json(await call(server, "place_order", LIMIT_BUY))
    token = preview["confirmation_token"]
    assert fake_kite.writes() == []

    no_token = await call(server, "place_order", {**LIMIT_BUY, "dry_run": False})
    assert no_token.is_error and "confirmation_token is required" in tool_text(no_token)

    sent = tool_json(await call(server, "place_order", {**LIMIT_BUY, "dry_run": False, "confirmation_token": token}))
    assert sent["sent"] is True and sent["response"]["order_id"] == "250101000000001"
    assert len(fake_kite.writes()) == 1
    form = dict(fake_kite.form(fake_kite.writes()[0]))
    assert form == {
        "exchange": "NSE",
        "tradingsymbol": "INFY",
        "transaction_type": "BUY",
        "quantity": "2",
        "product": "CNC",
        "order_type": "LIMIT",
        "price": "1500.0",
        "validity": "DAY",
    }

    replay = await call(server, "place_order", {**LIMIT_BUY, "dry_run": False, "confirmation_token": token})
    assert replay.is_error and "already used" in tool_text(replay)
    assert len(fake_kite.writes()) == 1
    events = [e["event"] for e in rt.audit.read()]
    assert events == ["confirmation_issued", "refused", "live_intent", "live_result", "refused"]
    assert rt.audit.usage().orders == 1


async def test_token_is_bound_to_the_exact_request(mcp_factory, fake_kite):
    kite_with_margins(fake_kite)
    server, _, _ = mcp_factory(**LIVE)
    token = tool_json(await call(server, "place_order", LIMIT_BUY))["confirmation_token"]
    changed = {**LIMIT_BUY, "quantity": 3, "dry_run": False, "confirmation_token": token}
    result = await call(server, "place_order", changed)
    assert result.is_error and "does not match" in tool_text(result)
    assert fake_kite.writes() == []


async def test_per_order_cap(mcp_factory, fake_kite):
    kite_with_margins(fake_kite)
    server, _, _ = mcp_factory(**LIVE, KITE_MCP_MAX_ORDER_VALUE="2000")
    answer = tool_json(await call(server, "place_order", LIMIT_BUY))
    assert answer["checks"]["ok"] is False
    assert any("per-order cap" in v for v in answer["checks"]["violations"])
    assert "confirmation_token" not in answer
    live = await call(server, "place_order", {**LIMIT_BUY, "dry_run": False})
    assert live.is_error and "per-order cap" in tool_text(live)
    assert fake_kite.writes() == []


async def test_daily_order_count_and_value_caps(mcp_factory, fake_kite):
    kite_with_margins(fake_kite)
    server, _, _ = mcp_factory(**LIVE, KITE_MCP_MAX_ORDERS_PER_DAY="1", KITE_MCP_MAX_DAILY_VALUE="5000")
    token = tool_json(await call(server, "place_order", LIMIT_BUY))["confirmation_token"]
    tool_json(await call(server, "place_order", {**LIMIT_BUY, "dry_run": False, "confirmation_token": token}))
    second = tool_json(await call(server, "place_order", LIMIT_BUY))
    violations = " ".join(second["checks"]["violations"])
    assert "daily limit of 1" in violations
    assert len(fake_kite.writes()) == 1


async def test_allow_lists(mcp_factory, fake_kite):
    kite_with_margins(fake_kite)
    server, _, _ = mcp_factory(**LIVE, KITE_MCP_ALLOWED_EXCHANGES="BSE", KITE_MCP_ALLOWED_PRODUCTS="MIS")
    answer = tool_json(await call(server, "place_order", LIMIT_BUY))
    violations = " ".join(answer["checks"]["violations"])
    assert "exchange NSE" in violations and "product CNC" in violations


async def test_allowlist_confirmation_mode_skips_token_only_for_listed_instruments(mcp_factory, fake_kite):
    kite_with_margins(fake_kite)
    fake_kite.on("POST", "/orders/regular", {"order_id": "2"})
    server, _, _ = mcp_factory(**LIVE, KITE_MCP_LIVE_CONFIRMATION="allowlist", KITE_MCP_ALLOWED_INSTRUMENTS="NSE:INFY")
    sent = tool_json(await call(server, "place_order", {**LIMIT_BUY, "dry_run": False}))
    assert sent["sent"] is True
    other = await call(server, "place_order", {**LIMIT_BUY, "tradingsymbol": "TCS", "dry_run": False})
    assert other.is_error and "ALLOWED_INSTRUMENTS" in tool_text(other)
    assert len(fake_kite.writes()) == 1


async def test_kill_switch_stops_everything(mcp_factory, fake_kite):
    kite_with_margins(fake_kite)
    server, _, rt = mcp_factory(**LIVE)
    token = tool_json(await call(server, "place_order", LIMIT_BUY))["confirmation_token"]
    tool_json(await call(server, "engage_kill_switch", {"reason": "testing the switch"}))
    assert rt.settings.kill_switch_file.exists()
    result = await call(server, "place_order", {**LIMIT_BUY, "dry_run": False, "confirmation_token": token})
    assert result.is_error and "kill switch" in tool_text(result)
    assert fake_kite.writes() == []


async def test_market_order_without_market_data_cannot_go_live(mcp_factory, fake_kite):
    fake_kite.on("POST", "/margins/orders", [])
    fake_kite.on("GET", "/quote/ltp", status=403, error_type="PermissionException", message="Insufficient permission")
    server, _, rt = mcp_factory(**LIVE)
    market = {**LIMIT_BUY, "order_type": "MARKET", "market_protection": -1}
    market.pop("price")
    answer = tool_json(await call(server, "place_order", market))
    assert answer["checks"]["ok"] is False
    assert "Personal" in answer["legs"][0]["value_note"]
    assert rt.tokens.current() is not None  # a PermissionException is not a dead session


async def test_market_order_value_uses_ltp_and_buffer(mcp_factory, fake_kite):
    kite_with_margins(fake_kite)
    server, _, _ = mcp_factory(**LIVE)
    market = {**LIMIT_BUY, "order_type": "MARKET", "market_protection": 2}
    market.pop("price")
    leg = tool_json(await call(server, "place_order", market))["legs"][0]
    assert leg["value_inr"] == "3060.00"  # 2 x 1500 x 1.02


async def test_commodity_value_cannot_be_verified(mcp_factory, fake_kite):
    kite_with_margins(fake_kite)
    server, _, _ = mcp_factory(**LIVE)
    mcx = {**LIMIT_BUY, "exchange": "MCX", "tradingsymbol": "GOLDM25DECFUT", "product": "NRML"}
    answer = tool_json(await call(server, "place_order", mcx))
    assert answer["checks"]["ok"] is False and "lots" in answer["legs"][0]["value_note"]


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"order_type": "LIMIT", "price": None}, "need a price"),
        ({"order_type": "SL"}, "trigger_price"),
        ({"variety": "iceberg"}, "iceberg_legs"),
        ({"variety": "auction"}, "auction_number"),
        ({"exchange": "NFO", "tradingsymbol": "NIFTY25NOVFUT", "product": "CNC"}, "CNC is for equity"),
        ({"market_protection": 5}, "only applies to MARKET"),
        ({"validity": "TTL"}, "validity_ttl"),
    ],
)
async def test_impossible_orders_are_rejected_before_anything(mcp_factory, fake_kite, change, message):
    kite_with_margins(fake_kite)
    server, _, rt = mcp_factory()
    args = {k: v for k, v in {**LIMIT_BUY, **change}.items() if v is not None}
    result = await call(server, "place_order", args)
    assert result.is_error and message in tool_text(result)
    assert fake_kite.requests == [] and rt.audit.read() == []


async def test_outcome_unknown_is_audited_and_never_retried(mcp_factory, fake_kite):
    kite_with_margins(fake_kite)

    def timeout(request):
        raise httpx2.ReadTimeout("slow", request=request)

    fake_kite.on("POST", "/orders/regular", handler=timeout)
    server, _, rt = mcp_factory(**LIVE)
    token = tool_json(await call(server, "place_order", LIMIT_BUY))["confirmation_token"]
    result = await call(server, "place_order", {**LIMIT_BUY, "dry_run": False, "confirmation_token": token})
    assert result.is_error and "before trying again" in tool_text(result)
    assert len(fake_kite.writes()) == 1
    assert [e["event"] for e in rt.audit.read()][-2:] == ["live_intent", "live_unknown"]


async def test_modify_counts_only_the_increase_and_caps_modifications(mcp_factory, fake_kite):
    kite_with_margins(fake_kite)
    fake_kite.on(
        "GET",
        "/orders/111",
        [
            {
                "order_id": "111",
                "status": "OPEN",
                "exchange": "NSE",
                "tradingsymbol": "INFY",
                "transaction_type": "BUY",
                "order_type": "LIMIT",
                "product": "CNC",
                "quantity": 2,
                "price": 1500.0,
                "variety": "regular",
            }
        ],
    )
    fake_kite.on("PUT", "/orders/regular/111", {"order_id": "111"})
    server, _, rt = mcp_factory(**LIVE, KITE_MCP_MAX_ORDERS_PER_DAY="100")
    args = {"order_id": "111", "price": 1490.0}
    preview = tool_json(await call(server, "modify_order", args))
    assert preview["checks"]["ok"] and preview["legs"][0]["value_inr"] == "2980.00"
    sent = tool_json(
        await call(
            server, "modify_order", {**args, "dry_run": False, "confirmation_token": preview["confirmation_token"]}
        )
    )
    assert sent["sent"] is True
    assert rt.audit.usage().value == 0  # a lower price adds nothing to the day's value
    assert rt.audit.modifications("111") == 1
    for _ in range(24):
        token = tool_json(await call(server, "modify_order", args))["confirmation_token"]
        tool_json(await call(server, "modify_order", {**args, "dry_run": False, "confirmation_token": token}))
    blocked = tool_json(await call(server, "modify_order", args))
    assert any("25 per order" in v for v in blocked["checks"]["violations"])


async def test_cancel_needs_confirmation_too(mcp_factory, fake_kite):
    fake_kite.on(
        "GET",
        "/orders/222",
        [
            {
                "order_id": "222",
                "status": "TRIGGER PENDING",
                "exchange": "NSE",
                "tradingsymbol": "INFY",
                "transaction_type": "SELL",
                "order_type": "SL-M",
                "product": "CNC",
                "quantity": 2,
                "trigger_price": 1400.0,
            }
        ],
    )
    fake_kite.on("DELETE", "/orders/regular/222", {"order_id": "222"})
    server, _, _ = mcp_factory(**LIVE)
    preview = tool_json(await call(server, "cancel_order", {"order_id": "222"}))
    assert "SELL 2 NSE:INFY" in preview["summary"] and preview["checks"]["ok"]
    refused = await call(server, "cancel_order", {"order_id": "222", "dry_run": False})
    assert refused.is_error
    done = tool_json(
        await call(
            server,
            "cancel_order",
            {"order_id": "222", "dry_run": False, "confirmation_token": preview["confirmation_token"]},
        )
    )
    assert done["sent"] is True and len(fake_kite.writes()) == 1


async def test_read_only_hides_write_tools(mcp_factory):
    server, reg, _ = mcp_factory(KITE_MCP_READ_ONLY="true")
    async with Client(server) as client:
        names = {t.name for t in (await client.list_tools()).tools}
    assert "get_orders" in names
    assert not {"place_order", "modify_order", "cancel_order"} & names
    assert reg.skipped["place_order"] == "read-only mode"


async def test_toolsets_and_exclusions(mcp_factory):
    server, reg, _ = mcp_factory(KITE_MCP_TOOLSETS="account,safety", KITE_MCP_EXCLUDED_TOOLS="get_margins")
    names = {info.name for info in reg.registered}
    assert "get_profile" in names and "get_margins" not in names and "place_order" not in names


async def test_order_reads_and_pagination(mcp_factory, fake_kite):
    orders = [
        {
            "order_id": str(i),
            "status": "OPEN" if i % 2 else "COMPLETE",
            "tradingsymbol": "INFY",
            "order_timestamp": f"2026-11-04 09:{i:02d}:00",
        }
        for i in range(1, 8)
    ]
    fake_kite.on("GET", "/orders", orders)
    server, _, _ = mcp_factory()
    page = tool_json(await call(server, "get_orders", {"status": "open", "limit": 2}))
    assert page["total"] == 4 and page["count"] == 2 and page["next_offset"] == 2
    assert [o["order_id"] for o in page["items"]] == ["7", "5"]


async def test_a_modification_that_raises_the_value_is_held_to_the_daily_cap(mcp_factory, fake_kite):
    kite_with_margins(fake_kite)
    fake_kite.on(
        "GET",
        "/orders/333",
        [
            {
                "order_id": "333",
                "status": "OPEN",
                "exchange": "NSE",
                "tradingsymbol": "INFY",
                "transaction_type": "BUY",
                "order_type": "LIMIT",
                "product": "CNC",
                "quantity": 2,
                "price": 1500.0,
            }
        ],
    )
    fake_kite.on("PUT", "/orders/regular/333", {"order_id": "333"})
    server, _, rt = mcp_factory(**LIVE, KITE_MCP_MAX_DAILY_VALUE="2000")
    bigger = tool_json(await call(server, "modify_order", {"order_id": "333", "quantity": 4}))  # +3000 > 2000
    assert any("daily cap" in v for v in bigger["checks"]["violations"])
    smaller = tool_json(await call(server, "modify_order", {"order_id": "333", "quantity": 3}))  # +1500 <= 2000
    assert smaller["checks"]["ok"]
    tool_json(
        await call(
            server,
            "modify_order",
            {"order_id": "333", "quantity": 3, "dry_run": False, "confirmation_token": smaller["confirmation_token"]},
        )
    )
    usage = rt.audit.usage()
    assert usage.orders == 0  # a modification is not a new order
    assert usage.value == 1500


async def test_an_unknown_outcome_counts_toward_the_caps(mcp_factory, fake_kite):
    kite_with_margins(fake_kite)

    def timeout(request):
        raise httpx2.ReadTimeout("slow", request=request)

    fake_kite.on("POST", "/orders/regular", handler=timeout)
    server, _, rt = mcp_factory(**LIVE)
    token = tool_json(await call(server, "place_order", LIMIT_BUY))["confirmation_token"]
    await call(server, "place_order", {**LIMIT_BUY, "dry_run": False, "confirmation_token": token})
    usage = rt.audit.usage()
    assert usage.orders == 1 and usage.value == 3000


async def test_cancelling_is_not_blocked_by_order_type_or_side_allow_lists(mcp_factory, fake_kite):
    fake_kite.on(
        "GET",
        "/orders/444",
        [
            {
                "order_id": "444",
                "status": "TRIGGER PENDING",
                "exchange": "NSE",
                "tradingsymbol": "INFY",
                "transaction_type": "SELL",
                "order_type": "SL-M",
                "product": "CNC",
                "quantity": 2,
                "trigger_price": 1400.0,
            }
        ],
    )
    server, _, _ = mcp_factory(
        **LIVE,
        KITE_MCP_ALLOWED_ORDER_TYPES="LIMIT",
        KITE_MCP_ALLOWED_TRANSACTION_TYPES="BUY",
        KITE_MCP_ALLOWED_EXCHANGES="BSE",
    )
    preview = tool_json(await call(server, "cancel_order", {"order_id": "444"}))
    violations = preview["checks"]["violations"]
    assert violations == ["exchange NSE is not in the allow-list (BSE)"]  # the exchange list still applies
