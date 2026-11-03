"""Tools: holdings, positions, auctions, position conversion and holdings authorisation
(https://kite.trade/docs/connect/v3/portfolio/).

The two writes are not orders, and the safety policy treats them that way:
    convert_position     changes the margin product of an open position, so it creates no new exposure and counts
                         toward neither the daily order count nor the daily value. It is still a confirmed write with a
                         leg for the allow-lists (the product the position ends up in). Its leg is valued at the
                         position's notional (quantity x multiplier x last_price from get_positions) and checked
                         against the per-order cap: moving a large position between margin products is exactly the
                         kind of slip the cap is for (MIS -> NRML carries it overnight). If the position cannot be
                         found, the value is unknown and the conversion cannot be sent live (Kite would reject it).
    authorise_holdings   asks the depository (CDSL) for permission to debit shares on a sale. No money moves and no
                         order is placed, so there are no legs and no caps; it is a confirmed write that returns the
                         link the user opens to enter their demat PIN.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Literal
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field

from ..errors import KiteMCPError, ToolInputError
from ..safety.pipeline import WriteRequest
from ..safety.policy import OrderLeg
from ._base import Registrar, Runtime, paginate
from .orders import CONFIRM, DRY_RUN, Exchange, Product, Side

PositionType = Literal["day", "overnight"]
DERIVATIVE_EXCHANGES = ("NFO", "BFO", "MCX", "CDS", "BCD")
ISIN_PATTERN = r"^[A-Za-z]{2}[A-Za-z0-9]{9}[0-9]$"
AUTHORISE_URL = "https://kite.zerodha.com/connect/portfolio/authorise/holdings/{api_key}/{request_id}"
POSITION_FIELDS = (
    "exchange",
    "tradingsymbol",
    "product",
    "quantity",
    "overnight_quantity",
    "multiplier",
    "average_price",
    "last_price",
    "pnl",
)
HOW_AUTHORISATION_WORKS = (
    "Selling shares from your demat account needs an electronic authorisation at the depository (CDSL) unless Kite "
    "holds a power of attorney. A sale without it is refused with HTTP 428 ('N quantity needs authorisation at "
    "depository'). After you send this request, open the link in the live answer and key in your CDSL demat PIN "
    "(TPIN) there: the PIN is known only to you and CDSL, never to this server or to Kite. By default Kite offers to "
    "authorise the full quantity of every holding. An authorisation is valid for one trading session (until 5:30 PM "
    "IST, after which it counts for the next trading day) and is used up as you sell; then retry the sell."
)
NEXT_STEP = (
    "Open authorise_url in a browser and key in your CDSL demat PIN (TPIN) there; the PIN never passes through this "
    "server. The page ends at .../finish?status=success (status=error if it failed). The authorisation is valid for "
    "the current trading session (until 5:30 PM IST) and is used up as you sell. Then retry the sell that was refused "
    "with HTTP 428."
)


class HoldingAuthorisation(BaseModel):
    """One holding to authorise for sale."""

    model_config = ConfigDict(extra="forbid")

    isin: Annotated[
        str, Field(pattern=ISIN_PATTERN, description="the holding's ISIN, e.g. INE002A01018 (get_holdings)")
    ]
    quantity: Annotated[int, Field(ge=1, description="shares to authorise for sale")]


def amount(value: Any) -> Decimal:
    """A Kite number as a Decimal; a missing, null or unusable value counts as 0."""
    if value is None:
        return Decimal(0)
    try:
        out = Decimal(str(value))
    except ArithmeticError:
        return Decimal(0)
    return out if out.is_finite() else Decimal(0)


def money(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def percent(part: Decimal, whole: Decimal) -> float | None:
    return money(part / whole * 100) if whole else None


def holdings_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Totals over `rows`, in INR. The share count of a holding is quantity + t1_quantity: the docs define quantity as
    the settled (T+2) quantity and t1_quantity as the shares bought but not yet delivered. invested and current_value
    use average_price and last_price, pnl is Kite's own pnl summed, and day_change is Kite's per-share day_change times
    the share count (it is the change against the previous close)."""
    invested = current = pnl = day = previous_close = Decimal(0)
    for row in rows:
        shares = amount(row.get("quantity")) + amount(row.get("t1_quantity"))
        invested += amount(row.get("average_price")) * shares
        current += amount(row.get("last_price")) * shares
        pnl += amount(row.get("pnl"))
        day += amount(row.get("day_change")) * shares
        previous_close += amount(row.get("close_price")) * shares
    return {
        "holdings": len(rows),
        "invested": money(invested),
        "current_value": money(current),
        "pnl": money(pnl),
        "pnl_percentage": percent(pnl, invested),
        "day_change": money(day),
        "day_change_percentage": percent(day, previous_close),
        "with_price_discrepancy": sum(1 for row in rows if row.get("discrepancy")),
    }


