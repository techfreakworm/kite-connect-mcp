"""Tools: margin and charges calculators (https://kite.trade/docs/connect/v3/margins/).

Three computing POSTs. They only calculate: nothing is placed or changed, so they are kind "calc", stay available in
read-only mode, and need no dry run or confirmation. Kite's answer is returned as it comes, next to a short `summary`
of the figures a caller would otherwise add up by hand.

A call takes 1 to MAX_ORDERS orders. Kite documents no limit; 50 keeps a request small and is far above any real
basket. Local checks reject what the calculators cannot answer sensibly (a LIMIT order without a price, a stop order
without a trigger, CNC on a derivative...) with a precise message instead of Kite's generic one, and refuse unknown
fields so that a misspelt `trigger_price` is an error rather than a silently different calculation.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..errors import ToolInputError
from ._base import Registrar
from ._orders_common import margin_order
from .orders import Exchange, OrderType, Product, Side, Variety

MAX_ORDERS = 50
EQUITY_EXCHANGES = frozenset({"NSE", "BSE"})
CHARGE_COMPONENTS = ("brokerage", "transaction_tax", "exchange_turnover_charge", "sebi_turnover_charge", "stamp_duty")

ORDERS_HELP = f"1 to {MAX_ORDERS} orders"
MODE_HELP = "compact: only the totals; omit for the full breakdown"


# --- the orders a caller describes ------------------------------------------------------------------------------------


class _Order(BaseModel):
    """The fields every calculator takes for an order."""

    model_config = ConfigDict(extra="forbid")

    exchange: Annotated[Exchange, Field(description="NSE, BSE (equity), NFO, BFO (F&O), CDS, BCD, MCX")]
    tradingsymbol: Annotated[str, Field(min_length=1, max_length=50, description="e.g. INFY, NIFTY25NOVFUT")]
    transaction_type: Annotated[Side, Field(description="BUY or SELL")]
    quantity: Annotated[int, Field(ge=1, description="units (NFO/BFO: a multiple of the lot size)")]
    product: Annotated[Product, Field(description="CNC delivery, MIS intraday, NRML F&O carry, MTF margin")]
    order_type: Annotated[OrderType, Field(description="MARKET, LIMIT, SL (stop-limit), SL-M (stop-market)")]
    variety: Annotated[Variety, Field(description="regular (default), amo, co, iceberg, auction")] = "regular"


class MarginOrder(_Order):
    """One order for get_order_margins and get_basket_margins."""

    price: Annotated[
        float,
        Field(ge=0, allow_inf_nan=False, description="limit price in INR (LIMIT, SL); leave 0 for MARKET and SL-M"),
    ] = 0
    trigger_price: Annotated[
        float,
        Field(ge=0, allow_inf_nan=False, description="trigger price in INR (SL, SL-M; the stop-loss of a co order)"),
    ] = 0


class ChargeOrder(_Order):
    """One executed (or imaginary) order for get_order_charges."""

    average_price: Annotated[
        float,
        Field(gt=0, allow_inf_nan=False, description="price the order was (or would be) executed at, INR; above 0"),
    ]
    order_id: Annotated[
        str | None,
        Field(
            max_length=64, description="any string (it only labels the answer); omitted ones become order-1, order-2..."
        ),
    ] = None


def _who(n: int, order: _Order) -> str:
    return f"order {n} ({order.transaction_type} {order.exchange}:{order.tradingsymbol.strip().upper()})"


def _shared_problems(n: int, order: _Order) -> list[str]:
    if order.product == "CNC" and order.exchange not in EQUITY_EXCHANGES:
        return [f"{_who(n, order)}: CNC is for equity delivery; use NRML or MIS on {order.exchange}"]
    return []


def check_margin_orders(orders: Sequence[MarginOrder]) -> None:
    """Raise ToolInputError for orders the margin calculators cannot answer; nothing is sent."""
    problems: list[str] = []
    for n, order in enumerate(orders, 1):
        who = _who(n, order)
        problems += _shared_problems(n, order)
        if order.order_type in ("LIMIT", "SL") and order.price <= 0:
            problems.append(f"{who}: {order.order_type} orders need a price above 0")
        if order.order_type in ("SL", "SL-M") and order.trigger_price <= 0:
            problems.append(f"{who}: {order.order_type} orders need a trigger_price above 0")
        if order.variety == "co" and order.trigger_price <= 0:
            problems.append(f"{who}: cover orders (co) need trigger_price (the stop-loss)")
        if order.order_type == "MARKET" and order.price > 0:
            problems.append(f"{who}: MARKET orders take no price (use LIMIT to set one)")
    if problems:
        raise ToolInputError("; ".join(problems))


def check_charge_orders(orders: Sequence[ChargeOrder]) -> None:
    problems = [p for n, order in enumerate(orders, 1) for p in _shared_problems(n, order)]
    if problems:
        raise ToolInputError("; ".join(problems))


# --- the request bodies -----------------------------------------------------------------------------------------------


def margin_body(orders: Sequence[MarginOrder]) -> list[dict[str, Any]]:
    """The JSON list POST /margins/orders and /margins/basket take: price and trigger_price are always present."""
    return [
        margin_order(
            exchange=o.exchange,
            tradingsymbol=o.tradingsymbol.strip().upper(),
            transaction_type=o.transaction_type,
            variety=o.variety,
            product=o.product,
            order_type=o.order_type,
            quantity=o.quantity,
            price=o.price,
            trigger_price=o.trigger_price,
        )
        for o in orders
    ]


def charges_body(orders: Sequence[ChargeOrder]) -> list[dict[str, Any]]:
    """The JSON list POST /charges/orders takes (Kite wants an order_id on each; any string will do)."""
    return [
        {
            "order_id": o.order_id or f"order-{n}",
            "exchange": o.exchange,
            "tradingsymbol": o.tradingsymbol.strip().upper(),
            "transaction_type": o.transaction_type,
            "variety": o.variety,
            "product": o.product,
            "order_type": o.order_type,
            "quantity": o.quantity,
            "average_price": o.average_price,
        }
        for n, o in enumerate(orders, 1)
    ]


# --- the summaries ----------------------------------------------------------------------------------------------------


def money(value: Decimal, places: int = 2) -> Decimal:
    return value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def num(value: Any) -> Decimal | None:
    """Kite's number as a Decimal; None for anything that is not a finite number."""
    if value is None or isinstance(value, bool):
        return None
    try:
        out = Decimal(str(value))
    except InvalidOperation:
        return None
    return out if out.is_finite() else None


