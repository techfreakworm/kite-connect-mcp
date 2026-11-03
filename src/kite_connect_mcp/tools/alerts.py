"""Tools: price alerts and ATO (alert triggers order) alerts (https://kite.trade/docs/connect/v3/alerts/).

A simple alert only notifies. An ATO alert also carries a basket of orders that Kite places by itself when the alert's
condition becomes true: the confirmation given when the alert is created is the only one those orders will ever get.
Kite allows at most 500 active alerts per user.

How the safety policy sees an alert:
    simple   no orders, so no legs and no caps; still a confirmed write.
    ato      every basket order is a leg (per-order cap, allow-lists). All of them are placed on a trigger, so unlike an
             OCO GTT the values ADD UP, and the alert uses as many slots of the daily order count as it has orders.
    modify   takes no new slot of the order count, but the full value of the new basket counts toward the day's value
             (the stored basket is not netted off: Kite stores it in another shape, and MARKET legs have no price).
    delete   risk-reducing and legless: never blocked by a cap or an allow-list, always confirmed.

The `basket` form field is a JSON object laid out exactly like the sample in the docs (name, type "alert", tags, items
with type "insert", weight and the order params), with the unused params sent as the zeros the sample shows.
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
from ._orders_common import (
    describe_order,
    estimate_leg,
    instrument_problems,
    margin_order,
    margins_preview,
    validate_order,
)
from .orders import CONFIRM, DRY_RUN, Exchange, OrderType, Product, Side, Validity

AlertExchange = Literal["NSE", "BSE", "NFO", "CDS", "BCD", "MCX", "INDICES"]  # the docs' list for lhs_exchange
Attribute = Literal["LastTradedPrice"]  # the only attribute the docs name ("e.g., LastTradedPrice")
Operator = Literal["<=", ">=", "<", ">", "=="]
AlertType = Literal["simple", "ato"]
RhsType = Literal["constant", "instrument"]
AlertStatus = Literal["enabled", "disabled", "deleted"]

UUID_PATTERN = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
AlertUuid = Annotated[str, Field(pattern=UUID_PATTERN, description="the alert's uuid (see get_alerts)")]
BASKET_NAME = "alerts-basket"  # the name the docs' sample uses

NameArg = Annotated[str, Field(min_length=1, max_length=100, description="a label for the alert")]
TypeArg = Annotated[
    AlertType,
    Field(description="simple: only notify. ato: also place the basket `orders` by itself when the condition is true"),
]
LhsExchangeArg = Annotated[
    AlertExchange,
    Field(description="exchange of the monitored instrument: NSE, BSE, NFO, CDS, BCD, MCX, or INDICES for indices"),
]
LhsSymbolArg = Annotated[
    str, Field(min_length=1, max_length=50, description="tradingsymbol of the monitored instrument: INFY, 'NIFTY 50'")
]
LhsAttributeArg = Annotated[
    Attribute, Field(description="what to monitor; LastTradedPrice is the only attribute Kite documents")
]
OperatorArg = Annotated[Operator, Field(description="comparison of the monitored value with the right-hand side")]
RhsTypeArg = Annotated[
    RhsType,
    Field(description="constant: compare with rhs_constant. instrument: compare with another instrument (rhs_*)"),
]
RhsConstantArg = Annotated[
    float | None,
    Field(allow_inf_nan=False, description="the number to compare with (INR for prices); only for rhs_type=constant"),
]
RhsExchangeArg = Annotated[AlertExchange | None, Field(description="only for rhs_type=instrument")]
RhsSymbolArg = Annotated[str | None, Field(max_length=50, description="only for rhs_type=instrument")]
RhsAttributeArg = Annotated[
    Attribute | None, Field(description="only for rhs_type=instrument; defaults to LastTradedPrice")
]


class AlertOrder(BaseModel):
    """One order of an ATO alert's basket. Kite places it as a regular order when the alert's condition is true."""

    model_config = ConfigDict(extra="forbid")

    exchange: Annotated[Exchange, Field(description="NSE, BSE (equity), NFO, BFO (F&O), CDS, BCD, MCX")]
    tradingsymbol: Annotated[str, Field(min_length=1, max_length=50, description="e.g. RELIANCE")]
    transaction_type: Annotated[Side, Field(description="BUY or SELL")]
    quantity: Annotated[int, Field(ge=1, description="units (NFO/BFO: a multiple of the lot size)")]
    product: Annotated[Product, Field(description="CNC delivery, MIS intraday, NRML F&O carry, MTF margin")]
    order_type: Annotated[OrderType, Field(description="MARKET, LIMIT, SL (stop-limit), SL-M (stop-market)")]
    price: Annotated[float | None, Field(gt=0, allow_inf_nan=False, description="limit price in INR (LIMIT, SL)")] = (
        None
    )
    trigger_price: Annotated[
        float | None, Field(gt=0, allow_inf_nan=False, description="trigger price in INR (SL, SL-M)")
    ] = None
    validity: Annotated[Validity, Field(description="DAY (default), IOC, or TTL")] = "DAY"
    validity_ttl: Annotated[int | None, Field(ge=1, le=365, description="minutes, for validity=TTL")] = None
    disclosed_quantity: Annotated[int | None, Field(ge=0, description="quantity shown in the market")] = None
    market_protection: Annotated[
        float | None, Field(description="MARKET / SL-M only: -1 automatic (the default), or a % above 0 up to 100")
    ] = None


OrdersArg = Annotated[
    list[AlertOrder] | None,
    Field(max_length=20, description="the basket of orders: required for type=ato (up to 20), not allowed for simple"),
]


@dataclass(frozen=True)
class AlertSpec:
    """The condition half of an alert, normalised."""

    name: str
    type: str
    lhs_exchange: str
    lhs_tradingsymbol: str
    lhs_attribute: str
    operator: str
    rhs_type: str
    rhs_constant: float | None
    rhs_exchange: str | None
    rhs_tradingsymbol: str | None
    rhs_attribute: str | None

    def check(self, orders: list[AlertOrder]) -> None:
        """Raise ToolInputError for an alert Kite would certainly reject (the parameter rules of the docs)."""
        problems: list[str] = []
        if not self.name or not self.lhs_tradingsymbol:
            problems.append("name and lhs_tradingsymbol must not be blank")
        instrument_fields = (self.rhs_exchange, self.rhs_tradingsymbol)
        if self.rhs_type == "constant":
            if self.rhs_constant is None:
                problems.append("rhs_type=constant needs rhs_constant")
            if any(instrument_fields) or self.rhs_attribute:
                problems.append("rhs_exchange / rhs_tradingsymbol / rhs_attribute are only for rhs_type=instrument")
        else:
            if not all(instrument_fields):
                problems.append("rhs_type=instrument needs rhs_exchange and rhs_tradingsymbol")
            if self.rhs_constant is not None:
                problems.append("rhs_constant is only for rhs_type=constant")
        if self.type == "ato" and not orders:
            problems.append("type=ato needs at least one order in `orders` (the basket)")
        if self.type == "simple" and orders:
            problems.append("`orders` is only for type=ato; a simple alert places no orders")
        if problems:
            raise ToolInputError("; ".join(problems))

    def form(self) -> dict[str, str]:
        data = {
            "name": self.name,
            "type": self.type,
            "lhs_exchange": self.lhs_exchange,
            "lhs_tradingsymbol": self.lhs_tradingsymbol,
            "lhs_attribute": self.lhs_attribute,
            "operator": self.operator,
            "rhs_type": self.rhs_type,
        }
        if self.rhs_type == "constant":
            assert self.rhs_constant is not None
            data["rhs_constant"] = num(self.rhs_constant)
        else:
            data.update(
                rhs_exchange=self.rhs_exchange or "",
                rhs_tradingsymbol=self.rhs_tradingsymbol or "",
                rhs_attribute=self.rhs_attribute or "",
            )
        return data

    def condition_text(self) -> str:
        right = (
            num(self.rhs_constant or 0)
            if self.rhs_type == "constant"
            else f"{self.rhs_exchange}:{self.rhs_tradingsymbol} {self.rhs_attribute}"
        )
        return f"{self.lhs_exchange}:{self.lhs_tradingsymbol} {self.lhs_attribute} {self.operator} {right}"


@dataclass
class Built:
    """An alert request ready for the pipeline: the form body, one leg per basket order, warnings, a summary."""

    data: dict[str, str]
    legs: list[OrderLeg]
    warnings: list[str]
    summary: str
    margin_orders: list[dict[str, Any]]


def num(value: float | Decimal | str) -> str:
    """A number as plain text without float noise: 27000.0 -> '27000', 71.8 -> '71.8'."""
    return format(Decimal(str(value)).normalize(), "f")


def make_spec(
    name: str,
    type: str,
    lhs_exchange: str,
    lhs_tradingsymbol: str,
    lhs_attribute: str,
    operator: str,
    rhs_type: str,
    rhs_constant: float | None,
    rhs_exchange: str | None,
    rhs_tradingsymbol: str | None,
    rhs_attribute: str | None,
) -> AlertSpec:
    """Normalise the arguments (case, spaces, the default right-hand attribute)."""
    by_instrument = rhs_type == "instrument"
    return AlertSpec(
        name=name.strip(),
        type=type,
        lhs_exchange=lhs_exchange,
        lhs_tradingsymbol=lhs_tradingsymbol.strip().upper(),
        lhs_attribute=lhs_attribute,
        operator=operator,
        rhs_type=rhs_type,
        rhs_constant=rhs_constant,
        rhs_exchange=rhs_exchange,
        rhs_tradingsymbol=rhs_tradingsymbol.strip().upper() if rhs_tradingsymbol else None,
        rhs_attribute=rhs_attribute or ("LastTradedPrice" if by_instrument else None),
    )


def protection(order: AlertOrder) -> float | None:
    """The docs' sample sends market_protection -1 (automatic) with its MARKET order, so that is the default for the
    order types it applies to; the other types carry no protection."""
    if order.market_protection is None and order.order_type in ("MARKET", "SL-M"):
        return -1
    return order.market_protection


def basket_item(order: AlertOrder, symbol: str, weight: int) -> dict[str, Any]:
    """One basket item, key for key like the docs' sample (the unused params are the zeros it shows)."""
    return {
        "type": "insert",
        "tradingsymbol": symbol,
        "exchange": order.exchange,
        "weight": weight,
        "params": {
            "transaction_type": order.transaction_type,
            "product": order.product,
            "order_type": order.order_type,
            "validity": order.validity,
            "validity_ttl": order.validity_ttl or 0,
            "quantity": order.quantity,
            "price": order.price or 0,
            "trigger_price": order.trigger_price or 0,
            "disclosed_quantity": order.disclosed_quantity or 0,
            "last_price": 0,
            "variety": "regular",
            "tags": [],
            "squareoff": 0,
            "stoploss": 0,
            "trailing_stoploss": 0,
            "iceberg_legs": 0,
            "market_protection": protection(order) or 0,
        },
    }