def positions_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    def total(field: str) -> float:
        return money(sum((amount(row.get(field)) for row in rows), Decimal(0)))

    return {
        "positions": len(rows),
        "open": sum(1 for row in rows if amount(row.get("quantity")) != 0),
        "pnl": total("pnl"),
        "m2m": total("m2m"),
        "unrealised": total("unrealised"),
        "realised": total("realised"),
    }


# --- position conversion --------------------------------------------------------------------------------------------


async def open_position(rt: Runtime, exchange: str, symbol: str, product: str) -> dict[str, Any] | None:
    """The position (net book first, then the day book) on this instrument and product, or None."""
    try:
        data = await rt.client.get("/portfolio/positions")
    except KiteMCPError:
        return None
    if not isinstance(data, dict):
        return None
    for book in ("net", "day"):
        for row in data.get(book) or []:
            if (
                isinstance(row, dict)
                and str(row.get("exchange", "")).upper() == exchange
                and str(row.get("tradingsymbol", "")).upper() == symbol
                and str(row.get("product", "")).upper() == product
            ):
                return row
    return None


def conversion_leg(
    row: dict[str, Any] | None, exchange: str, symbol: str, side: str, product: str, quantity: int
) -> OrderLeg:
    """The leg the policy sees: the product the position ends up in, valued at the converted quantity's notional.

    The multiplier comes from the positions book itself (1 for equity and F&O, the lot size for MCX), which is why this
    can value MCX positions where an order cannot be valued.
    """
    common: dict[str, Any] = {
        "exchange": exchange,
        "tradingsymbol": symbol,
        "transaction_type": side,
        "product": product,
        "quantity": quantity,
    }
    if row is None:
        return OrderLeg(**common, value=None, value_note="the position was not found in get_positions")
    price = amount(row.get("last_price"))
    if price <= 0:
        return OrderLeg(**common, value=None, value_note="get_positions shows no last_price for the position")
    multiplier = amount(row.get("multiplier")) or Decimal(1)
    value = (quantity * multiplier * price).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return OrderLeg(**common, price=price, value=value, value_note="quantity x multiplier x last_price of the position")


def conversion_warnings(
    row: dict[str, Any] | None, side: str, position_type: str, quantity: int, old_product: str, new_product: str
) -> list[str]:
    warnings: list[str] = []
    if old_product == "MIS" and new_product != "MIS":
        warnings.append(
            f"MIS positions are squared off by the broker at the end of the day; as {new_product} this one is carried "
            f"overnight and needs the full {new_product} margin"
        )
    if row is None:
        warnings.append(
            f"no {old_product} position on this instrument in get_positions: Kite is likely to reject the conversion"
        )
        return warnings
    held = amount(row.get("quantity"))
    overnight = abs(amount(row.get("overnight_quantity")))
    if held and (held > 0) != (side == "BUY"):
        warnings.append(
            f"the position is {'long' if held > 0 else 'short'}: transaction_type should be "
            f"{'BUY' if held > 0 else 'SELL'}"
        )
    if quantity > abs(held):
        warnings.append(f"quantity {quantity} is more than the position's quantity {abs(held)}")
    elif position_type == "overnight" and quantity > overnight:
        warnings.append(f"quantity {quantity} is more than the overnight quantity {overnight}")
    return warnings


