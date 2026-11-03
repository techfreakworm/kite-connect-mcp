"""The margin and charges calculators: exact request bodies and query strings, summaries, read-only availability,
local validation and error mapping. Everything runs against FakeKite; the figures are the examples of Kite's docs."""

from __future__ import annotations

import pytest
from mcp import Client

from conftest import tool_json, tool_text

CALCULATORS = ("get_order_margins", "get_basket_margins", "get_order_charges")

BUY_INFY = {
    "exchange": "NSE",
    "tradingsymbol": "INFY",
    "transaction_type": "BUY",
    "quantity": 1,
    "product": "CNC",
    "order_type": "MARKET",
}
BUY_TCS_LIMIT = {
    "exchange": "NSE",
    "tradingsymbol": "TCS",
    "transaction_type": "BUY",
    "quantity": 2,
    "product": "MIS",
    "order_type": "LIMIT",
    "price": 3200.5,
    "variety": "amo",
}
SELL_CE = {
    "exchange": "NFO",
    "tradingsymbol": "NIFTY23JUL20600CE",
    "transaction_type": "SELL",
    "quantity": 75,
    "product": "NRML",
    "order_type": "MARKET",
}
BUY_CE = {**SELL_CE, "tradingsymbol": "NIFTY23JUL20700CE", "transaction_type": "BUY"}


def charges(*, brokerage, tax, exchange, sebi, stamp, gst, total, tax_type="stt"):
    return {
        "transaction_tax": tax,
        "transaction_tax_type": tax_type,
        "exchange_turnover_charge": exchange,
        "sebi_turnover_charge": sebi,
        "brokerage": brokerage,
        "stamp_duty": stamp,
        "gst": {"igst": gst, "cgst": 0, "sgst": 0, "total": gst},
        "total": total,
    }


MARGIN_INFY = {
    "type": "equity",
    "tradingsymbol": "INFY",
    "exchange": "NSE",
    "span": 0,
    "exposure": 0,
    "option_premium": 0,
    "additional": 0,
    "bo": 0,
    "cash": 0,
    "var": 1498,
    "pnl": {"realised": 0, "unrealised": 0},
    "leverage": 1,
    "charges": charges(
        brokerage=0.01, tax=1.498, exchange=0.051681, sebi=0.001498, stamp=0.22, gst=0.01137222, total=1.79255122
    ),
    "total": 1498,
}
MARGIN_TCS = {
    "type": "equity",
    "tradingsymbol": "TCS",
    "exchange": "NSE",
    "var": 6401,
    "leverage": 5,
    "charges": charges(brokerage=20, tax=0, exchange=0.5, sebi=0.01, stamp=0, gst=3.69, total=24.2),
    "total": 3200,
}
BASKET = {
    "initial": {"type": "", "span": 66832.5, "exposure": 29151.225, "option_premium": 521.25, "total": 96504.975},
    "final": {
        "type": "",
        "span": 7788.0,
        "exposure": 29151.225,
        "option_premium": -2152.5,
        "total": 34786.725000000006,
    },
    "orders": [
        {
            "type": "equity",
            "tradingsymbol": "NIFTY23JUL20600CE",
            "exchange": "NFO",
            "total": 95983.725,
            "charges": charges(
                brokerage=20,
                tax=1.67109375,
                exchange=1.336875,
                sebi=0.00267375,
                stamp=0,
                gst=3.8411187749999995,
                total=26.851761274999998,
            ),
        },
        {
            "type": "equity",
            "tradingsymbol": "NIFTY23JUL20700CE",
            "exchange": "NFO",
            "total": 521.25,
            "charges": charges(
                brokerage=20,
                tax=0,
                exchange=0.260625,
                sebi=0.00052125,
                stamp=0,
                gst=3.6470063249999995,
                total=23.908152575,
            ),
        },
    ],
    "charges": charges(brokerage=40, tax=0, exchange=0, sebi=0.003195, stamp=0, gst=0, total=0, tax_type=""),
}
CHARGE_ORDERS = [
    {
        "order_id": "111111111",
        "exchange": "NSE",
        "tradingsymbol": "SBIN",
        "transaction_type": "BUY",
        "variety": "regular",
        "product": "CNC",
        "order_type": "MARKET",
        "quantity": 1,
        "average_price": 560,
    },
    {
        "exchange": "MCX",
        "tradingsymbol": "goldpetal23julfut ",
        "transaction_type": "SELL",
        "product": "NRML",
        "order_type": "LIMIT",
        "quantity": 1,
        "average_price": 5862,
    },
    {
        "order_id": "3333333333",
        "exchange": "NFO",
        "tradingsymbol": "NIFTY2371317900PE",
        "transaction_type": "BUY",
        "product": "NRML",
        "order_type": "LIMIT",
        "quantity": 100,
        "average_price": 1.5,
    },
]
CHARGES_ANSWER = [
    {
        "transaction_type": "BUY",
        "tradingsymbol": "SBIN",
        "exchange": "NSE",
        "variety": "regular",
        "product": "CNC",
        "order_type": "MARKET",
        "quantity": 1,
        "price": 560,
        "charges": charges(
            brokerage=0, tax=0.56, exchange=0.01876, sebi=0.00056, stamp=0, gst=0.0033767999999999997, total=0.5826968
        ),
    },
    {
        "transaction_type": "SELL",
        "tradingsymbol": "GOLDPETAL23JULFUT",
        "exchange": "MCX",
        "variety": "regular",
        "product": "NRML",
        "order_type": "LIMIT",
        "quantity": 1,
        "price": 5862,
        "charges": charges(
            brokerage=1.7586,
            tax=0.5862,
            exchange=0.152412,
            sebi=0.005862,
            stamp=0,
            gst=0.34503732,
            total=2.84811132,
            tax_type="ctt",
        ),
    },
    {
        "transaction_type": "BUY",
        "tradingsymbol": "NIFTY2371317900PE",
        "exchange": "NFO",
        "variety": "regular",
        "product": "NRML",
        "order_type": "LIMIT",
        "quantity": 100,
        "price": 1.5,
        "charges": charges(
            brokerage=20, tax=0, exchange=0.07575, sebi=0.00015, stamp=0, gst=3.613527, total=23.689427000000002
        ),
    },
]


