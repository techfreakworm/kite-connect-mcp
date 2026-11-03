"""Portfolio tools: holdings and positions summaries, auctions, position conversion, holdings authorisation."""

from __future__ import annotations

from typing import Any

import pytest
from mcp import Client

from conftest import API_KEY, tool_json, tool_text

LIVE = {"KITE_MCP_TRADING_MODE": "live"}


def holding(symbol: str, quantity: int, avg: float, last: float, close: float, pnl: float, change: float, **more: Any):
    return {
        "tradingsymbol": symbol,
        "exchange": "NSE",
        "instrument_token": 408065,
        "isin": more.pop("isin", "INE000000000"),
        "product": "CNC",
        "price": 0,
        "quantity": quantity,
        "t1_quantity": more.pop("t1_quantity", 0),
        "used_quantity": 0,
        "average_price": avg,
        "last_price": last,
        "close_price": close,
        "pnl": pnl,
        "day_change": change,
        "day_change_percentage": 0.5,
        "discrepancy": more.pop("discrepancy", False),
        **more,
    }


# worked by hand: invested 4000 + 7500 + 1000, current 4100 + 7400 + 1100, day change 50 - 50 + 20
HOLDINGS = [
    holding("ITC", 10, 400, 410, 405, 100, 5, isin="INE154A01025"),
    holding("INFY", 3, 1500, 1480, 1490, -100, -10, isin="INE009A01021", t1_quantity=2),  # 3 settled + 2 on T+1
    holding("GOLDBEES", 20, 50, 55, 54, 100, 1, isin="INF204KB17I5", discrepancy=True),
]
# the two holdings of the docs' sample, with its float noise
DOCS_HOLDINGS = [
    holding("AARON", 1, 161, 352.95, 352.35, 191.95, 0.5999999999999659),
    holding("SBIN", 16, 801.78125, 762.45, 766.4, -629.2999999999993, -3.949999999999932, exchange="BSE"),
]


def position(symbol: str, exchange: str, product: str, quantity: int, last: float, multiplier: int = 1, **more: Any):
    return {
        "tradingsymbol": symbol,
        "exchange": exchange,
        "instrument_token": 53496327,
        "product": product,
        "quantity": quantity,
        "overnight_quantity": more.pop("overnight_quantity", 0),
        "multiplier": multiplier,
        "average_price": last,
        "last_price": last,
        "value": -quantity * last * multiplier,
        "pnl": more.pop("pnl", 0),
        "m2m": more.pop("m2m", 0),
        "unrealised": more.pop("unrealised", 0),
        "realised": more.pop("realised", 0),
    }


POSITIONS = {
    "net": [
        position("LEADMINI17DECFUT", "MCX", "NRML", 1, 161.05, multiplier=1000),
        position("INFY", "NSE", "NRML", 5, 1510.0, overnight_quantity=5, pnl=50, m2m=50, unrealised=50),
        position("INFY", "NSE", "MIS", -2, 1510.0, pnl=-4, m2m=-4, unrealised=-4),
        position("SBIN", "NSE", "CO", 0, 308.4, pnl=-2, m2m=-2, unrealised=-2),
    ],
    "day": [position("GOLDGUINEA17DECFUT", "MCX", "NRML", -3, 23355.0, pnl=-93, m2m=-93, unrealised=-93)],
}
CONVERT: dict[str, Any] = {
    "exchange": "NSE",
    "tradingsymbol": "INFY",
    "transaction_type": "BUY",
    "position_type": "overnight",
    "quantity": 3,
    "old_product": "NRML",
    "new_product": "MIS",
}
CONVERT_FORM = {
    "tradingsymbol": "INFY",
    "exchange": "NSE",
    "transaction_type": "BUY",
    "position_type": "overnight",
    "quantity": "3",
    "old_product": "NRML",
    "new_product": "MIS",
}
REQUEST_ID = "fakerequestid0000000000000001"
TWO_ISINS = [{"isin": "INE009A01021", "quantity": 5}, {"isin": "INE154A01025", "quantity": 10}]


