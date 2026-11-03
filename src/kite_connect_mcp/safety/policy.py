"""The rules a write must pass. Pure functions over the settings, the request and today's usage."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from ..config import Settings
from .audit import Usage


@dataclass(frozen=True)
class OrderLeg:
    """What the policy needs to know about one order a write would create or change."""

    exchange: str | None = None
    tradingsymbol: str | None = None
    transaction_type: str | None = None
    product: str | None = None
    order_type: str | None = None
    variety: str | None = None
    quantity: int | None = None
    price: Decimal | None = None  # the price the value was computed at
    value: Decimal | None = None  # notional in INR (quantity x price, or an MF amount); None = cannot be known
    value_note: str = ""

    @property
    def instrument(self) -> str | None:
        if self.exchange and self.tradingsymbol:
            return f"{self.exchange.upper()}:{self.tradingsymbol.upper()}"
        return None


@dataclass
class Verdict:
    violations: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.violations


def _allowed(allow: frozenset[str] | None, value: str | None, label: str, verdict: Verdict, *, lower=False) -> None:
    if allow is None or value is None:
        return
    probe = value.lower() if lower else value.upper()
    if probe not in allow:
        verdict.violations.append(f"{label} {value} is not in the allow-list ({', '.join(sorted(allow))})")


def evaluate(
    settings: Settings,
    legs: list[OrderLeg],
    *,
    usage: Usage,
    counts_as_order: bool,
    order_count: int,
    daily_value: Decimal | None,
    risk_reducing: bool,
    kill_switch_on: bool,
) -> Verdict:
    verdict = Verdict()
    if kill_switch_on:
        verdict.violations.append(f"the kill switch is on ({settings.kill_switch_file} exists): every write is refused")
    for leg in legs:
        _allowed(settings.allowed_exchanges, leg.exchange, "exchange", verdict)
        _allowed(settings.allowed_products, leg.product, "product", verdict)
        _allowed(settings.allowed_varieties, leg.variety, "variety", verdict, lower=True)
        _allowed(settings.allowed_order_types, leg.order_type, "order_type", verdict)
        _allowed(settings.allowed_transaction_types, leg.transaction_type, "transaction_type", verdict)
        allowed = settings.allowed_instruments
        if allowed is not None and leg.instrument is not None and leg.instrument not in allowed:
            verdict.violations.append(f"instrument {leg.instrument} is not in KITE_MCP_ALLOWED_INSTRUMENTS")
        if risk_reducing:
            continue
        if settings.max_quantity is not None and leg.quantity is not None and leg.quantity > settings.max_quantity:
            verdict.violations.append(
                f"quantity {leg.quantity} is above KITE_MCP_MAX_QUANTITY ({settings.max_quantity})"
            )
        if leg.value is None:
            verdict.violations.append(
                f"the value of {leg.instrument or 'this order'} cannot be verified against the per-order cap"
                + (f" ({leg.value_note})" if leg.value_note else "")
            )
        elif leg.value > settings.max_order_value:
            verdict.violations.append(
                f"order value ₹{leg.value:,.2f} is above the per-order cap ₹{settings.max_order_value:,.2f} "
                "(KITE_MCP_MAX_ORDER_VALUE)"
            )
    if counts_as_order and not risk_reducing:
        if usage.orders + order_count > settings.max_orders_per_day:
            verdict.violations.append(
                f"{usage.orders} live orders today; {order_count} more would pass the daily limit of "
                f"{settings.max_orders_per_day} (KITE_MCP_MAX_ORDERS_PER_DAY)"
            )
        added = daily_value if daily_value is not None else sum((leg.value or Decimal(0) for leg in legs), Decimal(0))
        if usage.value + added > settings.max_daily_value:
            verdict.violations.append(
                f"₹{usage.value:,.2f} placed live today; ₹{added:,.2f} more would pass the daily cap "
                f"₹{settings.max_daily_value:,.2f} (KITE_MCP_MAX_DAILY_VALUE)"
            )
    return verdict


def preauthorised(settings: Settings, legs: list[OrderLeg]) -> bool:
    """In `allowlist` confirmation mode, a write whose every leg names an explicitly allow-listed instrument needs no
    confirmation token. Writes without legs (or legs without an instrument) always need one."""
    if settings.live_confirmation != "allowlist" or settings.allowed_instruments is None or not legs:
        return False
    return all(leg.instrument is not None and leg.instrument in settings.allowed_instruments for leg in legs)