async def call(server, name, args):
    async with Client(server) as client:
        return await client.call_tool(name, args)


# --- request bodies and summaries -----------------------------------------------------------------------------------


async def test_order_margins_send_the_documented_json_and_add_up_the_totals(mcp_factory, fake_kite):
    fake_kite.on("POST", "/margins/orders", [MARGIN_INFY, MARGIN_TCS])
    server, _, _ = mcp_factory()
    answer = tool_json(await call(server, "get_order_margins", {"orders": [BUY_INFY, {**BUY_TCS_LIMIT}]}))

    (request,) = fake_kite.calls("POST", "/margins/orders")
    assert request.url.query == b""
    assert request.headers["content-type"].startswith("application/json")
    assert fake_kite.json_body(request) == [
        {
            "exchange": "NSE",
            "tradingsymbol": "INFY",
            "transaction_type": "BUY",
            "variety": "regular",
            "product": "CNC",
            "order_type": "MARKET",
            "quantity": 1,
            "price": 0,
            "trigger_price": 0,
        },
        {
            "exchange": "NSE",
            "tradingsymbol": "TCS",
            "transaction_type": "BUY",
            "variety": "amo",
            "product": "MIS",
            "order_type": "LIMIT",
            "quantity": 2,
            "price": 3200.5,
            "trigger_price": 0,
        },
    ]
    assert answer["orders"] == [MARGIN_INFY, MARGIN_TCS]  # Kite's answer, untouched
    summary = answer["summary"]
    assert summary["orders"] == 2
    assert summary["sum_of_order_margins_inr"] == 4698.0
    assert summary["total_charges_inr"] == 25.99
    assert summary["charges_by_component_inr"] == {
        "brokerage": 20.01,
        "transaction_tax": 1.498,
        "exchange_turnover_charge": 0.5517,
        "sebi_turnover_charge": 0.0115,
        "stamp_duty": 0.22,
        "gst": 3.7014,
    }
    assert "get_basket_margins" in summary["note"]
    assert fake_kite.writes() == []


async def test_order_margins_compact_mode_is_a_query_parameter_and_has_no_charges_to_sum(mcp_factory, fake_kite):
    fake_kite.on(
        "POST", "/margins/orders", [{"type": "equity", "tradingsymbol": "INFY", "exchange": "NSE", "total": 1498}]
    )
    server, _, _ = mcp_factory()
    answer = tool_json(await call(server, "get_order_margins", {"orders": [BUY_INFY], "mode": "compact"}))
    (request,) = fake_kite.calls("POST", "/margins/orders")
    assert dict(request.url.params) == {"mode": "compact"}
    assert answer["summary"]["sum_of_order_margins_inr"] == 1498.0
    assert "total_charges_inr" not in answer["summary"]


