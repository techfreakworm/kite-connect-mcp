"""Helpers for the order-events tests: Kite order rows with placeholder data, and a clock that tests move."""

from __future__ import annotations

import datetime as dt
from typing import Any

from conftest import FIXED_NOW

ORDER_ID = "250101000000001"

ORDER_ROW: dict[str, Any] = {  # the shape of a row of GET /orders (Kite Connect v3), placeholder values
    "placed_by": "AB1234",
    "user_id": "AB1234",
    "app_id": 1234,
    "checksum": "",
    "guid": "XXXXXX",
    "order_id": ORDER_ID,
    "exchange_order_id": "1100000000000001",
    "parent_order_id": None,
    "status": "OPEN",
    "status_message": None,
    "status_message_raw": None,
    "order_timestamp": "2026-11-04 09:30:00",
    "exchange_update_timestamp": "2026-11-04 09:30:01",
    "exchange_timestamp": "2026-11-04 09:30:01",
    "variety": "regular",
    "modified": False,
    "exchange": "NSE",
    "tradingsymbol": "ABCD",
    "instrument_token": 12345,
    "order_type": "LIMIT",
    "transaction_type": "BUY",
    "validity": "DAY",
    "validity_ttl": 0,
    "product": "CNC",
    "quantity": 25,
    "disclosed_quantity": 0,
    "price": 1234.5,
    "trigger_price": 0,
    "average_price": 0,
    "filled_quantity": 0,
    "pending_quantity": 25,
    "cancelled_quantity": 0,
    "market_protection": 0,
    "meta": {},
    "tag": "mytag",
    "tags": ["mytag"],
}


def order_row(**changes: Any) -> dict[str, Any]:
    return {**ORDER_ROW, **changes}


def filled_row(**changes: Any) -> dict[str, Any]:
    """A fully filled order; `changes` override any field, the filled ones too."""
    filled = {
        "status": "COMPLETE",
        "filled_quantity": 25,
        "pending_quantity": 0,
        "average_price": 1234.5,
        "exchange_update_timestamp": "2026-11-04 09:31:00",
    }
    return order_row(**{**filled, **changes})


class Clock:
    """A clock the test moves by hand."""

    def __init__(self, now: dt.datetime = FIXED_NOW) -> None:
        self.now = now

    def __call__(self) -> dt.datetime:
        return self.now

    def advance(self, seconds: float) -> dt.datetime:
        self.now += dt.timedelta(seconds=seconds)
        return self.now