async def call(server, name, args):
    async with Client(server) as client:
        return await client.call_tool(name, args)


async def go_live(server, name, args):
    preview = tool_json(await call(server, name, args))
    return tool_json(
        await call(server, name, {**args, "dry_run": False, "confirmation_token": preview["confirmation_token"]})
    )


# --- holdings -------------------------------------------------------------------------------------------------------


async def test_get_holdings_summarises_every_matching_holding_not_just_the_page(mcp_factory, fake_kite):
    fake_kite.on("GET", "/portfolio/holdings", HOLDINGS)
    server, _, _ = mcp_factory()
    page = tool_json(await call(server, "get_holdings", {"limit": 2}))
    assert [h["tradingsymbol"] for h in page["items"]] == ["GOLDBEES", "INFY"]  # sorted by tradingsymbol
    assert page["total"] == 3 and page["count"] == 2 and page["next_offset"] == 2
    assert page["summary"] == {
        "holdings": 3,
        "invested": 12500.0,
        "current_value": 12600.0,
        "pnl": 100.0,
        "pnl_percentage": 0.8,
        "day_change": 20.0,
        "day_change_percentage": 0.16,
        "with_price_discrepancy": 1,
    }
    rest = tool_json(await call(server, "get_holdings", {"limit": 2, "offset": 2}))
    assert [h["tradingsymbol"] for h in rest["items"]] == ["ITC"] and "next_offset" not in rest
    assert rest["summary"] == page["summary"]
    assert fake_kite.requests[0].url.path == "/portfolio/holdings" and fake_kite.requests[0].url.query == b""


async def test_the_shares_of_a_holding_include_the_t1_quantity(mcp_factory, fake_kite):
    fake_kite.on("GET", "/portfolio/holdings", HOLDINGS)
    server, _, _ = mcp_factory()
    answer = tool_json(await call(server, "get_holdings", {"tradingsymbol": " infy "}))
    assert answer["total"] == 1 and answer["items"][0]["tradingsymbol"] == "INFY"
    # 3 settled + 2 on T+1 = 5 shares: 5 x 1500 invested, 5 x 1480 now, 5 x (-10) against the previous close
    assert answer["summary"] == {
        "holdings": 1,
        "invested": 7500.0,
        "current_value": 7400.0,
        "pnl": -100.0,
        "pnl_percentage": -1.33,
        "day_change": -50.0,
        "day_change_percentage": -0.67,
        "with_price_discrepancy": 0,
    }
    none = tool_json(await call(server, "get_holdings", {"tradingsymbol": "NOSUCH"}))
    assert none["total"] == 0 and none["items"] == [] and none["summary"]["invested"] == 0.0
    assert none["summary"]["pnl_percentage"] is None and none["summary"]["day_change_percentage"] is None


async def test_holdings_summary_matches_the_docs_sample(mcp_factory, fake_kite):
    fake_kite.on("GET", "/portfolio/holdings", DOCS_HOLDINGS)
    server, _, _ = mcp_factory()
    summary = tool_json(await call(server, "get_holdings", {}))["summary"]
    assert summary["invested"] == 12989.5 and summary["current_value"] == 12552.15
    assert summary["pnl"] == -437.35 and summary["pnl_percentage"] == -3.37
    assert summary["day_change"] == -62.6 and summary["day_change_percentage"] == -0.5


async def test_holdings_summary_tolerates_nulls_and_junk(mcp_factory, fake_kite):
    broken = holding("ITC", 10, 400, 410, 405, 100, 5)
    broken.update(last_price=None, pnl=None, day_change="n/a", close_price=None)
    fake_kite.on("GET", "/portfolio/holdings", [broken, "not a holding"])
    server, _, _ = mcp_factory()
    summary = tool_json(await call(server, "get_holdings", {}))["summary"]
    assert summary["holdings"] == 1 and summary["invested"] == 4000.0 and summary["current_value"] == 0.0
    assert summary["day_change"] == 0.0 and summary["day_change_percentage"] is None


