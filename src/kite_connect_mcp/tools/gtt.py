"""Tools: GTT, "good till triggered" orders (https://kite.trade/docs/connect/v3/gtt/).

A GTT is a trigger that Kite watches for you: when the instrument's price reaches a trigger value, Kite places a LIMIT
order. `single` has one trigger value and one order. `two-leg` is an OCO (one cancels other): two trigger values and two
orders, and only the order whose trigger is reached first is placed. The order at index i is the one placed when
trigger_values[i] is reached.

How the safety policy sees a GTT:
    place    takes one slot of the daily order count. Every order is a leg, checked on its own against the per-order cap
             and the allow-lists. The day's value grows by the LARGEST leg, because only one leg of an OCO can ever run.
    modify   takes no new slot. Only the INCREASE over the stored trigger's value counts toward the day's value.
             The request is marked as counting (with order_count=0) because the policy only checks, and the audit log
             only records, the day's value for requests that count.
    delete   risk-reducing: no caps. The allow-lists (taken from the stored orders) and the confirmation step apply.

Values are quantity x price of the LIMIT orders. MCX, CDS and BCD quantities are lots with a multiplier that is not in
the instrument dump, so those GTTs can be dry-run but not sent live (estimate_leg cannot value them).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..errors import KiteMCPError, ToolInputError
from ..safety.pipeline import WriteRequest
from ..safety.policy import OrderLeg
from ._base import Registrar, Runtime, paginate
from ._orders_common import estimate_leg, instrument_problems, margin_order, margins_preview, validate_order
from ._orders_common import last_price as fetch_ltp
from .orders import CONFIRM, DRY_RUN, Exchange, Product, Side

GttType = Literal["single", "two-leg"]
GttStatus = Literal["active", "triggered", "disabled", "expired", "cancelled", "rejected", "deleted"]
TRIGGER_COUNT = {"single": 1, "two-leg": 2}
LTP_TOLERANCE = Decimal("0.02")  # last_price further than this from the live price is flagged

TriggerId = Annotated[int, Field(ge=1, description="the GTT's trigger_id (see get_gtts)")]
GttTypeArg = Annotated[
    GttType, Field(description="single: one trigger, one order. two-leg: OCO, two triggers, two orders")
]
ExchangeArg = Annotated[
    Exchange, Field(description="exchange of the instrument: NSE, BSE (equity), NFO, BFO (F&O), CDS, BCD, MCX")
]
SymbolArg = Annotated[str, Field(min_length=1, max_length=50, description="tradingsymbol, e.g. INFY")]
TriggersArg = Annotated[
    list[Annotated[float, Field(gt=0, allow_inf_nan=False)]],
    Field(
        min_length=1,
        max_length=2,
        description="trigger prices in INR, on the tick size: ONE value for single; TWO for two-leg, the lower "
        "first (trigger_values[i] fires orders[i])",
    ),
]
LastPriceArg = Annotated[
    float,
    Field(
        gt=0,
        allow_inf_nan=False,
        description="the instrument's CURRENT price in INR (get_ltp / get_quotes / get_holdings). Kite uses it to "
        "decide which way each trigger fires, so it must be fresh",
    ),
]


class GttOrder(BaseModel):
    """One order a GTT places when its trigger value is reached. The exchange and tradingsymbol are the GTT's own."""

    model_config = ConfigDict(extra="forbid")

    transaction_type: Annotated[Side, Field(description="BUY or SELL")]
    quantity: Annotated[int, Field(ge=1, description="units to transact (NFO/BFO: a multiple of the lot size)")]
    price: Annotated[float, Field(gt=0, allow_inf_nan=False, description="LIMIT price in INR, on the tick size")]
    product: Annotated[Product, Field(description="CNC delivery, NRML F&O carry, MIS intraday, MTF margin")]
    order_type: Annotated[Literal["LIMIT"], Field(description="LIMIT is the only type Kite documents for GTTs")] = (
        "LIMIT"
    )


OrdersArg = Annotated[
    list[GttOrder],
    Field(min_length=1, max_length=2, description="the order(s) to place: one for single, two for two-leg"),
]


@dataclass(frozen=True)
class GttSpec:
    """What the caller asked for, normalised (upper-case exchange and symbol)."""

    kind: str
    exchange: str
    symbol: str
    triggers: list[float]
    last_price: float
    orders: list[GttOrder]


@dataclass
class Built:
    """A GTT request ready for the pipeline: the form body, one leg per order, warnings and a one-line summary."""

    data: dict[str, str]
    legs: list[OrderLeg]
    warnings: list[str]
    summary: str


