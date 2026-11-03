"""Shared pre-trade logic for every tool that creates or changes an order: orders, GTTs, ATO alerts, margins.

    validate_order(...)      the rules from https://kite.trade/docs/connect/v3/orders/ that can be checked locally
    estimate_leg(...)        an OrderLeg for the safety policy: the notional value and how it was computed
    instrument_problems(...) tick size and lot size from the instrument master (when it is available)
    margins_preview(...)     Kite's own margin and charges calculation for the order(s) (a read-only POST)

Value rules (what the per-order and daily caps are checked against):
    LIMIT / SL           quantity x price
    SL-M                 quantity x trigger_price x (1 + buffer)
    MARKET               quantity x LTP x (1 + buffer); LTP from the quote API (needs the paid Connect plan)
    buffer               market_protection% when given (> 0), else 5% (auto protection or none)
    MCX, CDS, BCD        quantity is in lots and the contract multiplier is not in the instrument dump, so the value
                         cannot be verified: such orders can be dry-run but are refused live
"""

from __future__ import annotations

import logging
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from ..errors import KiteAPIError, KiteMCPError, ToolInputError
from ..safety.policy import OrderLeg
from ._base import Runtime

log = logging.getLogger(__name__)

EXCHANGES = ("NSE", "BSE", "NFO", "BFO", "CDS", "BCD", "MCX")
PRODUCTS = ("CNC", "NRML", "MIS", "MTF")
ORDER_TYPES = ("MARKET", "LIMIT", "SL", "SL-M")
VARIETIES = ("regular", "amo", "co", "iceberg", "auction")
VALIDITIES = ("DAY", "IOC", "TTL")
TRANSACTION_TYPES = ("BUY", "SELL")
LOT_EXCHANGES = frozenset({"MCX", "CDS", "BCD"})
DEFAULT_BUFFER = Decimal("0.05")