async def test_holdings_pagination_limits_are_enforced(mcp_factory, fake_kite):
    fake_kite.on("GET", "/portfolio/holdings", HOLDINGS)
    server, _, _ = mcp_factory()
    for bad in ({"limit": 0}, {"limit": 501}, {"offset": -1}):
        assert (await call(server, "get_holdings", bad)).is_error, bad
    assert fake_kite.requests == []


# --- positions and auctions -----------------------------------------------------------------------------------------


async def test_get_positions_returns_the_requested_books_with_a_summary(mcp_factory, fake_kite):
    fake_kite.on("GET", "/portfolio/positions", POSITIONS)
    server, _, _ = mcp_factory()
    both = tool_json(await call(server, "get_positions", {}))
    assert set(both) == {"summary", "net", "day"} and len(both["net"]) == 4 and len(both["day"]) == 1
    assert both["summary"]["net"] == {
        "positions": 4,
        "open": 3,
        "pnl": 44.0,
        "m2m": 44.0,
        "unrealised": 44.0,
        "realised": 0.0,
    }
    assert both["summary"]["day"] == {
        "positions": 1,
        "open": 1,
        "pnl": -93.0,
        "m2m": -93.0,
        "unrealised": -93.0,
        "realised": 0.0,
    }

    net = tool_json(await call(server, "get_positions", {"kind": "net"}))
    assert set(net) == {"summary", "net"} and set(net["summary"]) == {"net"}
    day = tool_json(await call(server, "get_positions", {"kind": "day"}))
    assert set(day) == {"summary", "day"} and day["day"][0]["tradingsymbol"] == "GOLDGUINEA17DECFUT"
    assert (await call(server, "get_positions", {"kind": "weekly"})).is_error
    assert len(fake_kite.requests) == 3


async def test_get_positions_with_nothing_open(mcp_factory, fake_kite):
    fake_kite.on("GET", "/portfolio/positions", {"net": [], "day": []})
    server, _, _ = mcp_factory()
    answer = tool_json(await call(server, "get_positions", {}))
    assert answer["net"] == [] and answer["summary"]["net"]["positions"] == 0 and answer["summary"]["net"]["pnl"] == 0.0


async def test_get_auctions_lists_the_holdings_in_auction(mcp_factory, fake_kite):
    rows = [
        {"tradingsymbol": "ASHOKLEY", "exchange": "NSE", "isin": "INE208A01029", "quantity": 1, "auction_number": "20"},
        {"tradingsymbol": "BHEL", "exchange": "NSE", "isin": "INE257A01026", "quantity": 5, "auction_number": "34"},
    ]
    fake_kite.on("GET", "/portfolio/holdings/auctions", rows)
    server, _, _ = mcp_factory()
    answer = tool_json(await call(server, "get_auctions", {}))
    assert answer["count"] == 2 and [a["auction_number"] for a in answer["items"]] == ["20", "34"]
    fake_kite.on("GET", "/portfolio/holdings/auctions", [])
    assert tool_json(await call(server, "get_auctions", {})) == {"count": 0, "items": []}


async def test_read_errors_are_mapped(mcp_factory, fake_kite):
    fake_kite.on("GET", "/portfolio/positions", status=400, error_type="InputException", message="bad request")
    fake_kite.on("GET", "/portfolio/holdings", status=403, error_type="TokenException", message="Incorrect api_key")
    server, _, rt = mcp_factory()
    bad = await call(server, "get_positions", {})
    assert bad.is_error and "InputException" in tool_text(bad) and "bad request" in tool_text(bad)
    assert rt.tokens.current() is not None
    dead = await call(server, "get_holdings", {})
    assert dead.is_error and "rejected the session" in tool_text(dead) and "login" in tool_text(dead)
    assert rt.tokens.current() is None


# --- convert_position -----------------------------------------------------------------------------------------------


def convert_kite(fake):
    fake.on("GET", "/portfolio/positions", POSITIONS)
    fake.on("PUT", "/portfolio/positions", True)
    return fake


