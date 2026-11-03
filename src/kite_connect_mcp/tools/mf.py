"""Tools: mutual funds (https://kite.trade/docs/connect/v3/mutual-funds/).

Reads, always on: get_mf_holdings, get_mf_orders, get_mf_sips. A fund is named by its ISIN, which is the `tradingsymbol`
of Kite's MF instrument list (search_mf_instruments finds it). Amounts are INR, `quantity` is in units of the fund
(fractional), `last_price` is the fund's last NAV per unit.

Writes: place_mf_order, cancel_mf_order, place_mf_sip, modify_mf_sip, cancel_mf_sip. They are LEGACY routes. Kite's
current page says "order placement can't be done, as order placement needs payment from the user's bank account" and
lists no write endpoint, though the official client libraries still ship them. So they are registered only with
KITE_MCP_ALLOW_MF_WRITES=true (otherwise `reg.skipped` says why), every description says that Kite may reject them, and
they go through the same pipeline as every write: dry run by default, confirmation token, caps, allow-lists, audit log.

How the pipeline sees a mutual-fund write:

    value        BUY: the amount. SELL: units x the fund's last NAV (MF instrument master); with no NAV (master
                 unavailable, fund not listed) the value cannot be verified, so a SELL can be dry-run but never sent.
                 SIP: one leg for the initial amount (if given) and one for an instalment, each checked against the
                 per-order cap; the daily value counts the initial amount if given, else one instalment. Later
                 instalments are run by Kite on its own schedule, out of reach of any cap here (the dry run says so).
    exchange     None on every leg, so KITE_MCP_ALLOWED_EXCHANGES / PRODUCTS / VARIETIES / ORDER_TYPES never block
                 a fund. KITE_MCP_ALLOWED_TRANSACTION_TYPES does apply (BUY / SELL).
    instruments  KITE_MCP_ALLOWED_INSTRUMENTS applies as "MF:<ISIN>" and fails closed (see MFLeg): once that list is
                 set, a fund has to be on it (e.g. MF:INF000A01AA0) before this server buys, redeems or SIPs it, or
                 cancels an order or SIP of it. In the `allowlist` confirmation mode a listed fund needs no token.

When the fund is in the instrument master, its purchase_allowed / redemption_allowed flags, minimum amounts and
multiples are checked: an error for a request Kite will certainly refuse, a warning for a doubtful one. Without the
master those checks are skipped, with a warning.

Each write tool is a thin wrapper over a `*_request` builder, which validates, looks things up and returns the
WriteRequest; only `rt.pipeline.run` ever sends.
"""

from __future__ import annotations

import datetime as dt
import re
import textwrap
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import Field

from ..errors import KiteMCPError, NotLoggedIn, TokenError, ToolInputError
from ..instruments import MFInstrument
from ..safety.pipeline import WriteRequest
from ..safety.policy import OrderLeg
from ._base import Registrar, Runtime, paginate, require
from ._orders_common import dec
from .margins import money, num
from .orders import CONFIRM, DRY_RUN, Side

Frequency = Literal["weekly", "monthly", "quarterly"]
SipStatus = Literal["active", "paused"]

WRITE_TOOLS = ("place_mf_order", "cancel_mf_order", "place_mf_sip", "modify_mf_sip", "cancel_mf_sip")
WRITES_OFF = "KITE_MCP_ALLOW_MF_WRITES is false"
ISIN = re.compile(r"[A-Z]{2}[A-Z0-9]{9}[0-9]")
TAG = re.compile(r"[A-Za-z0-9]{1,8}")  # "alphanumeric, max 8 chars" in Kite's MF order and SIP attributes
ID_PATTERN = r"^[A-Za-z0-9_\-]{1,64}$"  # what the HTTP client lets into a path (client/http.py)
SIP_DAYS = (1, 5, 10, 15, 20, 25)
MAX_AMOUNT = 1_000_000_000  # INR and units: far above any real order, keeps the Decimal arithmetic in range
STALE_NAV_DAYS = 5  # a long weekend plus a holiday; an older NAV makes a NAV-based value doubtful

FundIsin = Annotated[
    str,
    Field(
        min_length=12,
        max_length=16,
        description="the fund's 12-character ISIN, which is its MF tradingsymbol (e.g. INF000A01AA0); "
        "find it with search_mf_instruments",
    ),
]
OrderId = Annotated[str, Field(pattern=ID_PATTERN, description="the MF order_id (from get_mf_orders)")]
SipId = Annotated[str, Field(pattern=ID_PATTERN, description="the sip_id (from get_mf_sips)")]
Tag = Annotated[str | None, Field(max_length=8, description="alphanumeric label, at most 8 characters")]
Limit = Annotated[int, Field(ge=1, le=500, description="items per page")]
Offset = Annotated[int, Field(ge=0, description="items to skip")]
InstalmentDay = Annotated[
    int | None, Field(ge=1, le=31, description="monthly SIPs only: day of the month (1, 5, 10, 15, 20 or 25)")
]


def prose(text: str) -> str:
    """Re-wrap each paragraph to one width, so sentences shared by several descriptions read alike."""
    return "\n\n".join(textwrap.fill(" ".join(p.split()), width=118) for p in text.split("\n\n"))


