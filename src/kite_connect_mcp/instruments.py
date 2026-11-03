"""The instrument master: Kite's daily CSV dump of every tradable instrument, cached locally.

INTERFACE CONTRACT (used by the order and MF tools; keep it stable):

    store = InstrumentStore(settings, client, clock=...)   # cheap: nothing is loaded until first use
    await store.lookup(exchange, tradingsymbol) -> Instrument | None      # exact, case-insensitive
    await store.by_token(instrument_token) -> Instrument | None
    await store.search(query, *, exchange=None, segment=None, instrument_type=None, expiry=None, limit=20)
        -> list[Instrument]
    await store.lookup_mf(tradingsymbol) -> MFInstrument | None           # by ISIN-like MF tradingsymbol
    await store.search_mf(query, *, amc=None, plan=None, scheme_type=None, limit=20) -> list[MFInstrument]
    await store.status() -> dict                                           # loaded date, counts, file paths
    await store.refresh(force=False) -> dict                               # (re)download today's dumps

`lookup` returns None only when the instrument list is loaded and the symbol is not in it. When the list cannot be
loaded at all, the methods raise errors.InstrumentsUnavailable, and the order tools then skip the tick-size / lot-size
checks with a warning instead of refusing.

(Placeholder: the store itself is filled in by its implementation.)
"""

from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any

from .errors import InstrumentsUnavailable


@dataclass(frozen=True)
class Instrument:
    instrument_token: int
    exchange_token: int
    tradingsymbol: str
    name: str
    last_price: float
    expiry: dt.date | None
    strike: float
    tick_size: Decimal
    lot_size: int
    instrument_type: str  # EQ, FUT, CE, PE
    segment: str  # NSE, NFO-FUT, NFO-OPT, INDICES, ...
    exchange: str

    @property
    def key(self) -> str:
        return f"{self.exchange}:{self.tradingsymbol}"

    def as_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["expiry"] = self.expiry.isoformat() if self.expiry else None
        out["tick_size"] = float(self.tick_size)
        out["key"] = self.key
        return out


@dataclass(frozen=True)
class MFInstrument:
    tradingsymbol: str  # the ISIN, e.g. INF846K01EW2
    amc: str
    name: str
    purchase_allowed: bool
    redemption_allowed: bool
    minimum_purchase_amount: Decimal
    purchase_amount_multiplier: Decimal
    minimum_additional_purchase_amount: Decimal
    minimum_redemption_quantity: Decimal
    redemption_quantity_multiplier: Decimal
    dividend_type: str
    scheme_type: str
    plan: str
    settlement_type: str
    last_price: Decimal
    last_price_date: dt.date | None

    def as_dict(self) -> dict[str, Any]:
        out = asdict(self)
        for key, value in out.items():
            if isinstance(value, Decimal):
                out[key] = float(value)
        out["last_price_date"] = self.last_price_date.isoformat() if self.last_price_date else None
        return out


class InstrumentStore:  # pragma: no cover - replaced by the real implementation
    def __init__(self, settings: Any = None, client: Any = None, *, clock: Any = None):
        self.settings, self.client, self.clock = settings, client, clock

    async def lookup(self, exchange: str, tradingsymbol: str) -> Instrument | None:
        raise InstrumentsUnavailable("the instrument master is not implemented yet")

    async def by_token(self, instrument_token: int) -> Instrument | None:
        raise InstrumentsUnavailable("the instrument master is not implemented yet")

    async def search(self, query: str, **filters: Any) -> list[Instrument]:
        raise InstrumentsUnavailable("the instrument master is not implemented yet")

    async def lookup_mf(self, tradingsymbol: str) -> MFInstrument | None:
        raise InstrumentsUnavailable("the instrument master is not implemented yet")

    async def search_mf(self, query: str, **filters: Any) -> list[MFInstrument]:
        raise InstrumentsUnavailable("the instrument master is not implemented yet")

    async def status(self) -> dict[str, Any]:
        return {"loaded": False}

    async def refresh(self, force: bool = False) -> dict[str, Any]:
        return {"loaded": False}