# --- holdings authorisation -----------------------------------------------------------------------------------------


async def authorisation_warnings(rt: Runtime, items: list[HoldingAuthorisation]) -> list[str]:
    """Check the ISINs and quantities against get_holdings; Kite shows its own error on the page otherwise."""
    try:
        stored = await rt.client.get("/portfolio/holdings")
    except KiteMCPError:
        stored = None
    if not isinstance(stored, list):
        return ["the holdings could not be read: the ISINs and quantities were not checked"]
    held: dict[str, Decimal] = {}
    for row in stored:
        if isinstance(row, dict):
            isin = str(row.get("isin", "")).upper()
            held[isin] = held.get(isin, Decimal(0)) + amount(row.get("quantity")) + amount(row.get("t1_quantity"))
    warnings: list[str] = []
    for item in items:
        have = held.get(item.isin.upper())
        if have is None:
            warnings.append(f"{item.isin.upper()} is not in your holdings")
        elif item.quantity > have:
            warnings.append(f"{item.isin.upper()}: authorising {item.quantity} but you hold {have}")
    return warnings


def authorisation_link(rt: Runtime, response: Any) -> dict[str, Any]:
    """What the user needs after a live authorisation request: the link built from the api_key and the request_id."""
    request_id = response.get("request_id") if isinstance(response, dict) else None
    if not request_id:
        return {"note": "Kite returned no request_id, so there is no link to open; see `response`"}
    url = AUTHORISE_URL.format(
        api_key=quote(rt.settings.require_api_key(), safe=""), request_id=quote(str(request_id), safe="")
    )
    return {"authorise_url": url, "next_step": NEXT_STEP}