LEGACY_WARNING = (
    "legacy route: Kite's current docs list no way to place or change mutual-fund orders or SIPs (they say order "
    "placement needs payment from the user's bank account), so Kite may reject this request"
)
LEGACY_DOC = (
    "LEGACY ROUTE: Kite's current documentation lists no way to place, change or cancel mutual-fund orders or SIPs, "
    "and says \"Order placement can't be done, as order placement needs payment from the user's bank account\". "
    "Kite may reject this request. This tool exists only because KITE_MCP_ALLOW_MF_WRITES=true; check "
    "get_mf_orders / get_mf_sips afterwards."
)
DRY_RUN_DOC = (
    "DRY RUN BY DEFAULT: with dry_run=true (the default) nothing is sent; the answer shows the exact request, the "
    "fund, the value used for the caps, every safety check and, when live trading is enabled and the checks pass, a "
    "confirmation_token. To send it, repeat the identical call with dry_run=false and that token."
)

PLACE_ORDER_DOC = prose(
    f"""\
Buy a mutual fund for an amount in INR, or redeem (SELL) a number of its units; the fund is named by its ISIN.
{DRY_RUN_DOC}

BUY takes `amount` (INR, up to 2 decimals; for a known fund a multiple of its purchase amount multiplier and at least
its minimum purchase) and no quantity. SELL takes `quantity` (units, e.g. 12.345) and no amount. For the per-order and
daily caps a BUY is worth its amount and a SELL its units x the fund's last NAV; when that NAV is unknown the SELL
cannot be verified, so it can be dry-run but never sent live. Counts as one order toward the daily limit.

{LEGACY_DOC}"""
)

CANCEL_ORDER_DOC = prose(
    f"""\
Cancel an open mutual fund order. {DRY_RUN_DOC} Cancelling reduces risk, so the value caps do not apply, but the
confirmation step does. The order is looked up first so the answer shows what it is.

{LEGACY_DOC}"""
)

PLACE_SIP_DOC = prose(
    f"""\
Start a SIP (a recurring purchase) in a mutual fund named by its ISIN. {DRY_RUN_DOC}

Each instalment buys `amount` INR (up to 2 decimals). `instalments` is how many instalments Kite runs; -1 should mean
no end, i.e. until the SIP is cancelled, which is how Kite's SIP list reports such SIPs, but Kite's current docs no
longer describe SIP creation, so verify with get_mf_sips after sending. `frequency` is weekly, monthly or quarterly;
`instalment_day` (1 to 31) is for monthly SIPs only, and Kite's older docs listed 1, 5, 10, 15, 20 and 25 as the days
that work (not re-checked). `initial_amount` is an optional lump sum bought before the SIP starts.

Only this call is checked: initial_amount and amount are each compared with the per-order cap, and the daily value cap
counts initial_amount if given, else one instalment (the SIP counts as one order). Later instalments are run by Kite on
its own schedule, outside this server's caps and audit log: stop a SIP with cancel_mf_sip.

{LEGACY_DOC}"""
)

MODIFY_SIP_DOC = prose(
    f"""\
Change a mutual fund SIP: its instalment amount, status (pause or resume), number of instalments, frequency or monthly
instalment_day. Give only what changes. {DRY_RUN_DOC}

The SIP is looked up first. The new instalment amount (or the current one, when you change something else) is compared
with the per-order cap; if the SIP cannot be read its value cannot be verified, so the change can be dry-run but never
sent live. A change that only pauses the SIP (status=paused and nothing else) reduces risk like a cancel: the value caps
do not apply, the confirmation step does. Resuming (status=active) does not count as risk-reducing. For `instalments`,
-1 should mean until cancelled (see place_mf_sip).

{LEGACY_DOC}"""
)

CANCEL_SIP_DOC = prose(
    f"""\
Cancel a mutual fund SIP so that no further instalments run. {DRY_RUN_DOC} Cancelling reduces risk, so the value caps
do not apply, but the confirmation step does. The SIP is looked up first so the answer shows what it is.

{LEGACY_DOC}"""
)


class MFLeg(OrderLeg):
    """An order leg for a mutual fund.

    `exchange` stays None, so the exchange / product / variety / order-type allow-lists never block a fund. The fund is
    named MF:<ISIN> for KITE_MCP_ALLOWED_INSTRUMENTS (and for the `allowlist` confirmation mode), so that list fails
    closed: someone who restricted trading to NSE:INFY has not thereby allowed every mutual fund."""

    @property
    def instrument(self) -> str | None:
        return f"MF:{self.tradingsymbol.upper()}" if self.tradingsymbol else None


# --- numbers and input checks -----------------------------------------------------------------------------------------


def plain(value: Decimal) -> str:
    """No exponent, no trailing zeros: how amounts and units go on the wire and into messages ("5000", "12.345")."""
    return format(value.normalize(), "f")


def rupees(value: Decimal) -> str:
    return f"₹{value:,.2f}"


def _d(value: Any) -> Decimal:
    return num(value) or Decimal(0)


def clean_isin(value: str) -> str:
    isin = value.strip().upper()
    hint = "find the fund's with search_mf_instruments"
    require(
        bool(ISIN.fullmatch(isin)), f"tradingsymbol {value!r} is not an ISIN (12 characters, e.g. INF000A01AA0); {hint}"
    )
    return isin