async def test_convert_position_dry_run_shows_the_request_and_the_position(mcp_factory, fake_kite):
    convert_kite(fake_kite)
    server, _, rt = mcp_factory()
    answer = tool_json(await call(server, "convert_position", {**CONVERT, "tradingsymbol": "infy"}))
    assert answer["dry_run"] is True and answer["sent"] is False
    assert answer["would_send"] == {"method": "PUT", "path": "/portfolio/positions", "form": CONVERT_FORM}
    assert answer["checks"]["ok"] is True
    [leg] = answer["legs"]
    assert leg["instrument"] == "NSE:INFY" and leg["product"] == "MIS" and leg["quantity"] == 3
    assert leg["value_inr"] == "4530.00" and "multiplier" in leg["value_note"]  # 3 x 1 x 1510
    assert answer["position"]["product"] == "NRML" and answer["position"]["quantity"] == 5
    assert "confirmation_token" not in answer
    assert fake_kite.writes() == [] and [e["event"] for e in rt.audit.read()] == ["dry_run"]


async def test_convert_position_live_sends_the_documented_form_once(mcp_factory, fake_kite):
    convert_kite(fake_kite)
    server, _, rt = mcp_factory(**LIVE)
    preview = tool_json(await call(server, "convert_position", CONVERT))
    refused = await call(server, "convert_position", {**CONVERT, "dry_run": False})
    assert refused.is_error and "confirmation_token is required" in tool_text(refused)
    assert fake_kite.writes() == []

    args = {**CONVERT, "dry_run": False, "confirmation_token": preview["confirmation_token"]}
    sent = tool_json(await call(server, "convert_position", args))
    assert sent["sent"] is True and sent["response"] is True
    [request] = fake_kite.writes()
    assert request.method == "PUT" and request.url.path == "/portfolio/positions"
    assert request.headers["content-type"] == "application/x-www-form-urlencoded"
    assert fake_kite.form(request) == list(CONVERT_FORM.items())  # the documented fields, in the documented order
    replay = await call(server, "convert_position", args)
    assert replay.is_error and "already used" in tool_text(replay)
    assert len(fake_kite.writes()) == 1
    usage = rt.audit.usage()
    assert usage.orders == 0 and usage.value == 0  # a conversion is no order and adds no exposure
    result = next(e for e in rt.audit.read() if e["event"] == "live_result")
    assert result["counts"] is False and result["value_inr"] == "0"  # a conversion adds nothing to the caps
    assert result["notional_inr"] == "4530.00"  # but its notional is on record


async def test_conversion_is_valued_with_the_positions_multiplier_and_held_to_the_per_order_cap(mcp_factory, fake_kite):
    convert_kite(fake_kite)
    mcx = {**CONVERT, "exchange": "MCX", "tradingsymbol": "LEADMINI17DECFUT", "quantity": 1}
    server, _, _ = mcp_factory(**LIVE)
    answer = tool_json(await call(server, "convert_position", mcx))
    assert answer["legs"][0]["value_inr"] == "161050.00"  # 1 lot x 1000 x 161.05
    assert answer["checks"]["ok"] is False and "per-order cap" in answer["checks"]["violations"][0]
    assert "confirmation_token" not in answer
    refused = await call(server, "convert_position", {**mcx, "dry_run": False})
    assert refused.is_error and "per-order cap" in tool_text(refused)

    roomy, _, _ = mcp_factory(**LIVE, KITE_MCP_MAX_ORDER_VALUE="200000")
    assert tool_json(await call(roomy, "convert_position", mcx))["checks"]["ok"] is True
    assert fake_kite.writes() == []


async def test_a_conversion_counts_toward_no_daily_cap(mcp_factory, fake_kite):
    convert_kite(fake_kite)
    server, _, rt = mcp_factory(**LIVE, KITE_MCP_MAX_ORDERS_PER_DAY="0", KITE_MCP_MAX_DAILY_VALUE="1")
    sent = await go_live(server, "convert_position", CONVERT)
    assert sent["sent"] is True and rt.audit.usage().orders == 0