def _sum(values: Iterable[Any]) -> Decimal:
    return sum((n for n in map(num, values) if n is not None), Decimal(0))


def _total(block: Any) -> Decimal | None:
    return num(block.get("total")) if isinstance(block, dict) else None


def _gst_total(charges: dict[str, Any]) -> Any:
    gst = charges.get("gst")
    return gst.get("total") if isinstance(gst, dict) else None


def charge_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Total charges and the total per component over the entries that carry a `charges` block ({} if none does).
    Compact mode leaves the block out; the transaction tax is STT or CTT depending on the instrument."""
    blocks = [r["charges"] for r in rows if isinstance(r.get("charges"), dict)]
    if not blocks:
        return {}
    parts = {name: money(_sum(b.get(name) for b in blocks), 4) for name in CHARGE_COMPONENTS}
    parts["gst"] = money(_sum(_gst_total(b) for b in blocks), 4)
    return {"total_charges_inr": money(_sum(b.get("total") for b in blocks)), "charges_by_component_inr": parts}


def order_margins_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "orders": len(rows),
        "sum_of_order_margins_inr": money(_sum(r.get("total") for r in rows)),
        "note": "each order is priced on its own; use get_basket_margins for the margin of the orders together",
        **charge_summary(rows),
    }


def basket_summary(data: dict[str, Any]) -> dict[str, Any]:
    rows = [r for r in data.get("orders") or [] if isinstance(r, dict)]
    summary: dict[str, Any] = {"orders": len(rows)}
    initial, final = _total(data.get("initial")), _total(data.get("final"))
    if initial is not None:
        summary["initial_margin_inr"] = money(initial)
    if final is not None:
        summary["final_margin_inr"] = money(final)
    if initial is not None and final is not None:
        summary["spread_benefit_inr"] = money(initial - final)
    # Kite's own overall charges block can omit the transaction tax (STT vs CTT in mixed baskets): sum the orders'
    summary.update(charge_summary(rows))
    return summary


def _ident(order: dict[str, Any]) -> tuple[Any, Any, Any]:
    return order.get("exchange"), order.get("tradingsymbol"), order.get("transaction_type")


def per_order_charges(sent: list[dict[str, Any]], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One line per order. Kite does not echo order_id, so ids are attached by position, and only when the answer
    lines up with the request (same count, same instrument and side at every position)."""
    aligned = len(sent) == len(rows) and all(_ident(s) == _ident(r) for s, r in zip(sent, rows, strict=True))
    lines: list[dict[str, Any]] = []
    for i, row in enumerate(rows):
        total = _total(row.get("charges"))
        line: dict[str, Any] = {
            "order_id": sent[i]["order_id"] if aligned else None,
            "instrument": f"{row.get('exchange')}:{row.get('tradingsymbol')}",
            "transaction_type": row.get("transaction_type"),
            "quantity": row.get("quantity"),
            "price": row.get("price"),
            "total_charges_inr": money(total) if total is not None else None,
        }
        lines.append({k: v for k, v in line.items() if v is not None})
    return lines