def clean_tag(tag: str | None) -> str | None:
    require(tag is None or bool(TAG.fullmatch(tag)), "tag must be 1 to 8 letters or digits (Kite's limit for MF tags)")
    return tag


def to_amount(value: float, name: str) -> Decimal:
    """INR: above 0, whole paise."""
    amount = dec(value)
    assert amount is not None
    require(amount > 0, f"{name} must be above 0")
    require(amount == amount.quantize(Decimal("0.01")), f"{name} {plain(amount)} has more than 2 decimals")
    return amount


def to_units(value: float, name: str) -> Decimal:
    """Units of a fund: above 0, at most 4 decimals (the fund's own multiple is checked once the fund is known)."""
    units = dec(value)
    assert units is not None
    require(units > 0, f"{name} must be above 0")
    require(units == units.quantize(Decimal("0.0001")), f"{name} {plain(units)} has more than 4 decimals")
    return units


def check_instalments(instalments: int | None) -> None:
    require(instalments in (None, -1) or instalments >= 1, "instalments must be -1 (until cancelled) or at least 1")


def day_problems(frequency: str | None, day: int | None) -> tuple[list[str], list[str]]:
    """(errors, warnings) for an instalment_day given the SIP's frequency (None: not known)."""
    if day is None:
        return [], []
    if frequency is None:
        return [], ["instalment_day only applies to monthly SIPs and this SIP's frequency is not known"]
    if frequency != "monthly":
        return [f"instalment_day only applies to monthly SIPs, not {frequency} ones"], []
    if day not in SIP_DAYS:
        days = ", ".join(map(str, SIP_DAYS))
        return [], [f"instalment_day {day} is not one of {days}, the days Kite's older docs list; Kite may reject it"]
    return [], []


# --- the fund, from the instrument master -----------------------------------------------------------------------------


async def lookup_fund(rt: Runtime, isin: str) -> tuple[MFInstrument | None, str | None]:
    """The fund, or (None, why the checks that need it were skipped). Never raises for a missing or broken master."""
    unchecked = "purchase limits, multiples and the NAV were not checked"
    if rt.instruments is None:
        return None, f"the MF instrument master is not loaded: {unchecked}"
    try:
        fund = await rt.instruments.lookup_mf(isin)
    except KiteMCPError as exc:  # InstrumentsUnavailable, or anything else of ours
        return None, f"the MF instrument master could not be read ({type(exc).__name__}): {unchecked}"
    if fund is None:
        unlisted = f"{isin} is not in the MF instrument list (see search_mf_instruments)"
        return None, f"{unlisted}: {unchecked}, and Kite will probably reject it"
    return fund, None


def fund_name(fund: MFInstrument) -> str:
    return f"{fund.name} ({fund.tradingsymbol})"


def fund_view(fund: MFInstrument) -> dict[str, Any]:
    """What a dry run shows about the fund, so the caller can see it is the one they meant."""
    return {
        "isin": fund.tradingsymbol,
        "name": fund.name,
        "amc": fund.amc,
        "plan": fund.plan,
        "scheme_type": fund.scheme_type,
        "dividend_type": fund.dividend_type,
        "purchase_allowed": fund.purchase_allowed,
        "redemption_allowed": fund.redemption_allowed,
        "minimum_purchase_amount_inr": fund.minimum_purchase_amount,
        "minimum_additional_purchase_amount_inr": fund.minimum_additional_purchase_amount,
        "purchase_amount_multiplier": fund.purchase_amount_multiplier,
        "minimum_redemption_quantity": fund.minimum_redemption_quantity,
        "redemption_quantity_multiplier": fund.redemption_quantity_multiplier,
        "last_nav_inr": fund.last_price,
        "last_nav_date": fund.last_price_date.isoformat() if fund.last_price_date else None,
    }


def closed_to_purchases(fund: MFInstrument | None) -> str | None:
    if fund is not None and not fund.purchase_allowed:
        return f"{fund_name(fund)} is closed to purchases (purchase_allowed is 0 in Kite's MF instrument list)"
    return None


def amount_problems(
    fund: MFInstrument | None, amount: Decimal, *, label: str, sip: bool = False
) -> tuple[list[str], list[str]]:
    """(errors, warnings) for a purchase amount against the fund's multiple and minimums.

    Kite lists a minimum for a first purchase and another for an additional one, and nothing says which this is, so only
    an amount below BOTH is an error; between the two it is a warning. SIP instalment minimums are not in the list at
    all, so for a SIP the additional minimum is only a hint."""
    if fund is None:
        return [], []
    errors: list[str] = []
    warnings: list[str] = []
    name = fund_name(fund)
    shown = f"{label} {rupees(amount)}"
    step = _d(fund.purchase_amount_multiplier)
    if step > 0 and amount % step != 0:
        errors.append(f"{shown} is not a multiple of {rupees(step)}, the purchase amount multiplier of {name}")
    first, more = _d(fund.minimum_purchase_amount), _d(fund.minimum_additional_purchase_amount)
    if sip:
        if amount < more:
            warnings.append(
                f"{shown} is below the {rupees(more)} minimum additional purchase of {name}; SIP minimums are not in "
                "Kite's instrument list, so Kite decides"
            )
    elif amount < min(first, more):
        errors.append(
            f"{shown} is below the minimum purchase of {name}: {rupees(first)} for a first purchase, "
            f"{rupees(more)} for an additional one"
        )
    elif amount < first:
        warnings.append(
            f"{shown} is below the {rupees(first)} minimum for a first purchase of {name}; Kite accepts it only as an "
            f"additional purchase (minimum {rupees(more)}) into a folio you already hold"
        )
    elif amount < more:
        warnings.append(
            f"{shown} is below the {rupees(more)} minimum for an additional purchase of {name}; Kite accepts it only "
            f"as a first purchase (minimum {rupees(first)})"
        )
    return errors, warnings