def register(reg: Registrar) -> None:
    rt = reg.rt

    # --- reads ------------------------------------------------------------------------------------------------------

    @reg.tool("get_holdings", title="Holdings", toolset="portfolio", kind="read")
    async def get_holdings(
        tradingsymbol: Annotated[
            str | None, Field(description="only holdings of this tradingsymbol, e.g. INFY")
        ] = None,
        limit: Annotated[int, Field(ge=1, le=500)] = 100,
        offset: Annotated[int, Field(ge=0)] = 0,
    ) -> Any:
        """Long-term equity holdings in the demat account, by tradingsymbol, with a `summary` over ALL the holdings
        that match (not just the page), in INR: invested (average_price x shares), current_value (last_price x shares),
        pnl (the sum of Kite's pnl) and its percentage, day_change (against the previous close) and its percentage.
        The shares of a holding are quantity (settled, T+2) plus t1_quantity (bought, not yet delivered). Each holding
        has tradingsymbol, exchange, isin, product, quantity, t1_quantity, used_quantity, authorised_quantity (for
        sale at the depository), collateral_quantity, average_price, last_price, close_price, pnl, day_change (per
        share) and day_change_percentage."""
        holdings = [h for h in await rt.client.get("/portfolio/holdings") or [] if isinstance(h, dict)]
        if tradingsymbol:
            wanted = tradingsymbol.strip().upper()
            holdings = [h for h in holdings if str(h.get("tradingsymbol", "")).upper() == wanted]
        holdings.sort(key=lambda h: (str(h.get("tradingsymbol", "")), str(h.get("exchange", ""))))
        return paginate(holdings, limit, offset, summary=holdings_summary(holdings))

    @reg.tool("get_positions", title="Positions", toolset="portfolio", kind="read")
    async def get_positions(
        kind: Annotated[
            Literal["net", "day", "both"],
            Field(description="net: the actual current positions. day: today's buying and selling. both (default)"),
        ] = "both",
    ) -> Any:
        """Short-term positions (derivatives and intraday equity). `net` is the actual current position book; `day`
        is a snapshot of today's buying and selling, useful for intraday P&L. Each position has tradingsymbol,
        exchange, product, quantity, overnight_quantity, multiplier (lot-size multiplier for P&L: 1 for equity and
        F&O, the lot size for MCX), average_price, last_price, value, pnl, m2m, unrealised, realised and the
        buy / sell / day_buy / day_sell quantity, price and value. A `summary` per book totals pnl, m2m, unrealised
        and realised (INR) and counts the open positions (quantity not 0). Equity positions carried overnight move
        to the holdings the next day."""
        data = await rt.client.get("/portfolio/positions")
        data = data if isinstance(data, dict) else {}
        books = ("net", "day") if kind == "both" else (kind,)
        rows = {book: [r for r in data.get(book) or [] if isinstance(r, dict)] for book in books}
        return {"summary": {book: positions_summary(rows[book]) for book in books}, **rows}

    @reg.tool("get_auctions", title="Auctions of your holdings", toolset="portfolio", kind="read")
    async def get_auctions() -> Any:
        """The auctions being held right now for stocks you hold in your demat account (only those). Each entry is
        the holding (tradingsymbol, exchange, isin, quantity, average_price, last_price, pnl, ...) plus its
        auction_number: pass that to place_order with variety=auction to take part."""
        auctions = [a for a in await rt.client.get("/portfolio/holdings/auctions") or [] if isinstance(a, dict)]
        return {"count": len(auctions), "items": auctions}

    # --- writes -----------------------------------------------------------------------------------------------------

    @reg.tool(
        "convert_position", title="Convert a position's product (dry run by default)", toolset="portfolio", kind="write"
    )
    async def convert_position(
        exchange: Annotated[Exchange, Field(description="exchange of the position: NSE, BSE, NFO, BFO, CDS, BCD, MCX")],
        tradingsymbol: Annotated[str, Field(min_length=1, max_length=50, description="e.g. INFY, NIFTY25NOVFUT")],
        transaction_type: Annotated[
            Side, Field(description="the position's side: BUY for a long position, SELL for a short one")
        ],
        position_type: Annotated[
            PositionType, Field(description="overnight: carried over from a previous day. day: opened today")
        ],
        quantity: Annotated[int, Field(ge=1, description="quantity to convert (MCX/CDS/BCD: lots)")],
        old_product: Annotated[Product, Field(description="the position's current margin product")],
        new_product: Annotated[Product, Field(description="the margin product to convert to")],
        dry_run: Annotated[bool, DRY_RUN] = True,
        confirmation_token: Annotated[str | None, CONFIRM] = None,
    ) -> Any:
        """Convert the margin product of an open position, for example MIS to NRML or NRML to MIS.
        DRY RUN BY DEFAULT: with dry_run=true (the default) nothing is sent; the answer shows the exact request, the
        position found in get_positions, every safety check and, when live trading is enabled and the checks pass, a
        confirmation_token. To convert, repeat the identical call with dry_run=false and that token.

        A conversion places no order and adds no exposure, so it does not count toward the daily order count or the
        daily value. It is still checked: the new product must be allowed, and the converted quantity's notional
        (quantity x multiplier x last_price from get_positions) must be within the per-order cap. If the position is
        not found, its value is unknown and the conversion cannot be sent live. Moving a position out of MIS keeps it
        open past the end of the day and needs the full margin of the new product. Returns true when sent."""
        exchange_u, symbol = exchange.upper(), tradingsymbol.strip().upper()
        problems: list[str] = []
        if not symbol:
            problems.append("tradingsymbol must not be blank")
        if old_product == new_product:
            problems.append("old_product and new_product are the same: there is nothing to convert")
        if new_product == "CNC" and exchange_u in DERIVATIVE_EXCHANGES:
            problems.append(f"CNC is for equity delivery; {exchange_u} positions are NRML or MIS")
        if problems:
            raise ToolInputError("; ".join(problems))
        row = await open_position(rt, exchange_u, symbol, old_product)
        request = WriteRequest(
            tool="convert_position",
            method="PUT",
            path="/portfolio/positions",
            data={
                "tradingsymbol": symbol,
                "exchange": exchange_u,
                "transaction_type": transaction_type,
                "position_type": position_type,
                "quantity": quantity,
                "old_product": old_product,
                "new_product": new_product,
            },
            summary=(
                f"convert {transaction_type} {quantity} {exchange_u}:{symbol} ({position_type} position) "
                f"from {old_product} to {new_product}"
            ),
            legs=[conversion_leg(row, exchange_u, symbol, transaction_type, new_product, quantity)],
            counts_as_order=False,
            preview={"position": {k: row.get(k) for k in POSITION_FIELDS}} if row else {},
            warnings=conversion_warnings(row, transaction_type, position_type, quantity, old_product, new_product),
        )
        return await rt.pipeline.run(request, dry_run=dry_run, confirmation_token=confirmation_token)

    @reg.tool(
        "authorise_holdings",
        title="Authorise holdings for sale (dry run by default)",
        toolset="portfolio",
        kind="write",
    )
    async def authorise_holdings(
        holdings: Annotated[
            list[HoldingAuthorisation] | None,
            Field(
                min_length=1,
                description="the holdings (isin, quantity) to authorise; omit to authorise the entire holdings",
            ),
        ] = None,
        dry_run: Annotated[bool, DRY_RUN] = True,
        confirmation_token: Annotated[str | None, CONFIRM] = None,
    ) -> Any:
        """Start a CDSL authorisation so that shares can be sold from the demat account. Call this when a SELL fails
        with HTTP 428 ('N quantity needs authorisation at depository'). DRY RUN BY DEFAULT: with dry_run=true (the
        default) nothing is sent; the answer shows the request and the checks. Repeat the identical call with
        dry_run=false and the confirmation_token to send it: Kite answers with a request_id, and this tool's live
        answer then contains `authorise_url`, the link YOU open in a browser to key in your demat PIN (TPIN) at CDSL.
        The PIN never passes through this server. Nothing is authorised until you finish that page.

        With `holdings` (isin and quantity pairs) only those shares are authorised; without it the page offers the
        entire holdings. An authorisation is valid for one trading session (until 5:30 PM IST, then it counts for the
        next trading day) and is used up as you sell. This is not an order: no caps apply."""
        items = holdings or []
        if len({item.isin.upper() for item in items}) != len(items):
            raise ToolInputError("each ISIN may appear only once in holdings")
        data: list[tuple[str, Any]] = []  # a list of pairs: isin and quantity repeat once per holding
        for item in items:
            data += [("isin", item.isin.upper()), ("quantity", item.quantity)]
        summary = (
            "authorise for sale at CDSL: " + ", ".join(f"{i.isin.upper()} x{i.quantity}" for i in items)
            if items
            else "authorise the entire holdings for sale at CDSL"
        )
        request = WriteRequest(
            tool="authorise_holdings",
            method="POST",
            path="/portfolio/holdings/authorise",
            data=data,
            summary=summary,
            counts_as_order=False,
            preview={"how_it_works": HOW_AUTHORISATION_WORKS} if dry_run else {},
            warnings=await authorisation_warnings(rt, items) if items and dry_run else [],
        )
        result = await rt.pipeline.run(request, dry_run=dry_run, confirmation_token=confirmation_token)
        if result.get("sent"):  # the pipeline's own answer, plus the link the user has to open
            result.update(authorisation_link(rt, result.get("response")))
        return result