def with_summary(summary: dict[str, Any], data: Any, key: str) -> dict[str, Any]:
    """Kite's answer (a dict: under its own keys; a list: under `key`) next to our summary."""
    if isinstance(data, dict):
        return {"summary": summary, **data}
    return {"summary": summary, key: data}


def _rows(data: Any) -> list[dict[str, Any]]:
    return [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []


# --- the tools --------------------------------------------------------------------------------------------------------


def register(reg: Registrar) -> None:
    rt = reg.rt

    @reg.tool("get_order_margins", title="Margin required for orders", toolset="margins", kind="calc")
    async def get_order_margins(
        orders: Annotated[list[MarginOrder], Field(min_length=1, max_length=MAX_ORDERS, description=ORDERS_HELP)],
        mode: Annotated[Literal["compact"] | None, Field(description=MODE_HELP)] = None,
    ) -> Any:
        """Margin Kite would block for each of 1 to 50 orders, without placing anything (works in read-only mode too).

        Every order is priced on its own, taking your existing positions and open orders into account; to price
        orders as one package (hedge or spread benefit) use get_basket_margins. All amounts are INR. Each entry has
        type (equity or commodity), span, exposure, option_premium, additional, bo, cash, var, pnl, leverage, charges
        and total, the margin to block. mode=compact returns the totals only. Leave price at 0 for MARKET and SL-M
        orders. `summary` adds up the totals and the charges."""
        check_margin_orders(orders)
        data = await rt.client.calc("/margins/orders", margin_body(orders), params={"mode": mode} if mode else None)
        return with_summary(order_margins_summary(_rows(data)), data, "orders")

    @reg.tool("get_basket_margins", title="Margin required for a basket of orders", toolset="margins", kind="calc")
    async def get_basket_margins(
        orders: Annotated[list[MarginOrder], Field(min_length=1, max_length=MAX_ORDERS, description=ORDERS_HELP)],
        consider_positions: Annotated[
            bool, Field(description="true (default): net the basket against your open positions; false: price it alone")
        ] = True,
        mode: Annotated[Literal["compact"] | None, Field(description=MODE_HELP)] = None,
    ) -> Any:
        """Margin for 1 to 50 orders taken together as one basket, without placing anything (also in read-only mode).

        `initial` is the total margin required to execute the orders, `final` the total margin with the spread
        (hedge) benefit, `orders` the per-order margins, and `charges` Kite's overall charges block, which can leave
        out the transaction tax when MCX and equity orders are mixed: use the per-order charges instead. All amounts
        are INR. mode=compact returns the totals only. `summary` gives the initial and final margin, the spread
        benefit (initial minus final) and the summed per-order charges."""
        check_margin_orders(orders)
        params: list[tuple[str, Any]] = [("consider_positions", consider_positions)]
        if mode:
            params.append(("mode", mode))
        data = await rt.client.calc("/margins/basket", margin_body(orders), params=params)
        summary = basket_summary(data if isinstance(data, dict) else {})
        return with_summary(summary, data, "margins")

    @reg.tool("get_order_charges", title="Charges for orders (virtual contract note)", toolset="margins", kind="calc")
    async def get_order_charges(
        orders: Annotated[list[ChargeOrder], Field(min_length=1, max_length=MAX_ORDERS, description=ORDERS_HELP)],
    ) -> Any:
        """Charges Kite would levy on 1 to 50 executed or imaginary orders (a virtual contract note), without
        placing anything (works in read-only mode too): brokerage, transaction tax (STT, or CTT on commodities),
        exchange and SEBI turnover charges, stamp duty and GST, all INR.

        Give each order's average_price (the price it was or would be executed at; must be above 0); order_id can
        be any string. Each entry in `orders` is Kite's answer for one order: the order's fields, the price used and
        a `charges` block (total plus the breakdown). `summary` has the grand total, the total per component and a
        line per order."""
        check_charge_orders(orders)
        sent = charges_body(orders)
        data = await rt.client.calc("/charges/orders", sent)
        rows = _rows(data)
        summary = {"orders": len(rows), **charge_summary(rows), "per_order": per_order_charges(sent, rows)}
        return with_summary(summary, data, "orders")
