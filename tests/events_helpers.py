"""Helpers for the order-events tests: Kite order rows with placeholder data, a clock that tests move, a consumer
program, a consumers file and an event service on a runtime wired to FakeKite."""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
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


def _python_wrapper() -> str:
    """A consumer program for the tests: a tiny script in a private temp folder that runs this interpreter.

    The consumers file refuses a program in a folder others can write (events/trust.py check_owned_file), and the
    interpreter of a virtualenv or a CI tool cache may sit in such a folder; a script in a mkdtemp folder never does."""
    folder = tempfile.mkdtemp(prefix="kite-events-tests-")
    path = os.path.join(folder, "python")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    os.chmod(path, 0o700)
    return path


PY = _python_wrapper()


def folders(parent: Path, *names: str) -> list[Path]:
    """New folders `names` in `parent`, each of mode 700 whatever the umask: the trust check (events/trust.py) refuses
    a folder that a group others share can write, and umask 002 with such a group would make a bare mkdir() one."""
    made = [parent / name for name in names]
    for folder in made:
        folder.mkdir()
        folder.chmod(0o700)
    return made


def write_consumers(
    tmp_path: Path, consumers: Sequence[dict[str, Any]], tag_labels: dict[str, str] | None = None
) -> Path:
    path = tmp_path / "consumers.json"
    path.write_text(json.dumps({"consumers": list(consumers), "tag_labels": tag_labels or {}}))
    path.chmod(0o600)
    return path


def make_service(
    runtime_factory: Callable[..., Any],
    tmp_path: Path,
    *,
    consumers: Sequence[dict[str, Any]] = (),
    tag_labels: dict[str, str] | None = None,
    clock: Callable[[], dt.datetime] | None = None,
    **env: str | None,
):
    """(service, runtime): an EventService on a runtime wired to FakeKite, with its store in tmp_path. The runtime is
    built with KITE_MCP_EVENTS off, so nothing else creates a service on the same store."""
    from kite_connect_mcp.events.service import EventService

    env.setdefault("KITE_MCP_EVENTS_DB", str(tmp_path / "events.sqlite3"))
    if consumers or tag_labels:
        env["KITE_MCP_EVENTS_CONSUMERS_FILE"] = str(write_consumers(tmp_path, consumers, tag_labels))
    rt = runtime_factory(**env)
    service = EventService(rt.settings, rt.client, rt.tokens, clock=clock or rt.clock)
    return service, rt