async def test_a_position_that_cannot_be_found_cannot_be_converted_live(mcp_factory, fake_kite):
    fake_kite.on("PUT", "/portfolio/positions", True)
    fake_kite.on("GET", "/portfolio/positions", {"net": [], "day": []})
    server, _, _ = mcp_factory(**LIVE)
    answer = tool_json(await call(server, "convert_position", CONVERT))
    assert answer["checks"]["ok"] is False and "position" not in answer
    assert "cannot be verified" in answer["checks"]["violations"][0] and "not found" in answer["legs"][0]["value_note"]
    assert any("no NRML position on this instrument" in w for w in answer["checks"]["warnings"])

    fake_kite.on("GET", "/portfolio/positions", status=400, error_type="InputException", message="oops")
    unreadable = tool_json(await call(server, "convert_position", CONVERT))
    assert unreadable["checks"]["ok"] is False
    refused = await call(server, "convert_position", {**CONVERT, "dry_run": False})
    assert refused.is_error and fake_kite.writes() == []


async def test_a_position_without_a_price_cannot_be_valued(mcp_factory, fake_kite):
    fake_kite.on("GET", "/portfolio/positions", {"net": [position("INFY", "NSE", "NRML", 5, 0.0)], "day": []})
    server, _, _ = mcp_factory(**LIVE)
    answer = tool_json(await call(server, "convert_position", CONVERT))
    assert answer["checks"]["ok"] is False and "no last_price" in answer["legs"][0]["value_note"]


async def test_the_allow_lists_judge_the_product_the_position_ends_up_in(mcp_factory, fake_kite):
    convert_kite(fake_kite)
    server, _, _ = mcp_factory(**LIVE, KITE_MCP_ALLOWED_PRODUCTS="CNC,NRML", KITE_MCP_ALLOWED_TRANSACTION_TYPES="SELL")
    violations = " ".join(tool_json(await call(server, "convert_position", CONVERT))["checks"]["violations"])
    assert "product MIS" in violations and "transaction_type BUY" in violations
    # converting INTO an allowed product is fine, whatever it was before
    back = {**CONVERT, "transaction_type": "SELL", "old_product": "MIS", "new_product": "NRML", "position_type": "day"}
    allowed = tool_json(await call(server, "convert_position", {**back, "quantity": 2}))
    assert allowed["checks"]["ok"] is True


async def test_a_conversion_in_allowlist_mode_follows_the_allowed_instruments(mcp_factory, fake_kite):
    convert_kite(fake_kite)
    server, _, _ = mcp_factory(**LIVE, KITE_MCP_LIVE_CONFIRMATION="allowlist", KITE_MCP_ALLOWED_INSTRUMENTS="NSE:INFY")
    assert tool_json(await call(server, "convert_position", {**CONVERT, "dry_run": False}))["sent"] is True
    other = {**CONVERT, "tradingsymbol": "SBIN", "dry_run": False}
    refused = await call(server, "convert_position", other)
    assert refused.is_error and "instrument NSE:SBIN is not in KITE_MCP_ALLOWED_INSTRUMENTS" in tool_text(refused)
    assert len(fake_kite.writes()) == 1


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"new_product": "NRML"}, "nothing to convert"),
        ({"tradingsymbol": "  "}, "tradingsymbol must not be blank"),
        (
            {"exchange": "NFO", "tradingsymbol": "NIFTY25NOVFUT", "old_product": "MIS", "new_product": "CNC"},
            "CNC is for equity",
        ),
        (
            {"exchange": "MCX", "tradingsymbol": "GOLDM25DECFUT", "old_product": "MIS", "new_product": "CNC"},
            "CNC is for equity",
        ),
    ],
)
async def test_impossible_conversions_are_rejected_before_anything(mcp_factory, fake_kite, changes, message):
    convert_kite(fake_kite)
    server, _, rt = mcp_factory()
    result = await call(server, "convert_position", {**CONVERT, **changes})
    assert result.is_error and message in tool_text(result)
    assert fake_kite.requests == [] and rt.audit.read() == []