def num(value: float | Decimal | str) -> str:
    """A number as plain text without float noise: 702.0 -> '702', 1499.95 -> '1499.95'."""
    return format(Decimal(str(value)).normalize(), "f")


def dumps(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"))


def check_shape(spec: GttSpec) -> None:
    """Raise ToolInputError for a GTT Kite would certainly reject.

    The docs fix the counts (single: one trigger value; two-leg: two) and pair trigger_values[i] with orders[i]. They
    show an OCO only as 702.0 < last_price 742.0 < 798.0: the lower trigger first and the price between the two, which
    is what an OCO means (a stop-loss side and a target side). The docs do not state that rule in words.
    """
    kind, triggers, last = spec.kind, spec.triggers, spec.last_price
    want = TRIGGER_COUNT[kind]
    problems: list[str] = []
    if not spec.symbol:
        problems.append("tradingsymbol must not be blank")
    if len(triggers) != want:
        problems.append(f"a {kind} GTT takes exactly {want} trigger value(s), not {len(triggers)}")
    if len(spec.orders) != want:
        problems.append(
            f"a {kind} GTT takes exactly {want} order(s), not {len(spec.orders)} (orders[i] is placed when "
            "trigger_values[i] is reached)"
        )
    if not problems and kind == "single" and triggers[0] == last:
        problems.append(f"the trigger value {num(triggers[0])} equals last_price: it must be above or below it")
    if not problems and kind == "two-leg" and not triggers[0] < last < triggers[1]:
        problems.append(
            "a two-leg (OCO) GTT needs the lower trigger first and last_price between the two triggers (the docs' "
            f"example is [702.0, 798.0] around 742.0); got {[float(t) for t in triggers]} around {last}"
        )
    if problems:
        raise ToolInputError("; ".join(problems))


async def instrument_checks(rt: Runtime, spec: GttSpec) -> list[str]:
    """Tick size (order price and trigger) and F&O lot size from the instrument master. Errors raise; warnings are
    returned."""
    errors: list[str] = []
    warnings: list[str] = []
    for trigger, order in zip(spec.triggers, spec.orders, strict=True):
        found, notes = await instrument_problems(rt, spec.exchange, spec.symbol, order.quantity, [order.price, trigger])
        errors += found
        warnings += notes
    if errors:
        raise ToolInputError("; ".join(dict.fromkeys(errors)))
    return warnings


async def ltp_warnings(rt: Runtime, spec: GttSpec) -> list[str]:
    """Compare last_price with the live price (needs market data). A stale or invented last_price makes Kite read the
    direction of a trigger wrongly, so a trigger could fire at once. Warnings only: the live price is not always
    known."""
    ltp, why = await fetch_ltp(rt, spec.exchange, spec.symbol)
    if ltp is None:  # the helper's reason ends with advice for order sizing ("; use a LIMIT order ..."): not ours
        return [f"last_price was not compared with the live price: {why.split(';')[0]}"]
    last = spec.last_price
    given = Decimal(str(last))
    out: list[str] = []
    if abs(ltp - given) > ltp * LTP_TOLERANCE:
        out.append(
            f"last_price {num(last)} is more than 2% away from the live price {num(ltp)}: Kite uses it to decide which "
            "way each trigger fires, so pass a fresh price"
        )
    for trigger in spec.triggers:
        value = Decimal(str(trigger))
        if (value - given) * (value - ltp) <= 0:
            out.append(
                f"trigger {num(trigger)} lies on the other side of the live price {num(ltp)} than last_price "
                f"{num(last)} implies: it may fire at once"
            )
    return out


def describe(spec: GttSpec) -> str:
    steps = [
        f"on reaching {num(t)}: {o.transaction_type} {o.quantity} LIMIT @ {num(o.price)} ({o.product})"
        for t, o in zip(spec.triggers, spec.orders, strict=True)
    ]
    label = "GTT single" if spec.kind == "single" else "GTT two-leg (OCO, only one executes)"
    return f"{label} on {spec.exchange}:{spec.symbol}: " + "; ".join(steps)


async def build_gtt(rt: Runtime, spec: GttSpec) -> Built:
    """Validate a GTT and turn it into the form Kite takes: type, condition (JSON object), orders (JSON array)."""
    check_shape(spec)
    warnings: list[str] = []
    for order in spec.orders:
        warnings += validate_order(
            variety="regular",
            exchange=spec.exchange,
            order_type="LIMIT",
            product=order.product,
            quantity=order.quantity,
            price=order.price,
            trigger_price=None,
            validity=None,
            validity_ttl=None,
            disclosed_quantity=None,
            iceberg_legs=None,
            iceberg_quantity=None,
            auction_number=None,
            market_protection=None,
            tag=None,
        )
        if order.product == "MIS":
            warnings.append(
                "product MIS is intraday: the broker squares it off at the end of the day, and Kite's GTT docs only "
                "show CNC examples, so a GTT that fires on a later day may be rejected"
            )
    warnings += await instrument_checks(rt, spec)
    legs = [
        await estimate_leg(
            rt,
            exchange=spec.exchange,
            tradingsymbol=spec.symbol,
            transaction_type=o.transaction_type,
            quantity=o.quantity,
            order_type="LIMIT",
            product=o.product,
            variety="regular",
            price=o.price,
            trigger_price=None,
        )
        for o in spec.orders
    ]
    condition = {
        "exchange": spec.exchange,
        "tradingsymbol": spec.symbol,
        "trigger_values": spec.triggers,
        "last_price": spec.last_price,
    }
    placed = [
        {
            "exchange": spec.exchange,
            "tradingsymbol": spec.symbol,
            "transaction_type": o.transaction_type,
            "quantity": o.quantity,
            "order_type": o.order_type,
            "product": o.product,
            "price": o.price,
        }
        for o in spec.orders
    ]
    return Built(
        data={"type": spec.kind, "condition": dumps(condition), "orders": dumps(placed)},
        legs=legs,
        warnings=list(dict.fromkeys(warnings)),
        summary=describe(spec),
    )


async def dry_run_extras(rt: Runtime, spec: GttSpec) -> tuple[dict[str, Any], list[str]]:
    """What only a dry run shows: Kite's margin calculation for the orders and the last_price check."""
    margins = await margins_preview(
        rt,
        [
            margin_order(
                exchange=spec.exchange,
                tradingsymbol=spec.symbol,
                transaction_type=o.transaction_type,
                variety="regular",
                product=o.product,
                order_type="LIMIT",
                quantity=o.quantity,
                price=o.price,
                trigger_price=None,
            )
            for o in spec.orders
        ],
    )
    if spec.kind == "two-leg":
        margins["margins_note"] = "margins are per order; only one order of an OCO can execute"
    return margins, await ltp_warnings(rt, spec)


def daily_value(legs: list[OrderLeg]) -> Decimal | None:
    """What a GTT adds to the day's value: the largest leg.

    Only one order of an OCO can ever execute (the other is dropped), so the sum of the legs would count money that can
    never be spent. Each leg is still checked on its own against the per-order cap by the policy. A single GTT has one
    leg, so this is its value. None when a leg cannot be valued (that leg is already a violation).
    """
    known = [leg.value for leg in legs if leg.value is not None]
    return max(known) if len(known) == len(legs) else None


def increase(new: Decimal | None, old: Decimal | None) -> Decimal | None:
    """What a modification adds to the day's value: the increase over the stored trigger, never negative. If the old
    value is unknown the whole new value counts: that can over-count, never under-count."""
    if new is None:
        return None
    if old is None:
        return new
    return max(Decimal(0), new - old)


# --- the stored trigger (GET /gtt/triggers/:id) ---------------------------------------------------------------------


async def current_gtt(rt: Runtime, trigger_id: int) -> dict[str, Any] | None:
    """The stored GTT, or None if Kite does not know it (or the read failed)."""
    try:
        data = await rt.client.get(f"/gtt/triggers/{trigger_id}")
    except KiteMCPError:
        return None
    return data if isinstance(data, dict) and data.get("orders") else None


def stored_orders(current: dict[str, Any]) -> list[dict[str, Any]]:
    return [o for o in current.get("orders") or [] if isinstance(o, dict)]


def stored_value(current: dict[str, Any]) -> Decimal | None:
    """The largest quantity x price among the stored orders, or None if one cannot be priced."""
    values: list[Decimal] = []
    for order in stored_orders(current):
        try:
            values.append(Decimal(str(order["quantity"])) * Decimal(str(order["price"])))
        except (KeyError, ArithmeticError):
            return None
    return max(values) if values else None


def stored_condition(current: dict[str, Any]) -> dict[str, Any]:
    condition = current.get("condition")
    return condition if isinstance(condition, dict) else {}


def stored_legs(current: dict[str, Any]) -> list[OrderLeg]:
    """The stored orders as legs, so the allow-lists see exactly what the GTT would have placed."""
    condition = stored_condition(current)
    return [
        OrderLeg(
            exchange=o.get("exchange") or condition.get("exchange"),
            tradingsymbol=o.get("tradingsymbol") or condition.get("tradingsymbol"),
            transaction_type=o.get("transaction_type"),
            product=o.get("product"),
            order_type=o.get("order_type"),
            variety="regular",
            quantity=int(o.get("quantity") or 0),
        )
        for o in stored_orders(current)
    ]


def stored_text(current: dict[str, Any]) -> str:
    condition = stored_condition(current)
    orders = "; ".join(
        f"{o.get('transaction_type')} {o.get('quantity')} @ {o.get('price')}" for o in stored_orders(current)
    )
    triggers = ", ".join(str(t) for t in condition.get("trigger_values") or [])
    return (
        f"{current.get('type')} on {condition.get('exchange')}:{condition.get('tradingsymbol')}, "
        f"status {current.get('status')}, trigger {triggers}: {orders}"
    )


def newest_first(trigger: dict[str, Any]) -> tuple[str, int]:
    ident = trigger.get("id")
    return str(trigger.get("created_at") or ""), ident if isinstance(ident, int) else 0


def register(reg: Registrar) -> None:
    rt = reg.rt

    # --- reads ------------------------------------------------------------------------------------------------------

    @reg.tool("get_gtts", title="GTT order book", toolset="gtt", kind="read")
    async def get_gtts(
        status: Annotated[
            GttStatus | None,
            Field(description="only GTTs in this state; omit for all (active, triggered, disabled, expired, ...)"),
        ] = None,
        limit: Annotated[int, Field(ge=1, le=500)] = 100,
        offset: Annotated[int, Field(ge=0)] = 0,
    ) -> Any:
        """The GTT book, newest first: every active GTT and those in other states from the previous 7 days. Each has
        id (the trigger_id), type (single or two-leg), status, created_at / updated_at / expires_at (IST), condition
        (exchange, tradingsymbol, trigger_values, last_price at placement), orders (with `result` once an order was
        placed by the trigger, including the rejection reason if the order failed) and meta."""
        triggers = [t for t in await rt.client.get("/gtt/triggers") or [] if isinstance(t, dict)]
        if status:
            triggers = [t for t in triggers if str(t.get("status", "")).lower() == status]
        return paginate(sorted(triggers, key=newest_first, reverse=True), limit, offset)

    @reg.tool("get_gtt", title="One GTT", toolset="gtt", kind="read")
    async def get_gtt(trigger_id: TriggerId) -> Any:
        """One GTT by its trigger_id, whatever its age or status: type, status, condition, the orders it places and
        the result of the order if the trigger fired."""
        return await rt.client.get(f"/gtt/triggers/{trigger_id}")

    # --- writes -----------------------------------------------------------------------------------------------------

    @reg.tool("place_gtt_order", title="Place a GTT (dry run by default)", toolset="gtt", kind="write")
    async def place_gtt_order(
        type: GttTypeArg,
        exchange: ExchangeArg,
        tradingsymbol: SymbolArg,
        trigger_values: TriggersArg,
        last_price: LastPriceArg,
        orders: OrdersArg,
        dry_run: Annotated[bool, DRY_RUN] = True,
        confirmation_token: Annotated[str | None, CONFIRM] = None,
    ) -> Any:
        """Place a GTT (good till triggered): Kite watches the price and places a LIMIT order when a trigger value is
        reached. DRY RUN BY DEFAULT: with dry_run=true (the default) nothing is sent; the answer shows the exact
        request, the value of each order, Kite's margin calculation, every safety check and, when live trading is
        enabled and the checks pass, a confirmation_token. To create it, repeat the identical call with dry_run=false
        and that token.

        type=single: one trigger value (not equal to last_price) and one order. type=two-leg is an OCO (one cancels
        other): two trigger values, lower first, with last_price between them, and two orders; orders[i] is placed when
        trigger_values[i] is reached and the other is dropped (typically a stop-loss below and a target above).
        last_price is the current price in INR from get_ltp, get_quotes or get_holdings: never guess it. Only LIMIT
        orders are supported; prices must sit on the tick size.

        Safety: a GTT uses one slot of the daily order count. Every order is checked against the per-order cap and the
        allow-lists; for an OCO only one order can execute, so the day's value grows by the larger order, not the sum.
        MCX, CDS and BCD GTTs cannot be valued and so cannot be sent live. Returns the trigger_id when sent."""
        spec = GttSpec(type, exchange.upper(), tradingsymbol.strip().upper(), trigger_values, last_price, orders)
        built = await build_gtt(rt, spec)
        preview: dict[str, Any] = {}
        if dry_run:
            preview, more = await dry_run_extras(rt, spec)
            built.warnings += more
        request = WriteRequest(
            tool="place_gtt_order",
            method="POST",
            path="/gtt/triggers",
            data=built.data,
            summary=built.summary,
            legs=built.legs,
            counts_as_order=True,
            order_count=1,
            daily_value=daily_value(built.legs),
            preview=preview,
            warnings=built.warnings,
        )
        return await rt.pipeline.run(request, dry_run=dry_run, confirmation_token=confirmation_token)

    @reg.tool("modify_gtt_order", title="Modify a GTT (dry run by default)", toolset="gtt", kind="write")
    async def modify_gtt_order(
        trigger_id: TriggerId,
        type: GttTypeArg,
        exchange: ExchangeArg,
        tradingsymbol: SymbolArg,
        trigger_values: TriggersArg,
        last_price: LastPriceArg,
        orders: OrdersArg,
        dry_run: Annotated[bool, DRY_RUN] = True,
        confirmation_token: Annotated[str | None, CONFIRM] = None,
    ) -> Any:
        """Modify an ACTIVE GTT. Kite replaces the whole trigger, so send its complete new definition: call get_gtt
        first and change only what you mean to change. The fields are those of place_gtt_order, with the same rules.
        DRY RUN BY DEFAULT, confirmed like place_gtt_order.

        Safety: the new orders are checked against the per-order cap and the allow-lists. A modification takes no new
        slot of the daily order count; only the increase over the stored trigger's value counts toward the day's
        value cap. Returns the trigger_id when sent."""
        spec = GttSpec(type, exchange.upper(), tradingsymbol.strip().upper(), trigger_values, last_price, orders)
        built = await build_gtt(rt, spec)
        current = await current_gtt(rt, trigger_id)
        preview: dict[str, Any] = {}
        if current is None:
            built.warnings.append(f"GTT {trigger_id} could not be read: its status and value could not be checked")
        else:
            preview["current_gtt"] = current
            if str(current.get("status", "")).lower() != "active":
                built.warnings.append(f"the GTT's status is {current.get('status')}: Kite only modifies active GTTs")
        if dry_run:
            extras, more = await dry_run_extras(rt, spec)
            preview.update(extras)
            built.warnings += more
        request = WriteRequest(
            tool="modify_gtt_order",
            method="PUT",
            path=f"/gtt/triggers/{trigger_id}",
            data=built.data,
            summary=f"modify GTT {trigger_id}: {built.summary}",
            legs=built.legs,
            # not a new order, but not exempt from the daily value cap either: counts_as_order with order_count=0
            # makes the policy check (and the audit log record) exactly the increase over the stored trigger
            counts_as_order=True,
            order_count=0,
            daily_value=increase(daily_value(built.legs), stored_value(current) if current else None),
            order_id=str(trigger_id),
            preview=preview,
            warnings=built.warnings,
        )
        return await rt.pipeline.run(request, dry_run=dry_run, confirmation_token=confirmation_token)

    @reg.tool("delete_gtt_order", title="Delete a GTT (dry run by default)", toolset="gtt", kind="write")
    async def delete_gtt_order(
        trigger_id: TriggerId,
        dry_run: Annotated[bool, DRY_RUN] = True,
        confirmation_token: Annotated[str | None, CONFIRM] = None,
    ) -> Any:
        """Delete an ACTIVE GTT so it can no longer fire. DRY RUN BY DEFAULT, confirmed like place_gtt_order (deleting
        a stop-loss GTT can increase risk, so it is not exempt). Caps do not apply to deletions; the allow-lists do,
        judged on the orders the GTT would have placed. The dry run shows the stored GTT. Returns the trigger_id
        when sent."""
        current = await current_gtt(rt, trigger_id)
        warnings: list[str] = []
        if current is None:
            warnings.append(f"GTT {trigger_id} could not be read: what it holds cannot be shown or checked")
        elif str(current.get("status", "")).lower() != "active":
            warnings.append(f"the GTT's status is {current.get('status')}: Kite only deletes active GTTs")
        request = WriteRequest(
            tool="delete_gtt_order",
            method="DELETE",
            path=f"/gtt/triggers/{trigger_id}",
            summary=f"delete GTT {trigger_id}" + (f" ({stored_text(current)})" if current else ""),
            legs=stored_legs(current) if current else [],
            risk_reducing=True,
            order_id=str(trigger_id),
            preview={"current_gtt": current} if current else {},
            warnings=warnings,
        )
        return await rt.pipeline.run(request, dry_run=dry_run, confirmation_token=confirmation_token)