async def build_alert(rt: Runtime, spec: AlertSpec, orders: list[AlertOrder]) -> Built:
    """Validate an alert and turn it into Kite's form; an ATO alert's basket becomes the JSON `basket` field."""
    spec.check(orders)
    warnings: list[str] = []
    legs: list[OrderLeg] = []
    margin_orders: list[dict[str, Any]] = []
    items: list[dict[str, Any]] = []
    errors: list[str] = []
    for index, order in enumerate(orders):
        symbol = order.tradingsymbol.strip().upper()
        exchange = order.exchange.upper()
        warnings += validate_order(
            variety="regular",
            exchange=exchange,
            order_type=order.order_type,
            product=order.product,
            quantity=order.quantity,
            price=order.price,
            trigger_price=order.trigger_price,
            validity=order.validity,
            validity_ttl=order.validity_ttl,
            disclosed_quantity=order.disclosed_quantity,
            iceberg_legs=None,
            iceberg_quantity=None,
            auction_number=None,
            market_protection=protection(order),
            tag=None,
        )
        prices = [order.price, order.trigger_price]
        found, notes = await instrument_problems(rt, exchange, symbol, order.quantity, prices)
        errors += found
        warnings += notes
        legs.append(
            await estimate_leg(
                rt,
                exchange=exchange,
                tradingsymbol=symbol,
                transaction_type=order.transaction_type,
                quantity=order.quantity,
                order_type=order.order_type,
                product=order.product,
                variety="regular",
                price=order.price,
                trigger_price=order.trigger_price,
                market_protection=protection(order),
            )
        )
        margin_orders.append(
            margin_order(
                exchange=exchange,
                tradingsymbol=symbol,
                transaction_type=order.transaction_type,
                variety="regular",
                product=order.product,
                order_type=order.order_type,
                quantity=order.quantity,
                price=order.price,
                trigger_price=order.trigger_price,
            )
        )
        items.append(basket_item(order, symbol, 10_000 * (index + 1)))
    if errors:
        raise ToolInputError("; ".join(dict.fromkeys(errors)))
    data = spec.form()
    summary = f"{spec.type} alert '{spec.name}': {spec.condition_text()}"
    if orders:
        data["basket"] = json.dumps(
            {"name": BASKET_NAME, "type": "alert", "tags": [], "items": items}, separators=(",", ":")
        )
        placed = "; ".join(
            describe_order(
                o.transaction_type,
                o.quantity,
                o.exchange,
                o.tradingsymbol.strip().upper(),
                o.order_type,
                o.price,
                o.trigger_price,
                o.product,
                "regular",
            )
            for o in orders
        )
        summary += f" -> when true, Kite places: {placed}"
    return Built(data, legs, list(dict.fromkeys(warnings)), summary, margin_orders)