@pytest.mark.parametrize(
    "changes",
    [
        {"position_type": "weekly"},
        {"transaction_type": "HOLD"},
        {"quantity": 0},
        {"quantity": 1.5},
        {"old_product": "BO"},
        {"new_product": "cnc"},
        {"exchange": "INDICES"},
        {"tradingsymbol": ""},
    ],
)
async def test_the_schema_refuses_malformed_conversions(mcp_factory, fake_kite, changes):
    convert_kite(fake_kite)
    server, _, rt = mcp_factory()
    assert (await call(server, "convert_position", {**CONVERT, **changes})).is_error
    assert fake_kite.requests == [] and rt.audit.read() == []


async def test_conversion_warnings(mcp_factory, fake_kite):
    convert_kite(fake_kite)
    server, _, _ = mcp_factory()

    def warnings(answer):
        return " ".join(answer["checks"]["warnings"])

    out_of_mis = {**CONVERT, "transaction_type": "SELL", "old_product": "MIS", "new_product": "NRML", "quantity": 2}
    text = warnings(tool_json(await call(server, "convert_position", out_of_mis)))
    assert "squared off by the broker at the end of the day" in text and "carried overnight" in text
    assert "should be" not in text  # the short MIS position is SELL, as given

    wrong_side = warnings(tool_json(await call(server, "convert_position", {**CONVERT, "transaction_type": "SELL"})))
    assert "the position is long: transaction_type should be BUY" in wrong_side
    too_many = warnings(tool_json(await call(server, "convert_position", {**CONVERT, "quantity": 9})))
    assert "quantity 9 is more than the position's quantity 5" in too_many
    day_only = {**CONVERT, "old_product": "MIS", "new_product": "NRML", "transaction_type": "SELL", "quantity": 1}
    overnight = warnings(tool_json(await call(server, "convert_position", {**day_only, "position_type": "overnight"})))
    assert "more than the overnight quantity 0" in overnight
    assert "overnight quantity" not in warnings(
        tool_json(await call(server, "convert_position", {**day_only, "position_type": "day"}))
    )


# --- authorise_holdings ---------------------------------------------------------------------------------------------


def authorise_kite(fake):
    fake.on("GET", "/portfolio/holdings", HOLDINGS)
    fake.on("POST", "/portfolio/holdings/authorise", {"request_id": REQUEST_ID})
    return fake


async def test_authorise_sends_repeated_isin_quantity_pairs_and_returns_the_link(mcp_factory, fake_kite):
    authorise_kite(fake_kite)
    server, _, rt = mcp_factory(**LIVE)
    args = {"holdings": [{"isin": "ine009a01021", "quantity": 5}, TWO_ISINS[1]]}
    preview = tool_json(await call(server, "authorise_holdings", args))
    assert preview["would_send"]["form"] == [
        ["isin", "INE009A01021"],
        ["quantity", "5"],
        ["isin", "INE154A01025"],
        ["quantity", "10"],
    ]
    assert preview["checks"]["ok"] is True and preview["checks"]["warnings"] == []
    assert "authorise_url" not in preview  # there is no request_id before Kite has answered
    assert fake_kite.writes() == []

    refused = await call(server, "authorise_holdings", {**args, "dry_run": False})
    assert refused.is_error and "confirmation_token is required" in tool_text(refused)
    assert fake_kite.writes() == []

    sent = tool_json(
        await call(
            server,
            "authorise_holdings",
            {**args, "dry_run": False, "confirmation_token": preview["confirmation_token"]},
        )
    )
    assert sent["sent"] is True and sent["response"] == {"request_id": REQUEST_ID}
    assert (
        sent["authorise_url"] == f"https://kite.zerodha.com/connect/portfolio/authorise/holdings/{API_KEY}/{REQUEST_ID}"
    )
    assert "TPIN" in sent["next_step"] and "428" in sent["next_step"] and "5:30 PM" in sent["next_step"]
    [request] = fake_kite.writes()
    assert request.method == "POST" and request.url.path == "/portfolio/holdings/authorise"
    assert request.headers["content-type"] == "application/x-www-form-urlencoded"
    assert fake_kite.form(request) == [
        ("isin", "INE009A01021"),
        ("quantity", "5"),
        ("isin", "INE154A01025"),
        ("quantity", "10"),
    ]
    assert request.content == b"isin=INE009A01021&quantity=5&isin=INE154A01025&quantity=10"
    usage = rt.audit.usage()
    assert usage.orders == 0 and usage.value == 0  # not an order


