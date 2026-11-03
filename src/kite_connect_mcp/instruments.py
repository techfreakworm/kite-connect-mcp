"""The instrument master: Kite's daily CSV dump of every tradable instrument, cached locally.

INTERFACE CONTRACT (used by the order tools for pre-trade checks; keep it stable):

    store = InstrumentStore(settings, client, clock=...)
    await store.lookup(exchange, tradingsymbol) -> Instrument | None      # exact, case-insensitive
    await store.by_token(instrument_token) -> Instrument | None
    await store.search(query, *, exchange=None, segment=None, instrument_type=None, expiry=None, limit=20)
        -> list[Instrument]

(Placeholder: the store itself is filled in by its implementation; `lookup` returning None makes the order tools
skip the tick-size / lot-size checks and say so in a warning.)
"""

from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any


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


class InstrumentStore:  # pragma: no cover - replaced by the real implementation
    async def lookup(self, exchange: str, tradingsymbol: str) -> Instrument | None:
        return None

    async def by_token(self, instrument_token: int) -> Instrument | None:
        return None

    async def search(self, query: str, **filters: Any) -> list[Instrument]:
        return []