def redemption_problems(fund: MFInstrument | None, units: Decimal) -> tuple[list[str], list[str]]:
    if fund is None:
        return [], []
    errors: list[str] = []
    warnings: list[str] = []
    name = fund_name(fund)
    if not fund.redemption_allowed:
        errors.append(f"{name} is closed to redemptions (redemption_allowed is 0 in Kite's MF instrument list)")
    step = _d(fund.redemption_quantity_multiplier)
    if step > 0 and units % step != 0:
        errors.append(
            f"quantity {plain(units)} is not a multiple of {plain(step)} units, "
            f"the redemption quantity multiplier of {name}"
        )
    least = _d(fund.minimum_redemption_quantity)
    if units < least:
        warnings.append(
            f"quantity {plain(units)} is below the minimum redemption quantity of {plain(least)} units of {name}; "
            "Kite accepts that only when it is your entire holding"
        )
    return errors, warnings


def purchase_leg(isin: str, amount: Decimal, note: str) -> MFLeg:
    return MFLeg(tradingsymbol=isin, transaction_type="BUY", value=money(amount), value_note=note)


def redemption_leg(isin: str, fund: MFInstrument | None, units: Decimal, today: dt.date) -> tuple[MFLeg, list[str]]:
    """The SELL leg, valued at the fund's last NAV; no NAV means no value, which the policy refuses live."""
    nav = _d(fund.last_price) if fund is not None else Decimal(0)
    if fund is None or nav <= 0:
        why = "the fund's last NAV is not known here" if fund is None else "Kite lists no NAV for this fund"
        return MFLeg(tradingsymbol=isin, transaction_type="SELL", value=None, value_note=f"units x last NAV: {why}"), []
    when = fund.last_price_date
    note = f"units x last NAV ₹{plain(nav)}" + (f" of {when.isoformat()}" if when else "")
    note += "; Kite prices a redemption at the NAV of the day it processes it"
    leg = MFLeg(tradingsymbol=isin, transaction_type="SELL", price=nav, value=money(units * nav), value_note=note)
    warnings: list[str] = []
    if when is None or (today - when).days > STALE_NAV_DAYS:
        age = "of unknown date" if when is None else f"from {when.isoformat()}"
        warnings.append(f"the last NAV of {fund_name(fund)} is {age}, so the value used for the caps may be off")
    return leg, warnings


# --- records read from Kite -------------------------------------------------------------------------------------------