async def test_authorise_without_holdings_asks_for_the_entire_holdings(mcp_factory, fake_kite):
    authorise_kite(fake_kite)
    server, _, _ = mcp_factory(**LIVE)
    preview = tool_json(await call(server, "authorise_holdings", {}))
    assert preview["summary"] == "authorise the entire holdings for sale at CDSL" and preview["legs"] == []
    sent = tool_json(
        await call(
            server, "authorise_holdings", {"dry_run": False, "confirmation_token": preview["confirmation_token"]}
        )
    )
    assert sent["authorise_url"].endswith(f"/{API_KEY}/{REQUEST_ID}")
    [request] = fake_kite.writes()
    assert request.content == b"" and fake_kite.form(request) == []
    assert fake_kite.calls("GET", "/portfolio/holdings") == []  # nothing to cross-check without a list of ISINs


@pytest.mark.parametrize(
    ("response", "expected"),
    [({}, None), (None, None), ({"request_id": ""}, None), ({"request_id": "a/b c?d"}, "a%2Fb%20c%3Fd")],
)
async def test_the_link_needs_a_request_id_and_is_url_safe(mcp_factory, fake_kite, response, expected):
    fake_kite.on("POST", "/portfolio/holdings/authorise", response)
    server, _, _ = mcp_factory(**LIVE)
    sent = await go_live(server, "authorise_holdings", {})
    if expected is None:
        assert "authorise_url" not in sent and "no request_id" in sent["note"]
    else:
        assert sent["authorise_url"].endswith(f"/{API_KEY}/{expected}")


async def test_authorise_dry_run_explains_the_cdsl_flow_and_checks_the_isins(mcp_factory, fake_kite):
    authorise_kite(fake_kite)
    server, _, _ = mcp_factory()
    holdings = [{"isin": "INE009A01021", "quantity": 6}, {"isin": "INE999Z01010", "quantity": 1}]
    answer = tool_json(await call(server, "authorise_holdings", {"holdings": holdings}))
    explanation = answer["how_it_works"]
    for expected in ("428", "TPIN", "CDSL", "5:30 PM", "never to this server"):
        assert expected in explanation
    assert answer["checks"]["warnings"] == [
        "INE009A01021: authorising 6 but you hold 5",
        "INE999Z01010 is not in your holdings",
    ]
    assert answer["checks"]["ok"] is True  # advice only: Kite shows its own error on the page

    fake_kite.routes.pop(("GET", "/portfolio/holdings"))
    unreadable = tool_json(await call(server, "authorise_holdings", {"holdings": holdings}))
    assert unreadable["checks"]["warnings"] == [
        "the holdings could not be read: the ISINs and quantities were not checked"
    ]


@pytest.mark.parametrize(
    "holdings",
    [
        [{"isin": "INE009A01021", "quantity": 0}],
        [{"isin": "INE009A01021", "quantity": -3}],
        [{"isin": "NOTANISIN", "quantity": 1}],
        [{"isin": "INE009A0102", "quantity": 1}],
        [{"isin": "INE009A0102X", "quantity": 1}],
        [{"isin": "INE009A01021"}],
        [{"quantity": 1}],
        [{"isin": "INE009A01021", "quantity": 1, "exchange": "NSE"}],
        [],
        [{"isin": "INE009A01021", "quantity": 1}, {"isin": "ine009a01021", "quantity": 2}],
    ],
)
async def test_bad_holdings_are_rejected_before_anything(mcp_factory, fake_kite, holdings):
    authorise_kite(fake_kite)
    server, _, rt = mcp_factory()
    assert (await call(server, "authorise_holdings", {"holdings": holdings})).is_error
    assert fake_kite.requests == [] and rt.audit.read() == []


