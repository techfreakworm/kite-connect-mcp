"""Tools: orders and trades (https://kite.trade/docs/connect/v3/orders/).

This module is the reference for write tools: validate locally, estimate the value, preview Kite's margins, then hand
one WriteRequest to the safety pipeline, which does the dry run or (only when everything allows it) the live send.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import Field

from ..errors import KiteMCPError, ToolInputError
from ..safety.pipeline import WriteRequest
from ._base import Registrar, Runtime, paginate, require
from ._orders_common import (
    describe_order,
    estimate_leg,
    instrument_problems,
    margin_order,
    margins_preview,
    validate_order,
)

Variety = Literal["regular", "amo", "co", "iceberg", "auction"]
Exchange = Literal["NSE", "BSE", "NFO", "BFO", "CDS", "BCD", "MCX"]
Product = Literal["CNC", "NRML", "MIS", "MTF"]
OrderType = Literal["MARKET", "LIMIT", "SL", "SL-M"]
Side = Literal["BUY", "SELL"]
Validity = Literal["DAY", "IOC", "TTL"]

DRY_RUN = Field(
    description="true (default): validate and preview only, nothing is sent. false: send it live, which "
    "needs live trading enabled on the server and the confirmation_token from a dry run"
)
CONFIRM = Field(description="the confirmation_token returned by a dry run of this exact call (single use)")
OPEN_STATUSES = {
    "OPEN",
    "TRIGGER PENDING",
    "AMO REQ RECEIVED",
    "OPEN PENDING",
    "MODIFY PENDING",
    "PUT ORDER REQ RECEIVED",
    "VALIDATION PENDING",
    "MODIFY VALIDATION PENDING",
}


async def current_order(rt: Runtime, order_id: str) -> dict[str, Any] | None:
    """The newest state of an order from its history (GET /orders/:order_id), or None if Kite has no such order."""
    try:
        history = await rt.client.get(f"/orders/{order_id}")
    except KiteMCPError:
        return None
    if isinstance(history, list) and history:
        return history[-1]
    return None


def register(reg: Registrar) -> None:
    rt = reg.rt

    # --- reads ------------------------------------------------------------------------------------------------------

    @reg.tool("get_orders", title="Today's orders", toolset="orders", kind="read")
    async def get_orders(
        status: Annotated[
            Literal["open", "complete", "cancelled", "rejected", "all"],
            Field(description="open = still working (OPEN, TRIGGER PENDING, AMO...), or a final state"),
        ] = "all",
        tradingsymbol: Annotated[str | None, Field(description="only orders for this tradingsymbol")] = None,
        limit: Annotated[int, Field(ge=1, le=500)] = 100,
        offset: Annotated[int, Field(ge=0)] = 0,
    ) -> Any:
        """The day's order book (open and executed orders), newest first. Each order has order_id, status,
        status_message, variety, exchange, tradingsymbol, transaction_type, order_type, product, quantity,
        filled/pending/cancelled quantities, price, trigger_price, average_price, timestamps and tag."""
        orders = await rt.client.get("/orders") or []
        wanted = {"open": None, "complete": "COMPLETE", "cancelled": "CANCELLED", "rejected": "REJECTED"}
        if status == "open":
            orders = [o for o in orders if str(o.get("status", "")).upper() in OPEN_STATUSES]
        elif status != "all":
            orders = [o for o in orders if str(o.get("status", "")).upper() == wanted[status]]
        if tradingsymbol:
            orders = [o for o in orders if str(o.get("tradingsymbol", "")).upper() == tradingsymbol.upper()]
        orders = sorted(orders, key=lambda o: str(o.get("order_timestamp") or ""), reverse=True)
        return paginate(orders, limit, offset)

    @reg.tool("get_order_history", title="Order history", toolset="orders", kind="read")
    async def get_order_history(
        order_id: Annotated[str, Field(min_length=1, max_length=64, description="the order_id")],
    ) -> Any:
        """Every state an order went through (put order req received, validation pending, open, complete,
        rejected...), oldest first, with the status_message of each step."""
        return await rt.client.get(f"/orders/{order_id}")

    @reg.tool("get_trades", title="Today's trades", toolset="orders", kind="read")
    async def get_trades(
        limit: Annotated[int, Field(ge=1, le=500)] = 100,
        offset: Annotated[int, Field(ge=0)] = 0,
    ) -> Any:
        """The day's executed trades (fills), newest first: trade_id, order_id, exchange, tradingsymbol, product,
        average_price, quantity, transaction_type, fill_timestamp."""
        trades = await rt.client.get("/trades") or []
        trades = sorted(trades, key=lambda t: str(t.get("fill_timestamp") or ""), reverse=True)
        return paginate(trades, limit, offset)

    @reg.tool("get_order_trades", title="Trades of an order", toolset="orders", kind="read")
    async def get_order_trades(
        order_id: Annotated[str, Field(min_length=1, max_length=64)],
    ) -> Any:
        """The fills that one order produced."""
        return await rt.client.get(f"/orders/{order_id}/trades")

    # --- writes -----------------------------------------------------------------------------------------------------

    @reg.tool("place_order", title="Place an order (dry run by default)", toolset="orders", kind="write")
    async def place_order(
        exchange: Annotated[Exchange, Field(description="NSE, BSE (equity), NFO, BFO (F&O), CDS, BCD, MCX")],
        tradingsymbol: Annotated[str, Field(min_length=1, max_length=50, description="e.g. INFY, NIFTY25NOVFUT")],
        transaction_type: Annotated[Side, Field(description="BUY or SELL")],
        quantity: Annotated[int, Field(ge=1, description="units (NFO/BFO: a multiple of the lot size)")],
        product: Annotated[Product, Field(description="CNC delivery, MIS intraday, NRML F&O carry, MTF margin")],
        order_type: Annotated[OrderType, Field(description="MARKET, LIMIT, SL (stop-limit), SL-M (stop-market)")],
        variety: Annotated[Variety, Field(description="regular, amo (after market), co (cover), iceberg, auction")] = (
            "regular"
        ),
        price: Annotated[float | None, Field(gt=0, description="limit price (LIMIT, SL)")] = None,
        trigger_price: Annotated[float | None, Field(gt=0, description="trigger (SL, SL-M; stop-loss for co)")] = None,
        validity: Annotated[Validity | None, Field(description="DAY (default), IOC, or TTL")] = None,
        validity_ttl: Annotated[int | None, Field(ge=1, le=365, description="minutes, for validity=TTL")] = None,
        disclosed_quantity: Annotated[int | None, Field(ge=0, description="quantity shown in the market")] = None,
        iceberg_legs: Annotated[int | None, Field(ge=2, le=50, description="iceberg only: number of legs")] = None,
        iceberg_quantity: Annotated[int | None, Field(ge=1, description="iceberg only: quantity per leg")] = None,
        auction_number: Annotated[str | None, Field(max_length=20, description="auction only (get_auctions)")] = None,
        market_protection: Annotated[
            float | None, Field(description="MARKET / SL-M only: -1 automatic, or a % above 0 and up to 100")
        ] = None,
        autoslice: Annotated[
            bool | None, Field(description="split a quantity above the exchange freeze limit into up to 10 orders")
        ] = None,
        tag: Annotated[str | None, Field(max_length=20, description="alphanumeric label, max 20 chars")] = None,
        dry_run: Annotated[bool, DRY_RUN] = True,
        confirmation_token: Annotated[str | None, CONFIRM] = None,
    ) -> Any:
        """Place an order. DRY RUN BY DEFAULT: with dry_run=true (the default) nothing is sent; the answer shows the
        exact request, the estimated value, Kite's margin calculation, every safety check and, when live trading is
        enabled and the checks pass, a confirmation_token. To send it, repeat the identical call with dry_run=false and
        that token. Live sends are checked against the server's caps and allow-lists and written to the audit log.

        Placing an order only registers it with Kite's OMS; check get_order_history for its fate. Live API orders must
        come from the static IP registered in the Kite developer console (SEBI rule, since April 2026)."""
        exchange_u, symbol = exchange.upper(), tradingsymbol.strip().upper()
        warnings = validate_order(
            variety=variety,
            exchange=exchange_u,
            order_type=order_type,
            product=product,
            quantity=quantity,
            price=price,
            trigger_price=trigger_price,
            validity=validity,
            validity_ttl=validity_ttl,
            disclosed_quantity=disclosed_quantity,
            iceberg_legs=iceberg_legs,
            iceberg_quantity=iceberg_quantity,
            auction_number=auction_number,
            market_protection=market_protection,
            tag=tag,
        )
        errors, more = await instrument_problems(rt, exchange_u, symbol, quantity, [price, trigger_price])
        if errors:
            raise ToolInputError("; ".join(errors))
        warnings += more
        leg = await estimate_leg(
            rt,
            exchange=exchange_u,
            tradingsymbol=symbol,
            transaction_type=transaction_type,
            quantity=quantity,
            order_type=order_type,
            product=product,
            variety=variety,
            price=price,
            trigger_price=trigger_price,
            market_protection=market_protection,
        )
        data: dict[str, Any] = {
            "exchange": exchange_u,
            "tradingsymbol": symbol,
            "transaction_type": transaction_type,
            "quantity": quantity,
            "product": product,
            "order_type": order_type,
            "price": price,
            "trigger_price": trigger_price,
            "validity": validity or "DAY",
            "validity_ttl": validity_ttl,
            "disclosed_quantity": disclosed_quantity,
            "iceberg_legs": iceberg_legs,
            "iceberg_quantity": iceberg_quantity,
            "auction_number": auction_number,
            "market_protection": market_protection,
            "autoslice": autoslice,
            "tag": tag,
        }
        preview: dict[str, Any] = {}
        if dry_run:
            preview = await margins_preview(
                rt,
                [
                    margin_order(
                        exchange=exchange_u,
                        tradingsymbol=symbol,
                        transaction_type=transaction_type,
                        variety=variety,
                        product=product,
                        order_type=order_type,
                        quantity=quantity,
                        price=price,
                        trigger_price=trigger_price,
                    )
                ],
            )
        request = WriteRequest(
            tool="place_order",
            method="POST",
            path=f"/orders/{variety}",
            data={k: v for k, v in data.items() if v is not None},
            summary=describe_order(
                transaction_type, quantity, exchange_u, symbol, order_type, price, trigger_price, product, variety
            ),
            legs=[leg],
            counts_as_order=True,
            order_count=10 if autoslice else 1,  # an autoslice order can become up to 10 orders
            preview=preview,
            warnings=warnings,
        )
        return await rt.pipeline.run(request, dry_run=dry_run, confirmation_token=confirmation_token)

    @reg.tool("modify_order", title="Modify an open order (dry run by default)", toolset="orders", kind="write")
    async def modify_order(
        order_id: Annotated[str, Field(min_length=1, max_length=64)],
        variety: Annotated[Variety, Field(description="the order's variety (it is part of the URL)")] = "regular",
        quantity: Annotated[int | None, Field(ge=1)] = None,
        price: Annotated[float | None, Field(gt=0)] = None,
        order_type: Annotated[OrderType | None, Field()] = None,
        trigger_price: Annotated[float | None, Field(gt=0)] = None,
        validity: Annotated[Validity | None, Field()] = None,
        disclosed_quantity: Annotated[int | None, Field(ge=0)] = None,
        parent_order_id: Annotated[str | None, Field(max_length=64, description="co second leg: the parent")] = None,
        market_protection: Annotated[float | None, Field(description="MARKET / SL-M: -1 or (0, 100]")] = None,
        dry_run: Annotated[bool, DRY_RUN] = True,
        confirmation_token: Annotated[str | None, CONFIRM] = None,
    ) -> Any:
        """Modify an open or pending order: quantity, price, order_type, trigger_price, validity, disclosed_quantity
        (cover orders: price / trigger_price only). DRY RUN BY DEFAULT, confirmed like place_order. The new value is
        checked against the per-order cap and any increase against the daily cap. Kite allows 25 modifications per
        order."""
        require(
            any(
                v is not None
                for v in (quantity, price, order_type, trigger_price, validity, disclosed_quantity, market_protection)
            ),
            "nothing to modify: give at least one new value",
        )
        current = await current_order(rt, order_id)
        warnings: list[str] = []
        legs = []
        daily_delta: Decimal | None = Decimal(0)
        summary = f"modify {order_id}"
        if current is None:
            warnings.append("the order was not found in today's order book: value and allow-lists cannot be checked")
            daily_delta = None
        else:
            status = str(current.get("status", "")).upper()
            if status not in OPEN_STATUSES:
                warnings.append(f"the order's status is {status}: Kite only modifies open or pending orders")
            new_type = order_type or current.get("order_type") or "LIMIT"
            new_qty = quantity or int(current.get("quantity") or 0)
            new_price = price if price is not None else (current.get("price") or None)
            new_trigger = trigger_price if trigger_price is not None else (current.get("trigger_price") or None)
            leg = await estimate_leg(
                rt,
                exchange=current.get("exchange", ""),
                tradingsymbol=current.get("tradingsymbol", ""),
                transaction_type=current.get("transaction_type", ""),
                quantity=new_qty,
                order_type=new_type,
                product=current.get("product"),
                variety=variety,
                price=new_price,
                trigger_price=new_trigger,
                market_protection=market_protection,
            )
            legs = [leg]
            old_price = current.get("price") or current.get("average_price") or 0
            old_value = Decimal(str(old_price)) * int(current.get("quantity") or 0)
            daily_delta = max(Decimal(0), (leg.value or Decimal(0)) - old_value) if leg.value is not None else None
            summary = (
                f"modify {order_id} ({current.get('transaction_type')} {current.get('exchange')}:"
                f"{current.get('tradingsymbol')}) -> {new_type} qty {new_qty}"
                + (f" @ {new_price}" if new_price else "")
                + (f" trigger {new_trigger}" if new_trigger else "")
            )
        data = {
            "quantity": quantity,
            "price": price,
            "order_type": order_type,
            "trigger_price": trigger_price,
            "validity": validity,
            "disclosed_quantity": disclosed_quantity,
            "parent_order_id": parent_order_id,
            "market_protection": market_protection,
        }
        request = WriteRequest(
            tool="modify_order",
            method="PUT",
            path=f"/orders/{variety}/{order_id}",
            data={k: v for k, v in data.items() if v is not None},
            summary=summary,
            legs=legs,
            counts_as_order=False,
            daily_value=daily_delta,
            order_id=order_id,
            warnings=warnings,
            preview={"current_order": current} if current else {},
        )
        if current is None:
            # without the order we cannot know its value; treat as unverifiable (refused live, fine as a dry run)
            from ..safety.policy import OrderLeg

            request.legs = [OrderLeg(value=None, value_note="order not found in today's order book")]
        return await rt.pipeline.run(request, dry_run=dry_run, confirmation_token=confirmation_token)

    @reg.tool("cancel_order", title="Cancel an open order (dry run by default)", toolset="orders", kind="write")
    async def cancel_order(
        order_id: Annotated[str, Field(min_length=1, max_length=64)],
        variety: Annotated[Variety, Field(description="the order's variety (it is part of the URL)")] = "regular",
        parent_order_id: Annotated[str | None, Field(max_length=64, description="co second leg: the parent")] = None,
        dry_run: Annotated[bool, DRY_RUN] = True,
        confirmation_token: Annotated[str | None, CONFIRM] = None,
    ) -> Any:
        """Cancel an open or pending order. DRY RUN BY DEFAULT, confirmed like place_order (cancelling a stop-loss
        can increase risk, so it is not exempt). Caps do not apply to cancellations."""
        current = await current_order(rt, order_id)
        warnings = [] if current else ["the order was not found in today's order book"]
        from ..safety.policy import OrderLeg

        legs = []
        if current:
            legs = [
                OrderLeg(
                    exchange=current.get("exchange"),
                    tradingsymbol=current.get("tradingsymbol"),
                    transaction_type=current.get("transaction_type"),
                    product=current.get("product"),
                    order_type=current.get("order_type"),
                    variety=variety,
                    quantity=int(current.get("quantity") or 0),
                )
            ]
        request = WriteRequest(
            tool="cancel_order",
            method="DELETE",
            path=f"/orders/{variety}/{order_id}",
            params={"parent_order_id": parent_order_id} if parent_order_id else None,
            summary=f"cancel {order_id}"
            + (
                f" ({current.get('transaction_type')} {current.get('quantity')} {current.get('exchange')}:"
                f"{current.get('tradingsymbol')}, {current.get('status')})"
                if current
                else ""
            ),
            legs=legs,
            risk_reducing=True,
            order_id=order_id,
            warnings=warnings,
        )
        return await rt.pipeline.run(request, dry_run=dry_run, confirmation_token=confirmation_token)