def rows(data: Any) -> list[dict[str, Any]]:
    return [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []


def as_record(data: Any) -> dict[str, Any] | None:
    """The one record of an answer that should be a single object (a list: its last element)."""
    if isinstance(data, list):
        data = data[-1] if data else None
    return data if isinstance(data, dict) and data else None


async def fetch_record(rt: Runtime, path: str, what: str) -> tuple[dict[str, Any] | None, str | None]:
    """(record, None), or (None, a warning saying why not). A dead session is raised: that is not "not found"."""
    try:
        record = as_record(await rt.client.get(path))
    except (NotLoggedIn, TokenError):
        raise
    except KiteMCPError as exc:
        return None, f"{what} could not be read from Kite ({type(exc).__name__})"
    return (record, None) if record else (None, f"{what} was not found")


def describe_order(order: dict[str, Any]) -> str:
    fund = order.get("fund") or order.get("tradingsymbol") or "?"
    if str(order.get("transaction_type") or "").upper() == "SELL":
        what = f"SELL {order.get('quantity')} units"
    else:
        what = f"BUY {rupees(_d(order.get('amount')))}"
    return f"{what} of {fund}, {order.get('status')}"


def describe_sip(sip: dict[str, Any]) -> str:
    fund = sip.get("fund") or sip.get("tradingsymbol") or "?"
    return f"{rupees(_d(sip.get('instalment_amount')))} {sip.get('frequency')} into {fund}, {sip.get('status')}"


def leg_of(record: dict[str, Any]) -> MFLeg:
    """The leg for cancelling an order or SIP: which fund and side, no value (cancels are risk-reducing)."""
    return MFLeg(
        tradingsymbol=str(record.get("tradingsymbol") or "").upper() or None,
        transaction_type=str(record.get("transaction_type") or "").upper() or None,
    )


# --- the write requests -----------------------------------------------------------------------------------------------


async def order_request(
    rt: Runtime,
    tradingsymbol: str,
    transaction_type: str,
    amount: float | None,
    quantity: float | None,
    tag: str | None,
) -> WriteRequest:
    isin = clean_isin(tradingsymbol)
    tag = clean_tag(tag)
    if transaction_type == "BUY":
        if amount is None or quantity is not None:
            raise ToolInputError("a BUY takes amount (INR) and no quantity; units are for SELL")
        return await purchase_request(rt, isin, to_amount(amount, "amount"), tag)
    if quantity is None or amount is not None:
        raise ToolInputError("a SELL takes quantity (units) and no amount; the amount is for BUY")
    return await redemption_request(rt, isin, to_units(quantity, "quantity"), tag)


async def purchase_request(rt: Runtime, isin: str, amount: Decimal, tag: str | None) -> WriteRequest:
    fund, note = await lookup_fund(rt, isin)
    errors, warnings = amount_problems(fund, amount, label="amount")
    if closed := closed_to_purchases(fund):
        errors.append(closed)
    if errors:
        raise ToolInputError("; ".join(errors))
    data = {"tradingsymbol": isin, "transaction_type": "BUY", "amount": plain(amount), "tag": tag}
    return WriteRequest(
        tool="place_mf_order",
        method="POST",
        path="/mf/orders",
        data={k: v for k, v in data.items() if v is not None},
        summary=f"BUY {rupees(amount)} of {fund_name(fund) if fund else isin}",
        legs=[purchase_leg(isin, amount, "purchase amount")],
        counts_as_order=True,
        preview={"fund": fund_view(fund)} if fund else {},
        warnings=[*([note] if note else []), *warnings, LEGACY_WARNING],
    )


async def redemption_request(rt: Runtime, isin: str, units: Decimal, tag: str | None) -> WriteRequest:
    fund, note = await lookup_fund(rt, isin)
    errors, warnings = redemption_problems(fund, units)
    if errors:
        raise ToolInputError("; ".join(errors))
    leg, nav_warnings = redemption_leg(isin, fund, units, rt.clock().date())
    data = {"tradingsymbol": isin, "transaction_type": "SELL", "quantity": plain(units), "tag": tag}
    return WriteRequest(
        tool="place_mf_order",
        method="POST",
        path="/mf/orders",
        data={k: v for k, v in data.items() if v is not None},
        summary=f"SELL {plain(units)} units of {fund_name(fund) if fund else isin}",
        legs=[leg],
        counts_as_order=True,
        preview={"fund": fund_view(fund)} if fund else {},
        warnings=[*([note] if note else []), *warnings, *nav_warnings, LEGACY_WARNING],
    )


async def cancel_order_request(rt: Runtime, order_id: str) -> WriteRequest:
    order, why = await fetch_record(rt, f"/mf/orders/{order_id}", f"order {order_id}")
    warnings = [why] if why else []
    summary = f"cancel MF order {order_id}"
    legs: list[OrderLeg] = []
    if order:
        if (status := str(order.get("status") or "").upper()) in ("COMPLETE", "REJECTED", "CANCELLED"):
            warnings.append(f"the order is already {status}: there is nothing left to cancel")
        summary += f" ({describe_order(order)})"
        legs = [leg_of(order)]
    return WriteRequest(
        tool="cancel_mf_order",
        method="DELETE",
        path=f"/mf/orders/{order_id}",
        summary=summary,
        legs=legs,
        risk_reducing=True,
        order_id=order_id,
        preview={"order": order} if order else {},
        warnings=[*warnings, LEGACY_WARNING],
    )


async def sip_request(
    rt: Runtime,
    tradingsymbol: str,
    amount: float,
    instalments: int,
    frequency: str,
    initial_amount: float | None,
    instalment_day: int | None,
    tag: str | None,
) -> WriteRequest:
    isin = clean_isin(tradingsymbol)
    tag = clean_tag(tag)
    each = to_amount(amount, "amount")
    first = to_amount(initial_amount, "initial_amount") if initial_amount is not None else None
    check_instalments(instalments)
    errors, warnings = day_problems(frequency, instalment_day)

    fund, note = await lookup_fund(rt, isin)
    if note:
        warnings.append(note)
    if closed := closed_to_purchases(fund):
        errors.append(closed)
    more_errors, more_warnings = amount_problems(fund, each, label="amount", sip=True)
    errors += more_errors
    warnings += more_warnings
    if first is not None:
        more_errors, more_warnings = amount_problems(fund, first, label="initial_amount")
        errors += more_errors
        warnings += more_warnings
    if errors:
        raise ToolInputError("; ".join(errors))

    counted = money(first if first is not None else each)
    legs: list[OrderLeg] = [purchase_leg(isin, first, "initial lump sum")] if first is not None else []
    legs.append(purchase_leg(isin, each, "one SIP instalment"))
    runs = "until cancelled" if instalments == -1 else f"{instalments} instalments"
    commitment: dict[str, Any] = {
        "instalment_inr": each,
        "frequency": frequency,
        "instalments": runs if instalments == -1 else instalments,
        "counted_toward_the_daily_cap_inr": counted,
    }
    if instalments > 0:
        commitment["total_of_all_instalments_inr"] = money(each * instalments)
    if first is not None:
        commitment["initial_amount_inr"] = first
    warnings.append(
        "Kite runs the later instalments on its own schedule: the caps here apply to this call only, so this SIP can "
        "spend more than they allow until it is cancelled (cancel_mf_sip)"
    )
    data: dict[str, Any] = {
        "tradingsymbol": isin,
        "amount": plain(each),
        "instalments": instalments,
        "frequency": frequency,
        "initial_amount": plain(first) if first is not None else None,
        "instalment_day": instalment_day,
        "tag": tag,
    }
    summary = f"SIP of {rupees(each)} {frequency} ({runs}) into {fund_name(fund) if fund else isin}"
    if first is not None:
        summary += f", starting with {rupees(first)}"
    return WriteRequest(
        tool="place_mf_sip",
        method="POST",
        path="/mf/sips",
        data={k: v for k, v in data.items() if v is not None},
        summary=summary,
        legs=legs,
        counts_as_order=True,
        daily_value=counted,
        preview={**({"fund": fund_view(fund)} if fund else {}), "commitment": commitment},
        warnings=[*warnings, LEGACY_WARNING],
    )


def change_summary(
    sip_id: str,
    sip: dict[str, Any] | None,
    amount: Decimal | None,
    status: str | None,
    instalments: int | None,
    frequency: str | None,
    instalment_day: int | None,
) -> str:
    changes = [
        f"amount {rupees(amount)}" if amount is not None else None,
        f"status {status}" if status else None,
        None if instalments is None else ("until cancelled" if instalments == -1 else f"{instalments} instalments"),
        frequency,
        f"day {instalment_day}" if instalment_day else None,
    ]
    what = f" ({describe_sip(sip)})" if sip else ""
    return f"modify SIP {sip_id}{what}: " + ", ".join(c for c in changes if c)


async def sip_change_request(
    rt: Runtime,
    sip_id: str,
    amount: float | None,
    status: str | None,
    instalments: int | None,
    frequency: str | None,
    instalment_day: int | None,
) -> WriteRequest:
    given = (amount, status, instalments, frequency, instalment_day)
    require(any(v is not None for v in given), "nothing to modify: give at least one new value")
    check_instalments(instalments)
    new_amount = to_amount(amount, "amount") if amount is not None else None

    sip, why = await fetch_record(rt, f"/mf/sips/{sip_id}", f"SIP {sip_id}")
    warnings = [f"{why}: its fund and current instalment are not known"] if why else []
    isin = str(sip.get("tradingsymbol") or "").upper() if sip else ""
    shown_frequency = str(sip.get("frequency") or "").lower() or None if sip else None
    errors, more_warnings = day_problems(frequency or shown_frequency, instalment_day)
    warnings += more_warnings
    if sip:
        current_status = str(sip.get("status") or "").upper()
        if current_status == "CANCELLED":
            warnings.append("the SIP is CANCELLED: Kite will probably refuse to change it")
        elif status and current_status == status.upper():
            warnings.append(f"the SIP is already {current_status}")
    if new_amount is not None and isin:
        fund, note = await lookup_fund(rt, isin)
        if note:
            warnings.append(note)
        more_errors, more_warnings = amount_problems(fund, new_amount, label="amount", sip=True)
        errors += more_errors
        warnings += more_warnings
        if closed := closed_to_purchases(fund):
            warnings.append(f"{closed}: the SIP's instalments will fail")
    if errors:
        raise ToolInputError("; ".join(errors))

    current = num(sip.get("instalment_amount")) if sip else None
    instalment = new_amount if new_amount is not None else current
    if sip is None:
        leg = MFLeg(transaction_type="BUY", value=None, value_note="the SIP could not be read, so its fund is unknown")
    else:
        note = "new SIP instalment" if new_amount is not None else "current SIP instalment (unchanged)"
        leg = MFLeg(
            tradingsymbol=isin or None,
            transaction_type=str(sip.get("transaction_type") or "BUY").upper(),
            value=money(instalment) if instalment is not None else None,
            value_note=note if instalment is not None else "the SIP lists no instalment_amount",
        )
    data: dict[str, Any] = {
        "amount": plain(new_amount) if new_amount is not None else None,
        "status": status,
        "instalments": instalments,
        "frequency": frequency,
        "instalment_day": instalment_day,
    }
    pausing_only = status == "paused" and all(v is None for v in (amount, instalments, frequency, instalment_day))
    if pausing_only:
        warnings.append("this only pauses the SIP, so it counts as risk-reducing: the value caps do not apply")
    return WriteRequest(
        tool="modify_mf_sip",
        method="PUT",
        path=f"/mf/sips/{sip_id}",
        data={k: v for k, v in data.items() if v is not None},
        summary=change_summary(sip_id, sip, new_amount, status, instalments, frequency, instalment_day),
        legs=[leg],
        risk_reducing=pausing_only,
        order_id=sip_id,
        preview={"current_sip": sip} if sip else {},
        warnings=[*warnings, LEGACY_WARNING],
    )


async def cancel_sip_request(rt: Runtime, sip_id: str) -> WriteRequest:
    sip, why = await fetch_record(rt, f"/mf/sips/{sip_id}", f"SIP {sip_id}")
    warnings = [why] if why else []
    summary = f"cancel SIP {sip_id}"
    legs: list[OrderLeg] = []
    if sip:
        if str(sip.get("status") or "").upper() == "CANCELLED":
            warnings.append("the SIP is already CANCELLED: there is nothing left to cancel")
        summary += f" ({describe_sip(sip)})"
        legs = [leg_of(sip)]
    return WriteRequest(
        tool="cancel_mf_sip",
        method="DELETE",
        path=f"/mf/sips/{sip_id}",
        summary=summary,
        legs=legs,
        risk_reducing=True,
        order_id=sip_id,
        preview={"sip": sip} if sip else {},
        warnings=[*warnings, LEGACY_WARNING],
    )


# --- summaries of what Kite returns -----------------------------------------------------------------------------------


def holding_value(holding: dict[str, Any]) -> Decimal:
    units, nav = num(holding.get("quantity")), num(holding.get("last_price"))
    return units * nav if units is not None and nav is not None else Decimal(0)


def holdings_summary(holdings: list[dict[str, Any]]) -> dict[str, Any]:
    """Totals over ALL holdings (not just the page): invested = units x average_price, value = units x last_price."""
    invested = Decimal(0)
    current = Decimal(0)
    priced = 0
    for h in holdings:
        units, cost, nav = num(h.get("quantity")), num(h.get("average_price")), num(h.get("last_price"))
        if units is None or cost is None or nav is None:
            continue
        priced += 1
        invested += units * cost
        current += units * nav
    summary: dict[str, Any] = {
        "funds": len(holdings),
        "invested_inr": money(invested),
        "current_value_inr": money(current),
        "pnl_inr": money(current - invested),
    }
    if invested > 0:
        summary["pnl_percent"] = money((current - invested) / invested * 100)
    if priced < len(holdings):
        summary["funds_without_prices"] = len(holdings) - priced
    summary["note"] = "invested = units x average_price; current value = units x last_price (the last available NAV)"
    return summary


def status_counts(items: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        key = str(item.get("status") or "UNKNOWN").upper()
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


# --- registration -----------------------------------------------------------------------------------------------------


def register(reg: Registrar) -> None:
    _register_reads(reg)
    if reg.rt.settings.allow_mf_writes:
        _register_writes(reg)
    else:
        for name in WRITE_TOOLS:  # the registrar's own reason first (toolset off, excluded, read-only), else the flag
            reg.skipped[name] = reg.enabled(name, "mf", "write") or WRITES_OFF


def _register_reads(reg: Registrar) -> None:
    rt = reg.rt

    @reg.tool("get_mf_holdings", title="Mutual fund holdings", toolset="mf", kind="read")
    async def get_mf_holdings(limit: Limit = 100, offset: Offset = 0) -> Any:
        """Mutual fund units in your demat account, largest first. Each holding has tradingsymbol (the fund's ISIN),
        fund (name), folio, quantity (units), average_price (INR per unit paid), last_price (the last available NAV,
        INR per unit), last_price_date, pnl and pledged_quantity. `summary` covers ALL holdings, not just the page:
        invested and current value in INR, the profit or loss and its percentage."""
        holdings = rows(await rt.client.get("/mf/holdings"))
        holdings.sort(key=lambda h: (-holding_value(h), str(h.get("fund") or "")))
        return paginate(holdings, limit, offset, summary=holdings_summary(holdings))

    @reg.tool("get_mf_orders", title="Mutual fund orders", toolset="mf", kind="read")
    async def get_mf_orders(
        order_id: Annotated[
            str | None,
            Field(pattern=ID_PATTERN, description="one order, whatever its age (the list only goes back 7 days)"),
        ] = None,
        status: Annotated[
            str | None,
            Field(
                max_length=32,
                description="list only: keep orders in this status, case-insensitive (COMPLETE, REJECTED, CANCELLED, "
                "OPEN...; status_counts shows which exist)",
            ),
        ] = None,
        limit: Limit = 100,
        offset: Offset = 0,
    ) -> Any:
        """Mutual fund orders. With order_id: that one order, however old. Without: every order of the last 7 days
        (open and executed, lump sums and SIP instalments), newest first, optionally filtered by status, with
        `status_counts` over the whole window. Each order has order_id, status, status_message, tradingsymbol (ISIN),
        fund, transaction_type, amount (INR), quantity (units allotted or sold), average_price (the NAV used),
        last_price (latest NAV), variety (regular, sip, amc_sip), folio and tag."""
        if order_id:
            return await rt.client.get(f"/mf/orders/{order_id}")
        orders = rows(await rt.client.get("/mf/orders"))
        counts = status_counts(orders)
        if status:
            orders = [o for o in orders if str(o.get("status") or "").upper() == status.strip().upper()]
        orders.sort(key=lambda o: str(o.get("order_timestamp") or ""), reverse=True)
        return paginate(orders, limit, offset, window="last 7 days", status_counts=counts)

    @reg.tool("get_mf_sips", title="Mutual fund SIPs", toolset="mf", kind="read")
    async def get_mf_sips(
        sip_id: Annotated[str | None, Field(pattern=ID_PATTERN, description="one SIP; omit to list them all")] = None,
        limit: Limit = 100,
        offset: Offset = 0,
    ) -> Any:
        """Mutual fund SIPs (recurring purchases): with sip_id one SIP, else all active and paused ones with
        `status_counts`. Each has sip_id, tradingsymbol (ISIN), fund, status (ACTIVE, PAUSED, CANCELLED),
        instalment_amount (INR), frequency (weekly, monthly, quarterly), instalment_day (monthly only, else 0),
        instalments (the number planned; -1 means until cancelled), completed_instalments, pending_instalments,
        next_instalment, last_instalment and tag."""
        if sip_id:
            return await rt.client.get(f"/mf/sips/{sip_id}")
        sips = rows(await rt.client.get("/mf/sips"))
        return paginate(sips, limit, offset, status_counts=status_counts(sips))


def _register_writes(reg: Registrar) -> None:
    rt = reg.rt

    @reg.tool(
        "place_mf_order",
        title="Buy or redeem a mutual fund (dry run by default)",
        toolset="mf",
        kind="write",
        description=PLACE_ORDER_DOC,
    )
    async def place_mf_order(
        tradingsymbol: FundIsin,
        transaction_type: Annotated[Side, Field(description="BUY (give amount) or SELL, i.e. redeem (give quantity)")],
        amount: Annotated[
            float | None, Field(gt=0, le=MAX_AMOUNT, description="BUY only: INR to invest, up to 2 decimals")
        ] = None,
        quantity: Annotated[
            float | None, Field(gt=0, le=MAX_AMOUNT, description="SELL only: units to redeem, fractional (e.g. 12.345)")
        ] = None,
        tag: Tag = None,
        dry_run: Annotated[bool, DRY_RUN] = True,
        confirmation_token: Annotated[str | None, CONFIRM] = None,
    ) -> Any:
        request = await order_request(rt, tradingsymbol, transaction_type, amount, quantity, tag)
        return await rt.pipeline.run(request, dry_run=dry_run, confirmation_token=confirmation_token)

    @reg.tool(
        "cancel_mf_order",
        title="Cancel a mutual fund order (dry run by default)",
        toolset="mf",
        kind="write",
        description=CANCEL_ORDER_DOC,
    )
    async def cancel_mf_order(
        order_id: OrderId,
        dry_run: Annotated[bool, DRY_RUN] = True,
        confirmation_token: Annotated[str | None, CONFIRM] = None,
    ) -> Any:
        request = await cancel_order_request(rt, order_id)
        return await rt.pipeline.run(request, dry_run=dry_run, confirmation_token=confirmation_token)

    @reg.tool(
        "place_mf_sip",
        title="Start a mutual fund SIP (dry run by default)",
        toolset="mf",
        kind="write",
        description=PLACE_SIP_DOC,
    )
    async def place_mf_sip(
        tradingsymbol: FundIsin,
        amount: Annotated[float, Field(gt=0, le=MAX_AMOUNT, description="INR per instalment, up to 2 decimals")],
        instalments: Annotated[
            int, Field(ge=-1, le=9999, description="number of instalments (1 or more), or -1 to run until cancelled")
        ],
        frequency: Annotated[Frequency, Field(description="weekly, monthly or quarterly")],
        initial_amount: Annotated[
            float | None, Field(gt=0, le=MAX_AMOUNT, description="optional INR lump sum bought before the SIP starts")
        ] = None,
        instalment_day: InstalmentDay = None,
        tag: Tag = None,
        dry_run: Annotated[bool, DRY_RUN] = True,
        confirmation_token: Annotated[str | None, CONFIRM] = None,
    ) -> Any:
        request = await sip_request(
            rt, tradingsymbol, amount, instalments, frequency, initial_amount, instalment_day, tag
        )
        return await rt.pipeline.run(request, dry_run=dry_run, confirmation_token=confirmation_token)

    @reg.tool(
        "modify_mf_sip",
        title="Modify a mutual fund SIP (dry run by default)",
        toolset="mf",
        kind="write",
        description=MODIFY_SIP_DOC,
    )
    async def modify_mf_sip(
        sip_id: SipId,
        amount: Annotated[
            float | None, Field(gt=0, le=MAX_AMOUNT, description="new INR per instalment, up to 2 decimals")
        ] = None,
        status: Annotated[SipStatus | None, Field(description="paused to pause the SIP, active to resume it")] = None,
        instalments: Annotated[
            int | None, Field(ge=-1, le=9999, description="new number of instalments, or -1 to run until cancelled")
        ] = None,
        frequency: Annotated[Frequency | None, Field(description="new frequency: weekly, monthly or quarterly")] = None,
        instalment_day: InstalmentDay = None,
        dry_run: Annotated[bool, DRY_RUN] = True,
        confirmation_token: Annotated[str | None, CONFIRM] = None,
    ) -> Any:
        request = await sip_change_request(rt, sip_id, amount, status, instalments, frequency, instalment_day)
        return await rt.pipeline.run(request, dry_run=dry_run, confirmation_token=confirmation_token)

    @reg.tool(
        "cancel_mf_sip",
        title="Cancel a mutual fund SIP (dry run by default)",
        toolset="mf",
        kind="write",
        description=CANCEL_SIP_DOC,
    )
    async def cancel_mf_sip(
        sip_id: SipId,
        dry_run: Annotated[bool, DRY_RUN] = True,
        confirmation_token: Annotated[str | None, CONFIRM] = None,
    ) -> Any:
        request = await cancel_sip_request(rt, sip_id)
        return await rt.pipeline.run(request, dry_run=dry_run, confirmation_token=confirmation_token)