async def test_authorise_is_a_confirmed_write_but_not_an_order(mcp_factory, fake_kite):
    authorise_kite(fake_kite)
    server, _, rt = mcp_factory(
        **LIVE,
        KITE_MCP_MAX_ORDER_VALUE="1",
        KITE_MCP_MAX_ORDERS_PER_DAY="0",
        KITE_MCP_ALLOWED_PRODUCTS="MIS",
        KITE_MCP_LIVE_CONFIRMATION="allowlist",
        KITE_MCP_ALLOWED_INSTRUMENTS="NSE:INFY",
    )
    preview = tool_json(await call(server, "authorise_holdings", {"holdings": TWO_ISINS}))
    assert preview["checks"]["ok"] is True and "confirmation_token" in preview  # no legs: allowlist mode cannot skip it
    refused = await call(server, "authorise_holdings", {"holdings": TWO_ISINS, "dry_run": False})
    assert refused.is_error and "confirmation_token is required" in tool_text(refused)
    assert fake_kite.writes() == [] and rt.audit.usage().orders == 0


async def test_authorise_is_refused_live_while_trading_mode_is_dry_run(mcp_factory, fake_kite):
    authorise_kite(fake_kite)
    server, _, _ = mcp_factory()
    for name, args in (("authorise_holdings", {}), ("convert_position", CONVERT)):
        result = await call(server, name, {**args, "dry_run": False})
        assert result.is_error and "KITE_MCP_TRADING_MODE=dry_run" in tool_text(result), name
    assert fake_kite.writes() == []


async def test_a_rejected_authorisation_is_reported_without_a_link(mcp_factory, fake_kite):
    authorise_kite(fake_kite).on(
        "POST", "/portfolio/holdings/authorise", status=400, error_type="InputException", message="invalid isin"
    )
    server, _, rt = mcp_factory(**LIVE)
    preview = tool_json(await call(server, "authorise_holdings", {}))
    result = await call(
        server, "authorise_holdings", {"dry_run": False, "confirmation_token": preview["confirmation_token"]}
    )
    assert result.is_error and "invalid isin" in tool_text(result) and "authorise_url" not in tool_text(result)
    assert [e["event"] for e in rt.audit.read()][-2:] == ["live_intent", "live_error"]


async def test_the_description_explains_the_428_and_the_tpin_flow(mcp_factory):
    _, reg, _ = mcp_factory()
    description = " ".join({i.name: i.description for i in reg.registered}["authorise_holdings"].split())
    for expected in ("428", "TPIN", "CDSL", "authorise_url", "DRY RUN BY DEFAULT", "5:30 PM"):
        assert expected in description


# --- exposure -------------------------------------------------------------------------------------------------------


async def test_read_only_hides_the_portfolio_write_tools(mcp_factory):
    server, reg, _ = mcp_factory(KITE_MCP_READ_ONLY="true")
    async with Client(server) as client:
        names = {t.name for t in (await client.list_tools()).tools}
    assert {"get_holdings", "get_positions", "get_auctions"} <= names
    assert not {"convert_position", "authorise_holdings"} & names
    assert reg.skipped["convert_position"] == "read-only mode" and reg.skipped["authorise_holdings"] == "read-only mode"


async def test_the_portfolio_toolset_can_be_switched_off(mcp_factory):
    _, reg, _ = mcp_factory(KITE_MCP_TOOLSETS="orders,safety")
    assert not [i for i in reg.registered if i.toolset == "portfolio"]
    _, reg, _ = mcp_factory(KITE_MCP_EXCLUDED_TOOLS="authorise_holdings")
    assert "authorise_holdings" in reg.skipped and "convert_position" not in reg.skipped


async def test_write_tools_say_dry_run_by_default(mcp_factory):
    server, reg, _ = mcp_factory()
    by_name = {info.name: info for info in reg.registered}
    for name in ("convert_position", "authorise_holdings"):
        assert by_name[name].kind == "write" and "DRY RUN BY DEFAULT" in " ".join(by_name[name].description.split())
    async with Client(server) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert tools["convert_position"].input_schema["properties"]["dry_run"]["default"] is True
    assert tools["authorise_holdings"].input_schema.get("required", []) == []  # every argument is optional