def dec(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        out = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ToolInputError(f"{value!r} is not a number") from None
    if not out.is_finite():
        raise ToolInputError(f"{value!r} is not a finite number")
    return out


def validate_order(
    *,
    variety: str,
    exchange: str,
    order_type: str,
    product: str,
    quantity: int,
    price: float | None,
    trigger_price: float | None,
    validity: str | None,
    validity_ttl: int | None,
    disclosed_quantity: int | None,
    iceberg_legs: int | None,
    iceberg_quantity: int | None,
    auction_number: str | None,
    market_protection: float | None,
    tag: str | None,
) -> list[str]:
    """Raise ToolInputError for a request Kite would certainly reject; return warnings for doubtful ones."""
    warnings: list[str] = []
    problems: list[str] = []
    if quantity < 1:
        problems.append("quantity must be at least 1")
    if order_type in ("LIMIT", "SL") and (price is None or price <= 0):
        problems.append(f"{order_type} orders need a price above 0")
    if order_type in ("SL", "SL-M") and (trigger_price is None or trigger_price <= 0):
        problems.append(f"{order_type} orders need a trigger_price above 0")
    if order_type == "MARKET" and price not in (None, 0):
        problems.append("MARKET orders take no price (use LIMIT to set one)")
    if validity == "TTL" and not validity_ttl:
        problems.append("validity TTL needs validity_ttl (minutes)")
    if validity_ttl and validity != "TTL":
        problems.append("validity_ttl is only for validity=TTL")
    if variety == "iceberg":
        if not iceberg_legs or not 2 <= iceberg_legs <= 50:
            problems.append("iceberg orders need iceberg_legs between 2 and 50")
        if not iceberg_quantity:
            problems.append("iceberg orders need iceberg_quantity (quantity per leg)")
    elif iceberg_legs or iceberg_quantity:
        problems.append("iceberg_legs / iceberg_quantity are only for variety=iceberg")
    if variety == "auction" and not auction_number:
        problems.append("auction orders need auction_number (see get_auctions)")
    if auction_number and variety != "auction":
        problems.append("auction_number is only for variety=auction")
    if variety == "co":
        if order_type not in ("MARKET", "LIMIT"):
            problems.append("cover orders (co) are MARKET or LIMIT, with a stop-loss trigger_price")
        if trigger_price is None or trigger_price <= 0:
            problems.append("cover orders (co) need trigger_price (the stop-loss)")
        if product != "MIS":
            warnings.append("cover orders are intraday: Kite expects product MIS")
    if market_protection is not None:
        if order_type not in ("MARKET", "SL-M"):
            problems.append("market_protection only applies to MARKET and SL-M orders")
        elif not (market_protection == -1 or 0 < market_protection <= 100):
            problems.append("market_protection must be -1 (automatic) or above 0 and up to 100 (percent)")
    elif order_type in ("MARKET", "SL-M"):
        warnings.append("no market_protection given: Kite's documented examples pass -1 (automatic protection)")
    if disclosed_quantity is not None and disclosed_quantity > quantity:
        problems.append("disclosed_quantity cannot exceed quantity")
    if tag is not None and (len(tag) > 20 or not tag.replace("_", "").replace("-", "").isalnum()):
        problems.append("tag must be alphanumeric, at most 20 characters")
    if exchange in ("NFO", "BFO", "MCX", "CDS", "BCD") and product == "CNC":
        problems.append(f"CNC is for equity delivery; use NRML or MIS on {exchange}")
    if problems:
        raise ToolInputError("; ".join(problems))
    return warnings


async def last_price(rt: Runtime, exchange: str, tradingsymbol: str) -> tuple[Decimal | None, str]:
    """The LTP from Kite's quote API, or (None, why not)."""
    key = f"{exchange}:{tradingsymbol}"
    try:
        data = await rt.client.get("/quote/ltp", params=[("i", key)])
    except KiteAPIError as exc:
        if exc.error_type == "PermissionException":
            return None, "no market data on this Kite plan (Personal); use a LIMIT order so the value is known"
        return None, f"the LTP could not be fetched ({exc.error_type})"
    except KiteMCPError as exc:
        return None, f"the LTP could not be fetched ({type(exc).__name__})"
    entry = (data or {}).get(key) if isinstance(data, dict) else None
    if not entry or entry.get("last_price") is None:
        return None, f"Kite returned no LTP for {key} (unknown instrument?)"
    return dec(entry["last_price"]), "LTP"


async def estimate_leg(
    rt: Runtime,
    *,
    exchange: str,
    tradingsymbol: str,
    transaction_type: str,
    quantity: int,
    order_type: str,
    product: str | None,
    variety: str | None,
    price: float | None,
    trigger_price: float | None,
    market_protection: float | None = None,
) -> OrderLeg:
    common: dict[str, Any] = {
        "exchange": exchange,
        "tradingsymbol": tradingsymbol,
        "transaction_type": transaction_type,
        "product": product,
        "order_type": order_type,
        "variety": variety,
        "quantity": quantity,
    }
    if exchange in LOT_EXCHANGES:
        return OrderLeg(
            **common,
            value=None,
            value_note=f"{exchange} quantities are lots with a contract multiplier "
            "that is not in the instrument dump, so the value cannot be checked against the caps",
        )
    buffer = Decimal(str(market_protection)) / 100 if market_protection and market_protection > 0 else DEFAULT_BUFFER
    qty = Decimal(quantity)
    if order_type in ("LIMIT", "SL"):
        basis = dec(price)
        assert basis is not None
        return OrderLeg(**common, price=basis, value=_inr(qty * basis), value_note="quantity x price")
    if order_type == "SL-M":
        basis = dec(trigger_price)
        assert basis is not None
        return OrderLeg(
            **common,
            price=basis,
            value=_inr(qty * basis * (1 + buffer)),
            value_note=f"quantity x trigger_price x (1 + {buffer:.0%} buffer)",
        )
    ltp, note = await last_price(rt, exchange, tradingsymbol)
    if ltp is None:
        return OrderLeg(**common, value=None, value_note=note)
    return OrderLeg(
        **common,
        price=ltp,
        value=_inr(qty * ltp * (1 + buffer)),
        value_note=f"quantity x LTP x (1 + {buffer:.0%} buffer)",
    )


def _inr(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


async def instrument_problems(
    rt: Runtime, exchange: str, tradingsymbol: str, quantity: int, prices: list[float | None]
) -> tuple[list[str], list[str]]:
    """(errors, warnings) from the instrument master: unknown symbol, price off the tick, quantity off the lot."""
    store = rt.instruments
    if store is None:
        return [], ["the instrument master is not loaded: tick size and lot size were not checked"]
    try:
        inst = await store.lookup(exchange, tradingsymbol)
    except KiteMCPError as exc:
        return [], [f"the instrument master could not be read ({type(exc).__name__}): tick/lot size not checked"]
    if inst is None:
        return [f"{exchange}:{tradingsymbol} is not in today's instrument list (use search_instruments)"], []
    errors: list[str] = []
    tick = inst.tick_size
    for p in prices:
        if p is None or p == 0 or not tick:
            continue
        value = dec(p)
        assert value is not None
        if (value / tick) % 1 != 0:
            errors.append(f"price {p} is not a multiple of the tick size {tick} for {inst.key}")
    if exchange in ("NFO", "BFO") and inst.lot_size > 1 and quantity % inst.lot_size:
        errors.append(f"quantity {quantity} is not a multiple of the lot size {inst.lot_size} for {inst.key}")
    return errors, []


async def margins_preview(rt: Runtime, orders: list[dict[str, Any]], *, basket: bool = False) -> dict[str, Any]:
    """Kite's margin and charges calculation for these orders, or {"margins_error": ...}. Never raises."""
    path = "/margins/basket" if basket else "/margins/orders"
    params = [("consider_positions", "true"), ("mode", "compact")] if basket else [("mode", "compact")]
    try:
        return {"margins": await rt.client.calc(path, orders, params=params)}
    except KiteMCPError as exc:
        return {"margins_error": rt.client.scrub(str(exc))[:300]}


def margin_order(
    *,
    exchange: str,
    tradingsymbol: str,
    transaction_type: str,
    variety: str,
    product: str,
    order_type: str,
    quantity: int,
    price: float | None,
    trigger_price: float | None,
) -> dict[str, Any]:
    """One order in the JSON shape the margin calculator takes."""
    return {
        "exchange": exchange,
        "tradingsymbol": tradingsymbol,
        "transaction_type": transaction_type,
        "variety": variety,
        "product": product,
        "order_type": order_type,
        "quantity": quantity,
        "price": price or 0,
        "trigger_price": trigger_price or 0,
    }


def describe_order(
    transaction_type: str,
    quantity: int,
    exchange: str,
    tradingsymbol: str,
    order_type: str,
    price: float | None,
    trigger_price: float | None,
    product: str,
    variety: str,
) -> str:
    text = f"{transaction_type} {quantity} {exchange}:{tradingsymbol} {order_type}"
    if price:
        text += f" @ {price}"
    if trigger_price:
        text += f" trigger {trigger_price}"
    return f"{text} ({product}, {variety})"