async def test_order_margins_clean_up_the_symbol_and_accept_stop_orders(mcp_factory, fake_kite):
    fake_kite.on("POST", "/margins/orders", [])
    server, _, _ = mcp_factory()
    stop = {**BUY_INFY, "tradingsymbol": " infy ", "order_type": "SL-M", "trigger_price": 1450.5}
    tool_json(await call(server, "get_order_margins", {"orders": [stop]}))
    (body,) = fake_kite.json_body(fake_kite.calls("POST", "/margins/orders")[0])
    assert body["tradingsymbol"] == "INFY" and body["order_type"] == "SL-M"
    assert body["trigger_price"] == 1450.5 and body["price"] == 0


async def test_basket_margins_default_to_considering_positions_and_report_the_spread_benefit(mcp_factory, fake_kite):
    fake_kite.on("POST", "/margins/basket", BASKET)
    server, _, _ = mcp_factory()
    answer = tool_json(await call(server, "get_basket_margins", {"orders": [SELL_CE, BUY_CE]}))

    (request,) = fake_kite.calls("POST", "/margins/basket")
    assert dict(request.url.params) == {"consider_positions": "true"}
    body = fake_kite.json_body(request)
    assert [(o["tradingsymbol"], o["transaction_type"], o["quantity"]) for o in body] == [
        ("NIFTY23JUL20600CE", "SELL", 75),
        ("NIFTY23JUL20700CE", "BUY", 75),
    ]
    assert answer["initial"] == BASKET["initial"] and answer["final"] == BASKET["final"]  # Kite's keys stay
    assert answer["orders"] == BASKET["orders"]
    assert answer["summary"] == {
        "orders": 2,
        "initial_margin_inr": 96504.98,
        "final_margin_inr": 34786.73,
        "spread_benefit_inr": 61718.25,
        # summed from the per-order blocks: Kite's own overall block says total 0 here
        "total_charges_inr": 50.76,
        "charges_by_component_inr": {
            "brokerage": 40.0,
            "transaction_tax": 1.6711,
            "exchange_turnover_charge": 1.5975,
            "sebi_turnover_charge": 0.0032,
            "stamp_duty": 0.0,
            "gst": 7.4881,
        },
    }


async def test_basket_margins_query_carries_both_flags(mcp_factory, fake_kite):
    fake_kite.on("POST", "/margins/basket", {"initial": {"total": 10}, "final": {"total": 4}, "orders": []})
    server, _, _ = mcp_factory()
    answer = tool_json(
        await call(server, "get_basket_margins", {"orders": [BUY_INFY], "consider_positions": False, "mode": "compact"})
    )
    (request,) = fake_kite.calls("POST", "/margins/basket")
    assert request.url.query == b"consider_positions=false&mode=compact"
    assert answer["summary"] == {
        "orders": 0,
        "initial_margin_inr": 10.0,
        "final_margin_inr": 4.0,
        "spread_benefit_inr": 6.0,
    }


async def test_order_charges_send_the_documented_json_and_break_the_total_down(mcp_factory, fake_kite):
    fake_kite.on("POST", "/charges/orders", CHARGES_ANSWER)
    server, _, _ = mcp_factory()
    answer = tool_json(await call(server, "get_order_charges", {"orders": CHARGE_ORDERS}))

    (request,) = fake_kite.calls("POST", "/charges/orders")
    assert request.url.query == b""
    body = fake_kite.json_body(request)
    assert body[0] == {
        "order_id": "111111111",
        "exchange": "NSE",
        "tradingsymbol": "SBIN",
        "transaction_type": "BUY",
        "variety": "regular",
        "product": "CNC",
        "order_type": "MARKET",
        "quantity": 1,
        "average_price": 560,
    }
    assert body[1]["order_id"] == "order-2"  # numbered when the caller gives none
    assert body[1]["tradingsymbol"] == "GOLDPETAL23JULFUT" and body[1]["variety"] == "regular"
    assert [o["average_price"] for o in body] == [560, 5862, 1.5]
    assert answer["orders"] == CHARGES_ANSWER
    summary = answer["summary"]
    assert summary["orders"] == 3 and summary["total_charges_inr"] == 27.12
    assert summary["charges_by_component_inr"] == {
        "brokerage": 21.7586,
        "transaction_tax": 1.1462,
        "exchange_turnover_charge": 0.2469,
        "sebi_turnover_charge": 0.0066,
        "stamp_duty": 0.0,
        "gst": 3.9619,
    }
    assert summary["per_order"] == [
        {
            "order_id": "111111111",
            "instrument": "NSE:SBIN",
            "transaction_type": "BUY",
            "quantity": 1,
            "price": 560,
            "total_charges_inr": 0.58,
        },
        {
            "order_id": "order-2",
            "instrument": "MCX:GOLDPETAL23JULFUT",
            "transaction_type": "SELL",
            "quantity": 1,
            "price": 5862,
            "total_charges_inr": 2.85,
        },
        {
            "order_id": "3333333333",
            "instrument": "NFO:NIFTY2371317900PE",
            "transaction_type": "BUY",
            "quantity": 100,
            "price": 1.5,
            "total_charges_inr": 23.69,
        },
    ]


