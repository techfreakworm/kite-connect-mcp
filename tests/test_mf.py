"""Mutual funds: the reads, the opt-in legacy writes (hidden unless KITE_MCP_ALLOW_MF_WRITES=true), and how those writes
go through the safety pipeline: dry run, two-step confirmation, caps, allow-lists, checks against the fund's rules.

The instrument master is another component, so these tests install a small stub on `rt.instruments`. The ISINs and
fund names are made up; there is no personal data here."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import httpx2
import pytest
from mcp import Client

from conftest import tool_json, tool_text
from kite_connect_mcp.errors import InstrumentsUnavailable
from kite_connect_mcp.instruments import MFInstrument
from kite_connect_mcp.tools.mf import WRITE_TOOLS

ELSS = "INF000A01AA0"  # minimum 500, in multiples of 500
ELSS_NAME = "Example ELSS Fund - Direct Growth"
FLEXI = "INF000B01BB1"  # first purchase 5000, additional 1000, any whole rupee
WRITES = {"KITE_MCP_ALLOW_MF_WRITES": "true"}
LIVE = {"KITE_MCP_TRADING_MODE": "live"}
BUY = {"tradingsymbol": ELSS, "transaction_type": "BUY", "amount": 1000}
SELL = {"tradingsymbol": ELSS, "transaction_type": "SELL", "quantity": 1}
SIP = {"tradingsymbol": ELSS, "amount": 1000, "instalments": 12, "frequency": "monthly", "instalment_day": 5}


def make_fund(isin: str = ELSS, **changes) -> MFInstrument:
    values = {
        "tradingsymbol": isin,
        "amc": "EXAMPLEMUTUALFUND_MF",
        "name": ELSS_NAME,
        "purchase_allowed": True,
        "redemption_allowed": True,
        "minimum_purchase_amount": Decimal("500"),
        "purchase_amount_multiplier": Decimal("500"),
        "minimum_additional_purchase_amount": Decimal("500"),
        "minimum_redemption_quantity": Decimal("0.001"),
        "redemption_quantity_multiplier": Decimal("0.001"),
        "dividend_type": "growth",
        "scheme_type": "elss",
        "plan": "direct",
        "settlement_type": "T3",
        "last_price": Decimal("33.0425"),
        "last_price_date": dt.date(2026, 11, 3),
    }
    return MFInstrument(**{**values, **changes})


def flexi_fund(**changes) -> MFInstrument:
    return make_fund(
        FLEXI,
        name="Example Focus Fund - Direct Plan - Growth",
        amc="EXAMPLEMF",
        scheme_type="equity",
        minimum_purchase_amount=Decimal("5000"),
        purchase_amount_multiplier=Decimal("1"),
        minimum_additional_purchase_amount=Decimal("1000"),
        last_price=Decimal("26.549"),
        **changes,
    )


class StubStore:
    """The only part of the instrument master the MF tools use: lookup_mf."""

    def __init__(self, *funds: MFInstrument, unavailable: bool = False):
        self.funds = {f.tradingsymbol: f for f in funds}
        self.unavailable = unavailable

    async def lookup_mf(self, tradingsymbol: str) -> MFInstrument | None:
        if self.unavailable:
            raise InstrumentsUnavailable("no MF instrument dump (test)")
        return self.funds.get(tradingsymbol)


def setup(mcp_factory, *funds: MFInstrument, **env):
    """(server, registrar, runtime) with MF writes enabled and a stub instrument master holding `funds`."""
    server, reg, rt = mcp_factory(**{**WRITES, **env})
    rt.instruments = StubStore(*funds)
    return server, reg, rt


async def call(server, name, args):
    async with Client(server) as client:
        return await client.call_tool(name, args)


async def dry(server, name, args):
    """The JSON answer of a call that must succeed (a dry run)."""
    return tool_json(await call(server, name, args))


async def confirmed(server, name, args):
    """The two-step flow: a dry run for the token, then the identical call with dry_run=false and that token."""
    preview = await dry(server, name, args)
    sent = await call(server, name, {**args, "dry_run": False, "confirmation_token": preview["confirmation_token"]})
    return preview, sent


def violations(answer):
    return answer["checks"]["violations"]


def warnings(answer):
    return answer["checks"]["warnings"]


# --- reads ----------------------------------------------------------------------------------------------------------

HOLDINGS = [
    {
        "folio": "F0000001",
        "average_price": 78.43,
        "last_price": 84.86,
        "last_price_date": "",
        "pledged_quantity": 0,
        "fund": "EXAMPLE TAX PLAN - DIRECT PLAN",
        "tradingsymbol": "INF000C01CC2",
        "pnl": 0,
        "quantity": 382.488,
    },
    {
        "folio": "F0000002",
        "average_price": 1874.101138,
        "last_price": 2081.4984,
        "last_price_date": "",
        "pledged_quantity": 0,
        "fund": "Example Liquid Fund - Direct Plan",
        "tradingsymbol": "INF000D01DD3",
        "pnl": 0,
        "quantity": 1.334,
    },
    {
        "folio": "F0000003",
        "average_price": 116.7,
        "last_price": 101.13,
        "last_price_date": "",
        "pledged_quantity": 0,
        "fund": "EXAMPLE TAX ADVANTAGE FUND - DIRECT PLAN",
        "tradingsymbol": "INF000E01EE4",
        "pnl": 0,
        "quantity": 257.057,
    },
]


async def test_holdings_largest_first_with_a_summary_over_all_of_them(mcp_factory, fake_kite):
    fake_kite.on("GET", "/mf/holdings", HOLDINGS)
    server, _, _ = mcp_factory()
    page = await dry(server, "get_mf_holdings", {"limit": 2})
    assert (page["total"], page["count"], page["next_offset"]) == (3, 2, 2)
    assert [h["tradingsymbol"] for h in page["items"]] == ["INF000C01CC2", "INF000E01EE4"]  # 32,457.93 then 25,996.17
    summary = page["summary"]  # covers the third holding too, which is not on this page
    assert (summary["funds"], summary["invested_inr"], summary["current_value_inr"]) == (3, 62497.14, 61230.82)
    assert (summary["pnl_inr"], summary["pnl_percent"]) == (-1266.31, -2.03)
    assert "funds_without_prices" not in summary
    second = await dry(server, "get_mf_holdings", {"limit": 2, "offset": 2})
    assert second["count"] == 1 and "next_offset" not in second
    assert second["items"][0]["tradingsymbol"] == "INF000D01DD3"


async def test_holdings_without_a_price_are_counted_but_not_valued(mcp_factory, fake_kite):
    fake_kite.on("GET", "/mf/holdings", [HOLDINGS[0], {**HOLDINGS[1], "last_price": None}])
    server, _, _ = mcp_factory()
    summary = (await dry(server, "get_mf_holdings", {}))["summary"]
    assert summary["funds"] == 2 and summary["funds_without_prices"] == 1
    assert summary["invested_inr"] == 29998.53 and summary["current_value_inr"] == 32457.93


ORDERS = [
    {"order_id": "o-1", "status": "REJECTED", "order_timestamp": "2026-11-02 08:33:07", "tradingsymbol": FLEXI},
    {"order_id": "o-2", "status": "OPEN", "order_timestamp": "2026-11-03 12:20:28", "tradingsymbol": ELSS},
    {"order_id": "o-3", "status": "COMPLETE", "order_timestamp": "2026-11-03 15:37:27", "tradingsymbol": ELSS},
    {"order_id": "o-4", "status": "OPEN", "order_timestamp": "2026-11-01 09:00:00", "tradingsymbol": FLEXI},
    {"order_id": "o-5", "status": "REJECTED", "order_timestamp": "2026-10-31 01:30:02", "tradingsymbol": FLEXI},
]


async def test_orders_of_the_last_seven_days_newest_first_with_status_filter(mcp_factory, fake_kite):
    fake_kite.on("GET", "/mf/orders", ORDERS)
    server, _, _ = mcp_factory()
    everything = await dry(server, "get_mf_orders", {})
    assert [o["order_id"] for o in everything["items"]] == ["o-3", "o-2", "o-1", "o-4", "o-5"]
    assert everything["status_counts"] == {"COMPLETE": 1, "OPEN": 2, "REJECTED": 2}
    assert everything["window"] == "last 7 days" and everything["total"] == 5
    open_only = await dry(server, "get_mf_orders", {"status": "open", "limit": 1})
    assert [o["order_id"] for o in open_only["items"]] == ["o-2"]
    assert (open_only["total"], open_only["next_offset"]) == (2, 1)
    assert open_only["status_counts"] == everything["status_counts"]  # counted before the filter
    assert [r.url.path for r in fake_kite.requests] == ["/mf/orders"] * 2


async def test_one_order_by_id_whatever_its_age(mcp_factory, fake_kite):
    order_id = "00000000-0000-4000-8000-000000000001"
    order = {**ORDERS[1], "order_id": order_id, "amount": 5000.0, "fund": ELSS_NAME}
    fake_kite.on("GET", f"/mf/orders/{order_id}", order)
    server, _, _ = mcp_factory()
    assert await dry(server, "get_mf_orders", {"order_id": order_id}) == order
    assert [r.url.path for r in fake_kite.requests] == [f"/mf/orders/{order_id}"]


SIPS = [
    {"sip_id": "s-1", "status": "ACTIVE", "tradingsymbol": ELSS, "instalment_amount": 500.0, "frequency": "weekly"},
    {"sip_id": "s-2", "status": "PAUSED", "tradingsymbol": FLEXI, "instalment_amount": 1000.0, "frequency": "monthly"},
    {
        "sip_id": "s-3",
        "status": "ACTIVE",
        "tradingsymbol": FLEXI,
        "instalment_amount": 7427.0,
        "frequency": "quarterly",
    },
]


async def test_sips_all_or_one(mcp_factory, fake_kite):
    fake_kite.on("GET", "/mf/sips", SIPS).on("GET", "/mf/sips/s-2", SIPS[1])
    server, _, _ = mcp_factory()
    page = await dry(server, "get_mf_sips", {"limit": 2})
    assert (page["total"], page["count"], page["next_offset"]) == (3, 2, 2)
    assert page["status_counts"] == {"ACTIVE": 2, "PAUSED": 1}
    assert [s["sip_id"] for s in page["items"]] == ["s-1", "s-2"]
    assert await dry(server, "get_mf_sips", {"sip_id": "s-2"}) == SIPS[1]


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("get_mf_orders", {"order_id": "../orders"}),
        ("get_mf_sips", {"sip_id": "a/b"}),
        ("get_mf_orders", {"limit": 0}),
        ("get_mf_holdings", {"offset": -1}),
    ],
)
async def test_read_arguments_are_validated_before_any_request(mcp_factory, fake_kite, tool, args):
    server, _, _ = mcp_factory()
    result = await call(server, tool, args)
    assert result.is_error and fake_kite.requests == []


async def test_reads_work_in_read_only_mode(mcp_factory, fake_kite):
    fake_kite.on("GET", "/mf/holdings", HOLDINGS)
    server, _, _ = mcp_factory(KITE_MCP_READ_ONLY="true", **WRITES)
    async with Client(server) as client:
        names = {t.name for t in (await client.list_tools()).tools}
    assert {"get_mf_holdings", "get_mf_orders", "get_mf_sips"} <= names
    assert not names & set(WRITE_TOOLS)
    assert (await dry(server, "get_mf_holdings", {}))["total"] == 3


async def test_read_errors_are_short_and_say_what_to_do(mcp_factory, fake_kite):
    fake_kite.on("GET", "/mf/orders/nope", status=400, error_type="InputException", message="Order not found")
    fake_kite.on("GET", "/mf/sips", status=403, error_type="TokenException", message="Incorrect api_key or token")
    server, _, _ = mcp_factory()
    missing = await call(server, "get_mf_orders", {"order_id": "nope"})
    assert missing.is_error and "get_mf_orders: Kite error InputException HTTP 400: Order not found" in tool_text(
        missing
    )
    expired = await call(server, "get_mf_sips", {})
    assert expired.is_error and "Kite rejected the session" in tool_text(expired)


# --- the writes are opt-in ------------------------------------------------------------------------------------------


async def test_writes_are_hidden_unless_the_flag_is_set(mcp_factory):
    server, reg, _ = mcp_factory()
    assert {name: reg.skipped[name] for name in WRITE_TOOLS} == dict.fromkeys(
        WRITE_TOOLS, "KITE_MCP_ALLOW_MF_WRITES is false"
    )
    async with Client(server) as client:
        names = {t.name for t in (await client.list_tools()).tools}
    assert {"get_mf_holdings", "get_mf_orders", "get_mf_sips"} <= names and not names & set(WRITE_TOOLS)

    server, reg, _ = mcp_factory(**WRITES)
    assert {i.name for i in reg.registered if i.toolset == "mf" and i.kind == "write"} == set(WRITE_TOOLS)
    assert not set(WRITE_TOOLS) & set(reg.skipped)
    async with Client(server) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    for name in WRITE_TOOLS:
        description = " ".join(tools[name].description.split())  # the text is hard-wrapped
        assert "Kite may reject this request" in description, name
        assert "DRY RUN BY DEFAULT" in description and "KITE_MCP_ALLOW_MF_WRITES" in description, name


@pytest.mark.parametrize(
    ("env", "reason"),
    [
        ({"KITE_MCP_READ_ONLY": "true"}, "read-only mode"),
        ({"KITE_MCP_TOOLSETS": "account"}, "toolset mf is not in KITE_MCP_TOOLSETS"),
        ({"KITE_MCP_EXCLUDED_TOOLS": "place_mf_sip"}, "listed in KITE_MCP_EXCLUDED_TOOLS"),
    ],
)
async def test_the_usual_switches_still_hide_the_writes(mcp_factory, env, reason):
    _, reg, _ = mcp_factory(**WRITES, **env)
    expected = ["place_mf_sip"] if "KITE_MCP_EXCLUDED_TOOLS" in env else list(WRITE_TOOLS)
    assert [name for name in WRITE_TOOLS if name in reg.skipped] == expected
    assert {reg.skipped[name] for name in expected} == {reason}
    _, reg, _ = mcp_factory(**env)  # with the flag off too, the outer reason is the one reported
    assert reg.skipped["place_mf_sip"] == reason


# --- placing an order: dry run, then the two-step live flow ---------------------------------------------------------


async def test_a_purchase_is_a_dry_run_by_default_and_shows_the_exact_request(mcp_factory, fake_kite):
    server, _, rt = setup(mcp_factory, make_fund())
    args = {**BUY, "tradingsymbol": ELSS.lower(), "amount": 5000, "tag": "coin01"}
    answer = await dry(server, "place_mf_order", args)
    assert answer["dry_run"] is True and answer["sent"] is False
    assert answer["summary"] == f"BUY ₹5,000.00 of {ELSS_NAME} ({ELSS})"
    assert answer["would_send"] == {
        "method": "POST",
        "path": "/mf/orders",
        "form": {"tradingsymbol": ELSS, "transaction_type": "BUY", "amount": "5000", "tag": "coin01"},
    }
    assert answer["checks"]["ok"] is True and violations(answer) == []
    assert any("legacy route" in w and "may reject" in w for w in warnings(answer))
    assert answer["legs"] == [
        {"instrument": f"MF:{ELSS}", "transaction_type": "BUY", "value_inr": "5000.00", "value_note": "purchase amount"}
    ]
    assert answer["fund"]["name"] == ELSS_NAME and answer["fund"]["last_nav_inr"] == 33.0425
    assert "confirmation_token" not in answer and "disabled" in answer["live_trading"]
    assert fake_kite.requests == []
    assert [e["event"] for e in rt.audit.read()] == ["dry_run"]

    live = await call(server, "place_mf_order", {**args, "dry_run": False})
    assert live.is_error and "KITE_MCP_TRADING_MODE=dry_run" in tool_text(live)
    assert fake_kite.writes() == []


async def test_a_purchase_goes_live_only_with_the_token_from_its_dry_run(mcp_factory, fake_kite):
    fake_kite.on("POST", "/mf/orders", {"order_id": "mf-order-1"})
    server, _, rt = setup(mcp_factory, make_fund(), **LIVE)
    args = {**BUY, "amount": 5000}
    preview = await dry(server, "place_mf_order", args)
    token = preview["confirmation_token"]
    assert preview["live_trading"] == "enabled" and fake_kite.writes() == []

    no_token = await call(server, "place_mf_order", {**args, "dry_run": False})
    assert no_token.is_error and "confirmation_token is required" in tool_text(no_token)
    changed = await call(
        server, "place_mf_order", {**args, "amount": 6000, "dry_run": False, "confirmation_token": token}
    )
    assert changed.is_error and "does not match" in tool_text(changed)
    assert fake_kite.writes() == []

    sent = await dry(server, "place_mf_order", {**args, "dry_run": False, "confirmation_token": token})
    assert sent["sent"] is True and sent["response"] == {"order_id": "mf-order-1"}
    (request,) = fake_kite.writes()
    assert (request.method, request.url.path) == ("POST", "/mf/orders")
    assert request.headers["content-type"] == "application/x-www-form-urlencoded"
    assert fake_kite.form(request) == [("tradingsymbol", ELSS), ("transaction_type", "BUY"), ("amount", "5000")]

    replay = await call(server, "place_mf_order", {**args, "dry_run": False, "confirmation_token": token})
    assert replay.is_error and "already used" in tool_text(replay)
    assert len(fake_kite.writes()) == 1
    events = [e["event"] for e in rt.audit.read()]
    assert events == ["confirmation_issued", "refused", "refused", "live_intent", "live_result", "refused"]
    usage = rt.audit.usage()
    assert (usage.orders, usage.value) == (1, Decimal(5000))


async def test_a_redemption_is_valued_at_the_last_nav_and_confirmed_the_same_way(mcp_factory, fake_kite):
    fake_kite.on("POST", "/mf/orders", {"order_id": "mf-order-2"})
    server, _, rt = setup(mcp_factory, make_fund(), **LIVE)
    preview, sent = await confirmed(server, "place_mf_order", {**SELL, "quantity": 12.345})
    assert preview["would_send"]["form"] == {"tradingsymbol": ELSS, "transaction_type": "SELL", "quantity": "12.345"}
    leg = preview["legs"][0]
    assert (leg["transaction_type"], leg["price_used"], leg["value_inr"]) == ("SELL", "33.0425", "407.91")  # x NAV
    assert leg["value_note"].startswith("units x last NAV ₹33.0425 of 2026-11-03")
    assert preview["checks"]["ok"] is True
    assert tool_json(sent)["sent"] is True
    (request,) = fake_kite.writes()
    assert fake_kite.form(request) == [("tradingsymbol", ELSS), ("transaction_type", "SELL"), ("quantity", "12.345")]
    assert rt.audit.usage().value == Decimal("407.91")


@pytest.mark.parametrize(
    ("store", "why"),
    [("unavailable", "could not be read"), ("unlisted", "not in the MF instrument list"), ("none", "not loaded")],
)
async def test_a_redemption_with_no_known_nav_can_be_previewed_but_never_sent(mcp_factory, fake_kite, store, why):
    server, _, rt = setup(mcp_factory, **LIVE)
    rt.instruments = {"unavailable": StubStore(unavailable=True), "unlisted": StubStore(), "none": None}[store]
    answer = await dry(server, "place_mf_order", {**SELL, "quantity": 10})
    assert answer["checks"]["ok"] is False
    assert any("cannot be verified" in v for v in violations(answer))
    assert any(why in w for w in warnings(answer))
    assert "value_inr" not in answer["legs"][0] and "confirmation_token" not in answer
    assert answer["would_send"]["form"]["quantity"] == "10"
    live = await call(server, "place_mf_order", {**SELL, "quantity": 10, "dry_run": False})
    assert live.is_error and "cannot be verified" in tool_text(live)
    assert fake_kite.writes() == []


async def test_a_redemption_of_a_fund_without_a_nav_is_unverifiable_too(mcp_factory):
    server, _, _ = setup(mcp_factory, make_fund(last_price=Decimal(0)), **LIVE)
    answer = await dry(server, "place_mf_order", SELL)
    assert answer["checks"]["ok"] is False and "lists no NAV" in answer["legs"][0]["value_note"]


async def test_a_purchase_needs_no_nav_so_it_still_goes_live_when_the_master_is_down(mcp_factory, fake_kite):
    fake_kite.on("POST", "/mf/orders", {"order_id": "mf-order-3"})
    server, _, rt = setup(mcp_factory, **LIVE)
    rt.instruments = StubStore(unavailable=True)
    preview, sent = await confirmed(server, "place_mf_order", {**BUY, "amount": 750})  # off the multiple, unchecked
    assert preview["checks"]["ok"] is True and "fund" not in preview
    assert any("could not be read" in w and "were not checked" in w for w in warnings(preview))
    assert tool_json(sent)["sent"] is True and len(fake_kite.writes()) == 1


# --- the fund's own rules -------------------------------------------------------------------------------------------

MULTIPLE = f"amount ₹750.00 is not a multiple of ₹500.00, the purchase amount multiplier of {ELSS_NAME} ({ELSS})"


@pytest.mark.parametrize(
    ("funds", "args", "message"),
    [
        ([make_fund()], {**BUY, "amount": 750}, MULTIPLE),
        ([make_fund()], {**BUY, "amount": 250}, f"amount ₹250.00 is below the minimum purchase of {ELSS_NAME}"),
        (
            [flexi_fund()],
            {**BUY, "tradingsymbol": FLEXI, "amount": 500},
            "₹5,000.00 for a first purchase, ₹1,000.00 for an additional one",
        ),
        ([make_fund(purchase_allowed=False)], BUY, "is closed to purchases (purchase_allowed is 0"),
        ([], {**BUY, "amount": 1000.005}, "amount 1000.005 has more than 2 decimals"),
        ([], {**BUY, "amount": 1e-05}, "amount 0.00001 has more than 2 decimals"),
        ([], {**BUY, "tradingsymbol": "XX-000A01AA0"}, "is not an ISIN"),
        ([], {**BUY, "tag": "way too long"}, "at most 8"),
        ([], {**BUY, "tag": "bad tag!"}, "tag must be 1 to 8 letters or digits"),
        ([], {**BUY, "quantity": 5}, "a BUY takes amount (INR) and no quantity"),
        ([], {**BUY, "amount": None}, "a BUY takes amount (INR) and no quantity"),
        ([], {**SELL, "amount": 1000}, "a SELL takes quantity (units) and no amount"),
        ([], {**SELL, "quantity": None}, "a SELL takes quantity (units) and no amount"),
        ([], {**SELL, "quantity": 1.23456}, "quantity 1.23456 has more than 4 decimals"),
        ([make_fund()], {**SELL, "quantity": 1.2345}, "not a multiple of 0.001 units"),
        ([make_fund(redemption_allowed=False)], SELL, "is closed to redemptions"),
    ],
)
async def test_requests_kite_would_certainly_refuse_are_errors_before_anything_happens(
    mcp_factory, fake_kite, funds, args, message
):
    server, _, rt = setup(mcp_factory, *funds)
    result = await call(server, "place_mf_order", {k: v for k, v in args.items() if v is not None})
    assert result.is_error and message in tool_text(result)
    assert fake_kite.requests == [] and rt.audit.read() == []


@pytest.mark.parametrize(
    ("fund", "args", "warning"),
    [
        (flexi_fund(), {**BUY, "tradingsymbol": FLEXI, "amount": 2000}, "below the ₹5,000.00 minimum for a first"),
        (make_fund(minimum_redemption_quantity=Decimal(1)), {**SELL, "quantity": 0.5}, "your entire holding"),
        (make_fund(last_price_date=dt.date(2026, 10, 20)), SELL, "is from 2026-10-20"),
        (make_fund(last_price_date=None), SELL, "is of unknown date"),
    ],
)
async def test_doubtful_requests_pass_with_a_warning(mcp_factory, fund, args, warning):
    server, _, _ = setup(mcp_factory, fund)
    answer = await dry(server, "place_mf_order", args)
    assert answer["checks"]["ok"] is True
    assert any(warning in w for w in warnings(answer)), warnings(answer)


async def test_a_purchase_that_meets_every_rule_has_no_warning_but_the_legacy_one(mcp_factory):
    server, _, _ = setup(mcp_factory, flexi_fund())
    answer = await dry(server, "place_mf_order", {**BUY, "tradingsymbol": FLEXI, "amount": 5000})
    assert [w[:12] for w in warnings(answer)] == ["legacy route"]


async def test_an_unlisted_fund_is_a_warning_not_an_error(mcp_factory):
    server, _, _ = setup(mcp_factory)  # an empty master
    answer = await dry(server, "place_mf_order", {**BUY, "amount": 750})
    assert answer["checks"]["ok"] is True
    assert any("not in the MF instrument list" in w and "probably reject" in w for w in warnings(answer))


# --- caps and allow-lists -------------------------------------------------------------------------------------------


async def test_the_per_order_cap_applies_to_a_purchase(mcp_factory, fake_kite):
    server, _, _ = setup(mcp_factory, make_fund(), KITE_MCP_MAX_ORDER_VALUE="4000", **LIVE)
    args = {**BUY, "amount": 5000}
    answer = await dry(server, "place_mf_order", args)
    assert answer["checks"]["ok"] is False and "confirmation_token" not in answer
    assert any("per-order cap" in v for v in violations(answer))
    live = await call(server, "place_mf_order", {**args, "dry_run": False})
    assert live.is_error and "per-order cap" in tool_text(live)
    assert fake_kite.writes() == []


async def test_orders_count_toward_the_daily_limits(mcp_factory, fake_kite):
    fake_kite.on("POST", "/mf/orders", {"order_id": "mf-order-4"})
    server, _, _ = setup(mcp_factory, make_fund(), KITE_MCP_MAX_ORDERS_PER_DAY="1", **LIVE)
    assert tool_json((await confirmed(server, "place_mf_order", BUY))[1])["sent"] is True
    second = await dry(server, "place_mf_order", {**BUY, "amount": 1500})
    assert any("daily limit of 1" in v for v in violations(second))
    assert second["today"] == {"live_orders": 1, "live_value_inr": "1000.00"}


async def test_an_instrument_allow_list_applies_to_funds_as_mf_and_the_isin(mcp_factory, fake_kite):
    server, _, _ = setup(mcp_factory, make_fund(), KITE_MCP_ALLOWED_INSTRUMENTS="NSE:INFY", **LIVE)
    weekly_sip = {k: v for k, v in {**SIP, "frequency": "weekly"}.items() if k != "instalment_day"}
    for name, args in (("place_mf_order", BUY), ("place_mf_sip", weekly_sip)):
        answer = await dry(server, name, args)
        assert answer["checks"]["ok"] is False and "confirmation_token" not in answer
        assert f"instrument MF:{ELSS} is not in KITE_MCP_ALLOWED_INSTRUMENTS" in violations(answer)
        live = await call(server, name, {**args, "dry_run": False})
        assert live.is_error and "KITE_MCP_ALLOWED_INSTRUMENTS" in tool_text(live)
    assert fake_kite.writes() == []


async def test_listing_the_fund_allows_it(mcp_factory):
    server, _, _ = setup(mcp_factory, make_fund(), KITE_MCP_ALLOWED_INSTRUMENTS=f"NSE:INFY,MF:{ELSS}", **LIVE)
    answer = await dry(server, "place_mf_order", BUY)
    assert answer["checks"]["ok"] is True and "confirmation_token" in answer


async def test_exchange_product_variety_and_order_type_lists_do_not_block_funds(mcp_factory):
    env = {
        "KITE_MCP_ALLOWED_EXCHANGES": "NSE",
        "KITE_MCP_ALLOWED_PRODUCTS": "CNC",
        "KITE_MCP_ALLOWED_VARIETIES": "regular",
        "KITE_MCP_ALLOWED_ORDER_TYPES": "LIMIT",
    }
    server, _, _ = setup(mcp_factory, make_fund(), **env, **LIVE)
    assert (await dry(server, "place_mf_order", BUY))["checks"]["ok"] is True


async def test_the_transaction_type_allow_list_does_apply(mcp_factory):
    server, _, _ = setup(mcp_factory, make_fund(), KITE_MCP_ALLOWED_TRANSACTION_TYPES="SELL", **LIVE)
    buy = await dry(server, "place_mf_order", BUY)
    assert any("transaction_type BUY is not in the allow-list" in v for v in violations(buy))
    assert (await dry(server, "place_mf_order", SELL))["checks"]["ok"] is True


async def test_allowlist_confirmation_mode_skips_the_token_for_a_listed_fund_only(mcp_factory, fake_kite):
    fake_kite.on("POST", "/mf/orders", {"order_id": "mf-order-5"})
    env = {"KITE_MCP_LIVE_CONFIRMATION": "allowlist", "KITE_MCP_ALLOWED_INSTRUMENTS": f"MF:{ELSS}"}
    server, _, _ = setup(mcp_factory, make_fund(), flexi_fund(), **env, **LIVE)
    sent = await dry(server, "place_mf_order", {**BUY, "dry_run": False})
    assert sent["sent"] is True
    other = await call(server, "place_mf_order", {**BUY, "tradingsymbol": FLEXI, "amount": 5000, "dry_run": False})
    assert other.is_error and "KITE_MCP_ALLOWED_INSTRUMENTS" in tool_text(other)
    assert len(fake_kite.writes()) == 1


# --- SIPs -----------------------------------------------------------------------------------------------------------


async def test_a_sip_counts_the_initial_amount_else_one_instalment_toward_the_daily_value(mcp_factory, fake_kite):
    fake_kite.on("POST", "/mf/sips", {"sip_id": "sip-1"})
    server, _, rt = setup(mcp_factory, make_fund(), **LIVE)
    preview, sent = await confirmed(server, "place_mf_sip", {**SIP, "initial_amount": 2000})
    assert preview["would_send"] == {
        "method": "POST",
        "path": "/mf/sips",
        "form": {
            "tradingsymbol": ELSS,
            "amount": "1000",
            "instalments": "12",
            "frequency": "monthly",
            "initial_amount": "2000",
            "instalment_day": "5",
        },
    }
    assert preview["summary"] == (
        f"SIP of ₹1,000.00 monthly (12 instalments) into {ELSS_NAME} ({ELSS}), starting with ₹2,000.00"
    )
    assert [(leg["value_inr"], leg["value_note"]) for leg in preview["legs"]] == [
        ("2000.00", "initial lump sum"),
        ("1000.00", "one SIP instalment"),
    ]
    assert preview["commitment"] == {
        "instalment_inr": 1000.0,
        "frequency": "monthly",
        "instalments": 12,
        "counted_toward_the_daily_cap_inr": 2000.0,
        "total_of_all_instalments_inr": 12000.0,
        "initial_amount_inr": 2000.0,
    }
    assert any("later instalments on its own schedule" in w for w in warnings(preview))
    assert tool_json(sent)["response"] == {"sip_id": "sip-1"}
    assert fake_kite.form(fake_kite.writes()[0]) == [
        ("tradingsymbol", ELSS),
        ("amount", "1000"),
        ("instalments", "12"),
        ("frequency", "monthly"),
        ("initial_amount", "2000"),
        ("instalment_day", "5"),
    ]
    usage = rt.audit.usage()
    assert (usage.orders, usage.value) == (1, Decimal(2000))  # the initial amount, not 2000 + 1000

    preview, sent = await confirmed(server, "place_mf_sip", {**SIP, "instalments": -1})  # no initial amount, no end
    assert preview["commitment"]["instalments"] == "until cancelled"
    assert "total_of_all_instalments_inr" not in preview["commitment"]
    assert preview["commitment"]["counted_toward_the_daily_cap_inr"] == 1000.0
    assert len(preview["legs"]) == 1 and "(until cancelled)" in preview["summary"]
    assert ("instalments", "-1") in fake_kite.form(fake_kite.writes()[1])
    usage = rt.audit.usage()
    assert (usage.orders, usage.value) == (2, Decimal(3000))  # plus one instalment


async def test_each_part_of_a_sip_is_checked_against_the_per_order_cap(mcp_factory):
    server, _, _ = setup(mcp_factory, make_fund(), KITE_MCP_MAX_ORDER_VALUE="1500", **LIVE)
    answer = await dry(server, "place_mf_sip", {**SIP, "initial_amount": 2000})
    assert answer["checks"]["ok"] is False
    assert any("₹2,000.00 is above the per-order cap ₹1,500.00" in v for v in violations(answer))
    assert (await dry(server, "place_mf_sip", SIP))["checks"]["ok"] is True


async def test_the_daily_value_cap_sees_the_sip(mcp_factory, fake_kite):
    fake_kite.on("POST", "/mf/sips", {"sip_id": "sip-2"})
    server, _, _ = setup(mcp_factory, make_fund(), KITE_MCP_MAX_DAILY_VALUE="2500", **LIVE)
    args = {**SIP, "initial_amount": 2000}
    await confirmed(server, "place_mf_sip", args)
    second = await dry(server, "place_mf_sip", args)
    assert any(
        "₹2,000.00 placed live today; ₹2,000.00 more would pass the daily cap ₹2,500.00" in v
        for v in violations(second)
    )
    assert len(fake_kite.writes()) == 1


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"instalments": 0}, "instalments must be -1 (until cancelled) or at least 1"),
        ({"instalments": -2}, "greater than or equal to -1"),
        ({"frequency": "weekly"}, "instalment_day only applies to monthly SIPs, not weekly ones"),
        ({"frequency": "daily"}, "Input should be 'weekly', 'monthly' or 'quarterly'"),
        ({"instalment_day": 32}, "less than or equal to 31"),
        ({"amount": 100.123}, "amount 100.123 has more than 2 decimals"),
        ({"initial_amount": 0}, "greater than 0"),
        ({"tradingsymbol": "INF000A01AA"}, "at least 12 characters"),
        ({"tradingsymbol": "XX-000A01AA0"}, "is not an ISIN"),
        ({"tag": "bad tag!"}, "tag must be 1 to 8 letters or digits"),
        ({"amount": 750}, "not a multiple of ₹500.00"),
        ({"initial_amount": 250}, "initial_amount ₹250.00 is not a multiple of ₹500.00"),
    ],
)
async def test_impossible_sips_are_rejected_before_anything(mcp_factory, fake_kite, change, message):
    server, _, rt = setup(mcp_factory, make_fund())
    result = await call(server, "place_mf_sip", {**SIP, **change})
    assert result.is_error and message in tool_text(result)
    assert fake_kite.requests == [] and rt.audit.read() == []


async def test_a_sip_into_a_fund_closed_to_purchases_is_refused_up_front(mcp_factory):
    server, _, _ = setup(mcp_factory, make_fund(purchase_allowed=False))
    result = await call(server, "place_mf_sip", SIP)
    assert result.is_error and "is closed to purchases" in tool_text(result)


@pytest.mark.parametrize(
    ("fund", "args", "warning"),
    [
        (make_fund(), {"instalment_day": 12}, "instalment_day 12 is not one of 1, 5, 10, 15, 20, 25"),
        (flexi_fund(), {"tradingsymbol": FLEXI, "amount": 500}, "SIP minimums are not in Kite's instrument list"),
        (flexi_fund(), {"tradingsymbol": FLEXI, "initial_amount": 2000}, "minimum for a first purchase"),
    ],
)
async def test_doubtful_sips_pass_with_a_warning(mcp_factory, fund, args, warning):
    server, _, _ = setup(mcp_factory, fund)
    answer = await dry(server, "place_mf_sip", {**SIP, **args})
    assert answer["checks"]["ok"] is True
    assert any(warning in w for w in warnings(answer)), warnings(answer)


# --- modifying and cancelling ---------------------------------------------------------------------------------------

SIP_RECORD = {
    "sip_id": "sip-1",
    "tradingsymbol": ELSS,
    "fund": ELSS_NAME,
    "status": "ACTIVE",
    "frequency": "monthly",
    "instalment_amount": 1000.0,
    "instalments": -1,
    "instalment_day": 5,
    "transaction_type": "BUY",
}


async def test_changing_a_sip_checks_the_new_instalment_and_does_not_count_as_an_order(mcp_factory, fake_kite):
    fake_kite.on("GET", "/mf/sips/sip-1", SIP_RECORD).on("PUT", "/mf/sips/sip-1", {"sip_id": "sip-1"})
    server, _, rt = setup(mcp_factory, make_fund(), **LIVE)
    preview, sent = await confirmed(server, "modify_mf_sip", {"sip_id": "sip-1", "amount": 1500})
    assert preview["would_send"] == {"method": "PUT", "path": "/mf/sips/sip-1", "form": {"amount": "1500"}}
    assert preview["summary"] == f"modify SIP sip-1 (₹1,000.00 monthly into {ELSS_NAME}, ACTIVE): amount ₹1,500.00"
    assert [(leg["instrument"], leg["value_inr"], leg["value_note"]) for leg in preview["legs"]] == [
        (f"MF:{ELSS}", "1500.00", "new SIP instalment")
    ]
    assert preview["current_sip"] == SIP_RECORD
    assert tool_json(sent)["sent"] is True
    (request,) = fake_kite.writes()
    assert (request.method, request.url.path) == ("PUT", "/mf/sips/sip-1")
    assert fake_kite.form(request) == [("amount", "1500")]
    assert rt.audit.usage().orders == 0

    # a cap of 1200 refuses a raise to 1500, but not a change that leaves the 1000 instalment as it is
    capped, _, _ = setup(mcp_factory, make_fund(), KITE_MCP_MAX_ORDER_VALUE="1200", **LIVE)
    over = await dry(capped, "modify_mf_sip", {"sip_id": "sip-1", "amount": 1500})
    assert any("per-order cap" in v for v in violations(over))
    under = await dry(capped, "modify_mf_sip", {"sip_id": "sip-1", "instalments": 24})
    assert under["checks"]["ok"] is True and under["legs"][0]["value_note"] == "current SIP instalment (unchanged)"


async def test_pausing_a_sip_is_risk_reducing_but_resuming_or_raising_it_is_not(mcp_factory, fake_kite):
    fake_kite.on("GET", "/mf/sips/sip-1", SIP_RECORD).on("PUT", "/mf/sips/sip-1", {"sip_id": "sip-1"})
    server, _, _ = setup(mcp_factory, KITE_MCP_MAX_ORDER_VALUE="500", **LIVE)  # the SIP's 1000 is over the cap

    async def checks(**args):
        return (await dry(server, "modify_mf_sip", {"sip_id": "sip-1", **args}))["checks"]["ok"]

    paused = await dry(server, "modify_mf_sip", {"sip_id": "sip-1", "status": "paused"})
    assert paused["checks"]["ok"] is True and "confirmation_token" in paused
    assert paused["would_send"]["form"] == {"status": "paused"}
    assert any("only pauses the SIP" in w for w in warnings(paused))
    assert await checks(status="active") is False  # resuming a 1000 SIP under a 500 cap
    assert await checks(status="paused", instalments=6) is False  # pausing AND changing is not only pausing
    assert await checks(amount=400) is True  # lowering it under the cap is fine
    assert await checks(amount=600) is False
    sent = (await confirmed(server, "modify_mf_sip", {"sip_id": "sip-1", "status": "paused"}))[1]
    assert tool_json(sent)["sent"] is True


async def test_a_sip_that_cannot_be_read_cannot_be_raised_live_but_can_still_be_paused(mcp_factory, fake_kite):
    fake_kite.on("GET", "/mf/sips/sip-9", status=400, error_type="InputException", message="SIP not found")
    fake_kite.on("PUT", "/mf/sips/sip-9", {"sip_id": "sip-9"})
    server, _, _ = setup(mcp_factory, make_fund(), **LIVE)
    raised = await dry(server, "modify_mf_sip", {"sip_id": "sip-9", "amount": 1500})
    assert raised["checks"]["ok"] is False and "confirmation_token" not in raised
    assert any("cannot be verified" in v for v in violations(raised))
    assert any("SIP sip-9 could not be read from Kite (InputError)" in w for w in warnings(raised))
    paused = await dry(server, "modify_mf_sip", {"sip_id": "sip-9", "status": "paused"})
    assert paused["checks"]["ok"] is True and "current_sip" not in paused
    assert tool_json((await confirmed(server, "modify_mf_sip", {"sip_id": "sip-9", "status": "paused"}))[1])["sent"]


@pytest.mark.parametrize(
    ("name", "args"),
    [("modify_mf_sip", {"sip_id": "sip-1", "status": "paused"}), ("cancel_mf_sip", {"sip_id": "sip-1"})],
)
async def test_a_dead_session_while_reading_the_sip_is_not_reported_as_not_found(mcp_factory, fake_kite, name, args):
    fake_kite.on("GET", "/mf/sips/sip-1", status=403, error_type="TokenException", message="Incorrect api_key or token")
    server, _, rt = setup(mcp_factory, make_fund())
    result = await call(server, name, args)
    assert result.is_error and "Kite rejected the session" in tool_text(result)
    assert "not found" not in tool_text(result) and rt.audit.read() == []


@pytest.mark.parametrize(
    ("record", "args", "expected"),
    [
        ({}, {}, "nothing to modify"),
        ({}, {"instalments": 0}, "instalments must be -1"),
        ({"frequency": "weekly"}, {"instalment_day": 10}, "instalment_day only applies to monthly SIPs, not weekly"),
        ({"frequency": "weekly"}, {"instalment_day": 10, "frequency": "monthly"}, None),
        ({}, {"amount": 750}, "not a multiple of ₹500.00"),
    ],
)
async def test_modify_arguments_are_checked_against_the_sip_and_the_fund(
    mcp_factory, fake_kite, record, args, expected
):
    fake_kite.on("GET", "/mf/sips/sip-1", {**SIP_RECORD, **record})
    server, _, _ = setup(mcp_factory, make_fund())
    result = await call(server, "modify_mf_sip", {"sip_id": "sip-1", **args})
    if expected is None:
        assert not result.is_error
    else:
        assert result.is_error and expected in tool_text(result)


async def test_modify_warns_about_a_cancelled_or_unchanged_sip(mcp_factory, fake_kite):
    fake_kite.on("GET", "/mf/sips/sip-1", {**SIP_RECORD, "status": "CANCELLED"})
    server, _, _ = setup(mcp_factory, make_fund())
    answer = await dry(server, "modify_mf_sip", {"sip_id": "sip-1", "amount": 1500})
    assert any("CANCELLED" in w and "refuse" in w for w in warnings(answer))
    fake_kite.on("GET", "/mf/sips/sip-1", {**SIP_RECORD, "status": "PAUSED"})
    answer = await dry(server, "modify_mf_sip", {"sip_id": "sip-1", "status": "paused"})
    assert any("already PAUSED" in w for w in warnings(answer))


async def test_cancelling_an_order_is_risk_reducing_but_still_confirmed(mcp_factory, fake_kite):
    order = {
        "order_id": "ord-1",
        "status": "OPEN",
        "tradingsymbol": ELSS,
        "transaction_type": "BUY",
        "amount": 5000.0,
        "fund": ELSS_NAME,
    }
    fake_kite.on("GET", "/mf/orders/ord-1", order).on("DELETE", "/mf/orders/ord-1", {"order_id": "ord-1"})
    server, _, rt = setup(mcp_factory, KITE_MCP_MAX_ORDER_VALUE="1", **LIVE)  # a cap that would stop any purchase
    preview = await dry(server, "cancel_mf_order", {"order_id": "ord-1"})
    assert preview["summary"] == f"cancel MF order ord-1 (BUY ₹5,000.00 of {ELSS_NAME}, OPEN)"
    assert preview["would_send"] == {"method": "DELETE", "path": "/mf/orders/ord-1"}
    assert preview["legs"] == [{"instrument": f"MF:{ELSS}", "transaction_type": "BUY"}]
    assert preview["checks"]["ok"] is True and preview["order"] == order
    refused = await call(server, "cancel_mf_order", {"order_id": "ord-1", "dry_run": False})
    assert refused.is_error and "confirmation_token is required" in tool_text(refused)
    assert fake_kite.writes() == []
    args = {"order_id": "ord-1", "dry_run": False, "confirmation_token": preview["confirmation_token"]}
    assert (await dry(server, "cancel_mf_order", args))["sent"] is True
    (request,) = fake_kite.writes()
    assert (request.method, request.url.path, request.content) == ("DELETE", "/mf/orders/ord-1", b"")
    usage = rt.audit.usage()
    assert (usage.orders, usage.value) == (0, Decimal(0))


async def test_cancelling_warns_when_there_is_nothing_to_cancel_or_nothing_found(mcp_factory, fake_kite):
    fake_kite.on("GET", "/mf/orders/ord-2", {"order_id": "ord-2", "status": "COMPLETE", "tradingsymbol": ELSS})
    fake_kite.on("GET", "/mf/orders/ord-3", status=400, error_type="InputException", message="Order not found")
    fake_kite.on("GET", "/mf/sips/sip-1", {**SIP_RECORD, "status": "CANCELLED"})
    server, _, _ = setup(mcp_factory)
    done = await dry(server, "cancel_mf_order", {"order_id": "ord-2"})
    assert any("already COMPLETE" in w for w in warnings(done))
    unknown = await dry(server, "cancel_mf_order", {"order_id": "ord-3"})
    assert unknown["legs"] == [] and unknown["summary"] == "cancel MF order ord-3"
    assert any("order ord-3 could not be read from Kite (InputError)" in w for w in warnings(unknown))
    sip = await dry(server, "cancel_mf_sip", {"sip_id": "sip-1"})
    assert any("already CANCELLED" in w for w in warnings(sip))


async def test_cancelling_a_sip_two_step(mcp_factory, fake_kite):
    fake_kite.on("GET", "/mf/sips/sip-1", SIP_RECORD).on("DELETE", "/mf/sips/sip-1", {"sip_id": "sip-1"})
    server, _, _ = setup(mcp_factory, KITE_MCP_MAX_ORDER_VALUE="1", **LIVE)
    preview, sent = await confirmed(server, "cancel_mf_sip", {"sip_id": "sip-1"})
    assert preview["summary"] == f"cancel SIP sip-1 (₹1,000.00 monthly into {ELSS_NAME}, ACTIVE)"
    assert preview["checks"]["ok"] is True and preview["sip"] == SIP_RECORD
    assert tool_json(sent)["sent"] is True
    (request,) = fake_kite.writes()
    assert (request.method, request.url.path) == ("DELETE", "/mf/sips/sip-1")


async def test_cancels_obey_the_instrument_allow_list_like_every_other_leg(mcp_factory, fake_kite):
    fake_kite.on("GET", "/mf/sips/sip-1", SIP_RECORD)
    server, _, _ = setup(mcp_factory, KITE_MCP_ALLOWED_INSTRUMENTS="NSE:INFY", **LIVE)
    answer = await dry(server, "cancel_mf_sip", {"sip_id": "sip-1"})
    assert answer["checks"]["ok"] is False
    assert f"instrument MF:{ELSS} is not in KITE_MCP_ALLOWED_INSTRUMENTS" in violations(answer)


# --- what Kite says -------------------------------------------------------------------------------------------------


async def test_kite_refusing_the_legacy_route_is_reported_and_audited(mcp_factory, fake_kite):
    fake_kite.on(
        "POST", "/mf/orders", status=400, error_type="InputException", message="Order placement is unavailable"
    )
    server, _, rt = setup(mcp_factory, make_fund(), **LIVE)
    _, sent = await confirmed(server, "place_mf_order", BUY)
    assert sent.is_error
    assert "place_mf_order: Kite error InputException HTTP 400: Order placement is unavailable" in tool_text(sent)
    assert [e["event"] for e in rt.audit.read()][-2:] == ["live_intent", "live_error"]
    assert len(fake_kite.writes()) == 1  # never retried
    usage = rt.audit.usage()
    assert (usage.orders, usage.value) == (0, Decimal(0))  # a refused order does not use up the day's caps


async def test_a_timeout_on_a_live_write_is_outcome_unknown_and_never_retried(mcp_factory, fake_kite):
    def timeout(request):
        raise httpx2.ReadTimeout("slow", request=request)

    fake_kite.on("POST", "/mf/sips", handler=timeout)
    server, _, rt = setup(mcp_factory, make_fund(), **LIVE)
    _, sent = await confirmed(server, "place_mf_sip", SIP)
    assert sent.is_error and "may have been executed" in tool_text(sent)
    assert len(fake_kite.writes()) == 1
    assert [e["event"] for e in rt.audit.read()][-2:] == ["live_intent", "live_unknown"]


async def test_the_kill_switch_stops_mutual_fund_writes_too(mcp_factory, fake_kite):
    server, _, rt = setup(mcp_factory, make_fund(), **LIVE)
    token = (await dry(server, "place_mf_order", BUY))["confirmation_token"]
    await dry(server, "engage_kill_switch", {"reason": "testing the switch"})
    result = await call(server, "place_mf_order", {**BUY, "dry_run": False, "confirmation_token": token})
    assert result.is_error and "kill switch" in tool_text(result)
    assert fake_kite.writes() == [] and rt.settings.kill_switch_file.exists()