async def basket_margins(rt: Runtime, built: Built) -> dict[str, Any]:
    """Kite's margin calculation for the basket orders (what they would need when the alert fires); {} for a simple
    alert. One order uses the orders calculator, several the basket calculator."""
    if not built.margin_orders:
        return {}
    return await margins_preview(rt, built.margin_orders, basket=len(built.margin_orders) > 1)


def basket_value(legs: list[OrderLeg]) -> Decimal | None:
    """What an ATO alert adds to the day's value: every basket order is placed on a trigger, so the values add up
    (unlike an OCO GTT, where only one order runs). None when a leg cannot be valued (that leg is a violation)."""
    known = [leg.value for leg in legs if leg.value is not None]
    return sum(known, Decimal(0)) if len(known) == len(legs) else None


def newest_first(item: dict[str, Any]) -> str:
    return str(item.get("created_at") or "")


async def current_alert(rt: Runtime, uuid: str) -> dict[str, Any] | None:
    """The stored alert, or None if Kite does not know it (or the read failed)."""
    try:
        data = await rt.client.get(f"/alerts/{uuid}")
    except KiteMCPError:
        return None
    return data if isinstance(data, dict) and data.get("uuid") else None


def stored_basket_warnings(current: dict[str, Any] | None, uuid: str) -> list[str]:
    """The docs show basket items as type "insert" and never say whether a basket sent on modify replaces the stored
    one or is added to it. If it were added, Kite would hold more orders than the caps were checked against."""
    if current is None:
        return [f"alert {uuid} could not be read: whether it already has a basket of orders could not be checked"]
    basket = current.get("basket")
    items = basket.get("items") if isinstance(basket, dict) else None
    if not items:
        return []
    return [
        f"the alert already has {len(items)} basket order(s) and Kite's docs do not say whether the basket sent here "
        "replaces them or is added to them (the items are sent as type 'insert'): check get_alert afterwards, or "
        "delete the alert and create a new one"
    ]