async def test_order_charges_leave_out_order_ids_when_the_answer_does_not_line_up(mcp_factory, fake_kite):
    fake_kite.on("POST", "/charges/orders", [CHARGES_ANSWER[2], CHARGES_ANSWER[0]])  # not the order we asked in
    server, _, _ = mcp_factory()
    answer = tool_json(await call(server, "get_order_charges", {"orders": CHARGE_ORDERS[:2]}))
    lines = answer["summary"]["per_order"]
    assert [line["instrument"] for line in lines] == ["NFO:NIFTY2371317900PE", "NSE:SBIN"]
    assert all("order_id" not in line for line in lines)


# --- read-only mode and tool selection ------------------------------------------------------------------------------


async def test_calculators_stay_available_in_read_only_mode(mcp_factory, fake_kite):
    fake_kite.on("POST", "/margins/orders", [MARGIN_INFY])
    server, reg, _ = mcp_factory(KITE_MCP_READ_ONLY="true")
    async with Client(server) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        assert set(CALCULATORS) <= set(tools)
        assert all(tools[name].annotations.read_only_hint is True for name in CALCULATORS)
        result = await client.call_tool("get_order_margins", {"orders": [BUY_INFY]})
    assert tool_json(result)["summary"]["sum_of_order_margins_inr"] == 1498.0
    assert {i.name: i.kind for i in reg.registered if i.toolset == "margins"} == dict.fromkeys(CALCULATORS, "calc")
    assert fake_kite.writes() == []


async def test_toolset_and_exclusions_hide_calculators(mcp_factory):
    _, reg, _ = mcp_factory(KITE_MCP_TOOLSETS="account")
    assert {reg.skipped[name] for name in CALCULATORS} == {"toolset margins is not in KITE_MCP_TOOLSETS"}
    _, reg, _ = mcp_factory(KITE_MCP_EXCLUDED_TOOLS="get_order_charges")
    assert reg.skipped["get_order_charges"] == "listed in KITE_MCP_EXCLUDED_TOOLS"
    assert "get_order_margins" not in reg.skipped and "get_basket_margins" not in reg.skipped


async def test_schemas_document_the_list_limits(mcp_factory):
    server, _, _ = mcp_factory()
    async with Client(server) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    for name in CALCULATORS:
        orders = tools[name].input_schema["properties"]["orders"]
        assert (orders["minItems"], orders["maxItems"]) == (1, 50)
        assert "1 to 50" in tools[name].description
    margin_item = tools["get_order_margins"].input_schema["$defs"]["MarginOrder"]
    assert margin_item["additionalProperties"] is False
    assert set(margin_item["required"]) == {
        "exchange",
        "tradingsymbol",
        "transaction_type",
        "quantity",
        "product",
        "order_type",
    }


# --- local validation: nothing is sent -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"order_type": "LIMIT"}, "LIMIT orders need a price above 0"),
        ({"order_type": "SL", "price": 100}, "SL orders need a trigger_price above 0"),
        ({"order_type": "SL-M"}, "SL-M orders need a trigger_price above 0"),
        ({"order_type": "MARKET", "price": 10}, "MARKET orders take no price"),
        (
            {"variety": "co", "order_type": "LIMIT", "price": 100, "product": "MIS"},
            "cover orders (co) need trigger_price",
        ),
        ({"exchange": "NFO", "tradingsymbol": "NIFTY25NOVFUT", "product": "CNC"}, "CNC is for equity delivery"),
        ({"validity": "DAY"}, "Extra inputs are not permitted"),
        ({"exchange": "XYZ"}, "Input should be"),
        ({"quantity": 0}, "greater than or equal to 1"),
        ({"price": -1, "order_type": "LIMIT"}, "greater than or equal to 0"),
    ],
)
@pytest.mark.parametrize("tool", ["get_order_margins", "get_basket_margins"])
async def test_margin_orders_that_cannot_be_calculated_are_rejected_locally(
    mcp_factory, fake_kite, tool, change, message
):
    server, _, _ = mcp_factory()
    result = await call(server, tool, {"orders": [{**BUY_INFY, **change}]})
    assert result.is_error and message in tool_text(result)
    assert fake_kite.requests == []


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"average_price": 0}, "greater than 0"),
        ({"exchange": "MCX", "tradingsymbol": "GOLDM25DECFUT", "product": "CNC"}, "CNC is for equity delivery"),
        ({"price": 5}, "Extra inputs are not permitted"),
    ],
)
async def test_charge_orders_that_cannot_be_calculated_are_rejected_locally(mcp_factory, fake_kite, change, message):
    server, _, _ = mcp_factory()
    result = await call(server, "get_order_charges", {"orders": [{**CHARGE_ORDERS[0], **change}]})
    assert result.is_error and message in tool_text(result)
    assert fake_kite.requests == []


async def test_every_problem_in_a_basket_is_reported_at_once(mcp_factory, fake_kite):
    server, _, _ = mcp_factory()
    bad = [{**BUY_INFY, "order_type": "LIMIT"}, BUY_INFY, {**BUY_INFY, "order_type": "SL"}]
    result = await call(server, "get_basket_margins", {"orders": bad})
    text = tool_text(result)
    assert result.is_error and "order 1 (BUY NSE:INFY): LIMIT orders need a price above 0" in text
    assert "order 3 (BUY NSE:INFY): SL orders need a price above 0" in text
    assert "order 3 (BUY NSE:INFY): SL orders need a trigger_price above 0" in text
    assert "order 2" not in text  # the valid order is not blamed
    assert fake_kite.requests == []


@pytest.mark.parametrize("tool", CALCULATORS)
async def test_the_list_must_hold_between_1_and_50_orders(mcp_factory, fake_kite, tool):
    server, _, _ = mcp_factory()
    valid = {**BUY_INFY, "average_price": 100} if tool == "get_order_charges" else BUY_INFY
    empty = await call(server, tool, {"orders": []})
    assert empty.is_error and "at least 1 item" in tool_text(empty)
    many = await call(server, tool, {"orders": [valid] * 51})
    assert many.is_error and "at most 50 items" in tool_text(many)
    assert fake_kite.requests == []


async def test_fifty_orders_are_accepted(mcp_factory, fake_kite):
    fake_kite.on("POST", "/margins/orders", [])
    server, _, _ = mcp_factory()
    tool_json(await call(server, "get_order_margins", {"orders": [BUY_INFY] * 50}))
    assert len(fake_kite.json_body(fake_kite.calls("POST", "/margins/orders")[0])) == 50


# --- error mapping ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("tool", CALCULATORS)
async def test_kite_input_errors_come_back_as_short_tool_errors(mcp_factory, fake_kite, tool):
    for path in ("/margins/orders", "/margins/basket", "/charges/orders"):
        fake_kite.on("POST", path, status=400, error_type="InputException", message="Invalid tradingsymbol")
    server, _, _ = mcp_factory()
    valid = {**BUY_INFY, "average_price": 100} if tool == "get_order_charges" else BUY_INFY
    result = await call(server, tool, {"orders": [valid]})
    assert result.is_error
    assert f"{tool}: Kite error InputException HTTP 400: Invalid tradingsymbol" in tool_text(result)


async def test_an_expired_session_says_how_to_log_in_again(mcp_factory, fake_kite):
    fake_kite.on(
        "POST", "/margins/orders", status=403, error_type="TokenException", message="Incorrect api_key or access_token"
    )
    server, _, _ = mcp_factory()
    result = await call(server, "get_order_margins", {"orders": [BUY_INFY]})
    text = tool_text(result)
    assert result.is_error and "Kite rejected the session" in text and "login" in text


async def test_not_being_logged_in_is_reported_before_any_request(mcp_factory, fake_kite, tmp_path):
    server, _, _ = mcp_factory(KITE_TOKEN_FILE=str(tmp_path / "missing.json"))
    result = await call(server, "get_order_margins", {"orders": [BUY_INFY]})
    assert result.is_error and "not logged in to Kite" in tool_text(result)
    assert fake_kite.requests == []