async def describe_alerts(rt: Runtime, uuids: list[str]) -> tuple[list[dict[str, Any]], list[str]]:
    """(the named alerts as stored, warnings) for the delete dry run, from one read of the alert list."""
    try:
        stored = await rt.client.get("/alerts")
    except KiteMCPError:
        return [], ["the alert list could not be read: what is being deleted cannot be shown"]
    if not isinstance(stored, list):
        return [], ["the alert list could not be read: what is being deleted cannot be shown"]
    by_uuid = {str(a.get("uuid")).lower(): a for a in stored if isinstance(a, dict)}
    shown = [{k: by_uuid[u].get(k) for k in ("uuid", "name", "type", "status")} for u in uuids if u in by_uuid]
    missing = [u for u in uuids if u not in by_uuid]
    warnings = [f"no alert with uuid {u} in your alert list" for u in missing]
    return shown, warnings


def register(reg: Registrar) -> None:
    rt = reg.rt

    # --- reads ------------------------------------------------------------------------------------------------------

    @reg.tool("get_alerts", title="Alerts", toolset="alerts", kind="read")
    async def get_alerts(
        status: Annotated[
            AlertStatus | None,
            Field(description="only alerts in this state (enabled, disabled, deleted); omit for the default list"),
        ] = None,
        limit: Annotated[int, Field(ge=1, le=500)] = 100,
        offset: Annotated[int, Field(ge=0)] = 0,
    ) -> Any:
        """Your alerts, newest first. Kite allows at most 500 active alerts per user. Each has uuid, type (simple or
        ato), name, status (enabled = being evaluated, disabled, deleted = soft-deleted), disabled_reason, the
        condition (lhs_exchange, lhs_tradingsymbol, lhs_attribute, operator, rhs_type, rhs_constant or the rhs_*
        instrument), alert_count (how many times it triggered), created_at / updated_at (IST) and, for an ato alert,
        the basket of orders it places."""
        alerts = await rt.client.get("/alerts", params={"status": status} if status else None) or []
        alerts = sorted((a for a in alerts if isinstance(a, dict)), key=newest_first, reverse=True)
        return paginate(alerts, limit, offset)

    @reg.tool("get_alert", title="One alert", toolset="alerts", kind="read")
    async def get_alert(uuid: AlertUuid) -> Any:
        """One alert by its uuid: type, name, status, condition, alert_count and, for an ato alert, its basket."""
        return await rt.client.get(f"/alerts/{uuid.lower()}")

    @reg.tool("get_alert_history", title="When an alert triggered", toolset="alerts", kind="read")
    async def get_alert_history(
        uuid: AlertUuid,
        limit: Annotated[int, Field(ge=1, le=500)] = 50,
        offset: Annotated[int, Field(ge=0)] = 0,
    ) -> Any:
        """Every time the alert triggered, newest first: the condition as evaluated, created_at (IST), the market
        data of the instrument at that moment (last_price, ohlc, volume, oi, ...) and, for an ato alert, the orders
        it placed (order_meta)."""
        history = await rt.client.get(f"/alerts/{uuid.lower()}/history") or []
        events = sorted((h for h in history if isinstance(h, dict)), key=newest_first, reverse=True)
        return paginate(events, limit, offset)

    # --- writes -----------------------------------------------------------------------------------------------------

    @reg.tool("create_alert", title="Create an alert (dry run by default)", toolset="alerts", kind="write")
    async def create_alert(
        name: NameArg,
        type: TypeArg,
        lhs_exchange: LhsExchangeArg,
        lhs_tradingsymbol: LhsSymbolArg,
        operator: OperatorArg,
        rhs_type: RhsTypeArg,
        rhs_constant: RhsConstantArg = None,
        rhs_exchange: RhsExchangeArg = None,
        rhs_tradingsymbol: RhsSymbolArg = None,
        rhs_attribute: RhsAttributeArg = None,
        lhs_attribute: LhsAttributeArg = "LastTradedPrice",
        orders: OrdersArg = None,
        dry_run: Annotated[bool, DRY_RUN] = True,
        confirmation_token: Annotated[str | None, CONFIRM] = None,
    ) -> Any:
        """Create a price alert. DRY RUN BY DEFAULT: with dry_run=true (the default) nothing is sent; the answer shows
        the exact request, every safety check and, when live trading is enabled and the checks pass, a
        confirmation_token. To create it, repeat the identical call with dry_run=false and that token. Kite allows at
        most 500 active alerts per user.

        The alert watches lhs_attribute (LastTradedPrice) of lhs_exchange:lhs_tradingsymbol and triggers when
        "value operator right-hand side" becomes true (operators: <=, >=, <, >, ==). The right-hand side is a constant
        (rhs_type=constant with rhs_constant, in INR for prices) or another instrument's attribute (rhs_type=instrument
        with rhs_exchange and rhs_tradingsymbol). Indices live on the exchange INDICES (INDICES:NIFTY 50).

        type=simple only notifies. type=ato (alert triggers order) also places the basket `orders` by itself when the
        condition becomes true, with no confirmation at that moment: the confirmation you give now covers them. Each
        basket order is checked like place_order (per-order cap, allow-lists, tick and lot size) and counts toward the
        daily order count and value caps; MARKET orders are valued at the live price plus a buffer (paid market data
        needed), MCX/CDS/BCD orders cannot be valued and so cannot be sent live. A simple alert has no orders and no
        caps. Returns the created alert (with its uuid) when sent."""
        spec = make_spec(
            name=name,
            type=type,
            lhs_exchange=lhs_exchange,
            lhs_tradingsymbol=lhs_tradingsymbol,
            lhs_attribute=lhs_attribute,
            operator=operator,
            rhs_type=rhs_type,
            rhs_constant=rhs_constant,
            rhs_exchange=rhs_exchange,
            rhs_tradingsymbol=rhs_tradingsymbol,
            rhs_attribute=rhs_attribute,
        )
        built = await build_alert(rt, spec, orders or [])
        preview = await basket_margins(rt, built) if dry_run else {}
        request = WriteRequest(
            tool="create_alert",
            method="POST",
            path="/alerts",
            data=built.data,
            summary=built.summary,
            legs=built.legs,
            counts_as_order=bool(built.legs),
            order_count=max(1, len(built.legs)),
            daily_value=basket_value(built.legs),
            preview=preview,
            warnings=built.warnings,
        )
        return await rt.pipeline.run(request, dry_run=dry_run, confirmation_token=confirmation_token)

    @reg.tool("modify_alert", title="Modify an alert (dry run by default)", toolset="alerts", kind="write")
    async def modify_alert(
        uuid: AlertUuid,
        name: NameArg,
        type: TypeArg,
        lhs_exchange: LhsExchangeArg,
        lhs_tradingsymbol: LhsSymbolArg,
        operator: OperatorArg,
        rhs_type: RhsTypeArg,
        rhs_constant: RhsConstantArg = None,
        rhs_exchange: RhsExchangeArg = None,
        rhs_tradingsymbol: RhsSymbolArg = None,
        rhs_attribute: RhsAttributeArg = None,
        lhs_attribute: LhsAttributeArg = "LastTradedPrice",
        orders: OrdersArg = None,
        dry_run: Annotated[bool, DRY_RUN] = True,
        confirmation_token: Annotated[str | None, CONFIRM] = None,
    ) -> Any:
        """Modify an alert. Kite replaces the whole alert, so send its complete new definition: call get_alert first
        and change only what you mean to change. The fields are those of create_alert, with the same rules (for an ato
        alert that includes the whole basket `orders`). DRY RUN BY DEFAULT, confirmed like create_alert.

        Safety: an ato alert's new basket is checked like place_order. A modification takes no new slot of the daily
        order count, but the full value of the new basket counts toward the day's value cap. The dry run shows the
        stored alert. Kite's docs do not say whether a basket sent here replaces the stored basket or adds to it, so
        the dry run warns when the alert already has one. Returns the updated alert when sent."""
        spec = make_spec(
            name=name,
            type=type,
            lhs_exchange=lhs_exchange,
            lhs_tradingsymbol=lhs_tradingsymbol,
            lhs_attribute=lhs_attribute,
            operator=operator,
            rhs_type=rhs_type,
            rhs_constant=rhs_constant,
            rhs_exchange=rhs_exchange,
            rhs_tradingsymbol=rhs_tradingsymbol,
            rhs_attribute=rhs_attribute,
        )
        built = await build_alert(rt, spec, orders or [])
        preview: dict[str, Any] = {}
        if dry_run:
            preview = await basket_margins(rt, built)
            current = await current_alert(rt, uuid.lower())
            built.warnings += stored_basket_warnings(current, uuid.lower())
            if current is not None:
                preview["current_alert"] = current
        request = WriteRequest(
            tool="modify_alert",
            method="PUT",
            path=f"/alerts/{uuid.lower()}",
            data=built.data,
            summary=f"modify alert {uuid.lower()}: {built.summary}",
            legs=built.legs,
            counts_as_order=bool(built.legs),
            order_count=0,
            daily_value=basket_value(built.legs),
            order_id=uuid.lower(),
            preview=preview,
            warnings=built.warnings,
        )
        return await rt.pipeline.run(request, dry_run=dry_run, confirmation_token=confirmation_token)

    @reg.tool("delete_alerts", title="Delete alerts (dry run by default)", toolset="alerts", kind="write")
    async def delete_alerts(
        uuids: Annotated[
            list[AlertUuid],
            Field(min_length=1, max_length=50, description="uuids of the alerts to delete (see get_alerts), up to 50"),
        ],
        dry_run: Annotated[bool, DRY_RUN] = True,
        confirmation_token: Annotated[str | None, CONFIRM] = None,
    ) -> Any:
        """Delete one or more alerts in a single call (a deleted alert shows status 'deleted'). Deleting an ato alert
        also removes the orders it would have placed. DRY RUN BY DEFAULT, confirmed like create_alert. No caps or
        allow-lists apply to a deletion. The dry run lists the alerts that will be deleted. Kite answers with no
        data when it is sent."""
        ids = list(dict.fromkeys(u.lower() for u in uuids))
        shown, warnings = await describe_alerts(rt, ids)
        names = ", ".join(f"'{a['name']}'" for a in shown)
        request = WriteRequest(
            tool="delete_alerts",
            method="DELETE",
            path="/alerts",
            params=[("uuid", u) for u in ids],
            summary=f"delete {len(ids)} alert(s)" + (f": {names}" if names else ""),
            risk_reducing=True,
            preview={"alerts": shown} if shown else {},
            warnings=warnings,
        )
        return await rt.pipeline.run(request, dry_run=dry_run, confirmation_token=confirmation_token)
