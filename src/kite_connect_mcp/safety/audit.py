"""The audit log: one JSON line per event of every write tool call, dry run or live, in a file per IST day.

    <state_dir>/audit/audit-YYYY-MM-DD.jsonl      folder 700, files 600, append-only

Events: dry_run, refused, confirmation_issued, live_intent (written and fsynced BEFORE the request is sent),
live_result, live_error, live_unknown. live_result and live_unknown carry `counts`, `order_count` and `value_inr`:
what they add to today's usage (an unknown outcome is counted as if it happened, so the caps err on the safe side),
and `notional_inr`, the full value of the legs (e.g. a conversion's notional, which adds nothing to the caps).

Each event carries the call id, the tool, the exact request (method, path, payload), the checks and, for live calls,
Kite's answer. No credential is ever written (the payload holds order parameters only; the Authorization header is
added later by the HTTP client and never seen here).

Today's usage for the daily caps is computed from the log itself, so a restart never resets the caps.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from ..timeutil import ist_date, now_ist

COUNTED = ("live_result", "live_unknown")  # an unknown outcome may have happened: it counts toward the caps


@dataclass
class Usage:
    orders: int = 0
    value: Decimal = Decimal(0)

    def as_dict(self) -> dict[str, Any]:
        return {"live_orders": self.orders, "live_value_inr": str(self.value)}


class AuditLog:
    def __init__(self, folder: Path, *, clock: Callable[[], dt.datetime] = now_ist):
        self.folder = Path(folder)
        self._clock = clock
        self._lock = threading.Lock()

    def path_for(self, day: dt.date) -> Path:
        return self.folder / f"audit-{day.isoformat()}.jsonl"

    def write(self, event: str, *, durable: bool = False, **fields: Any) -> dict[str, Any]:
        """Append one event. `durable` fsyncs it (used for live_intent: it must be on disk before the request goes)."""
        now = self._clock()
        record = {"ts": now.isoformat(timespec="milliseconds"), "event": event, **fields}
        line = json.dumps(record, default=_default, separators=(",", ":"), ensure_ascii=False) + "\n"
        with self._lock:
            self.folder.mkdir(parents=True, exist_ok=True, mode=0o700)
            path = self.path_for(ist_date(now))
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(fd, line.encode("utf-8"))
                if durable:
                    os.fsync(fd)
            finally:
                os.close(fd)
        return record

    def read(self, day: dt.date | None = None, *, limit: int = 50, tool: str | None = None) -> list[dict[str, Any]]:
        """The newest `limit` events of `day` (default today), oldest first."""
        events = [e for e in self._iter(day or ist_date(self._clock())) if tool is None or e.get("tool") == tool]
        return events[-limit:] if limit > 0 else events

    def usage(self, day: dt.date | None = None) -> Usage:
        """Live writes that count toward the daily caps (successful live_result events marked `counts`)."""
        usage = Usage()
        for event in self._iter(day or ist_date(self._clock())):
            if event.get("event") not in COUNTED or not event.get("counts"):
                continue
            usage.orders += int(event.get("order_count", 1) or 0)
            try:
                usage.value += Decimal(str(event.get("value_inr") or 0))
            except ArithmeticError:
                continue
        return usage

    def modifications(self, order_id: str, day: dt.date | None = None) -> int:
        """How many live modifications of `order_id` succeeded today (Kite allows 25 per order)."""
        return sum(
            1
            for e in self._iter(day or ist_date(self._clock()))
            if e.get("event") in COUNTED and e.get("tool") == "modify_order" and e.get("order_id") == order_id
        )

    def _iter(self, day: dt.date) -> Iterator[dict[str, Any]]:
        path = self.path_for(day)
        try:
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        value = json.loads(line)
                    except ValueError:
                        continue  # a torn last line after a crash: skip it, keep the rest
                    if isinstance(value, dict):
                        yield value
        except FileNotFoundError:
            return


def _default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dt.datetime | dt.date):
        return value.isoformat()
    if isinstance(value, set | frozenset | tuple):
        return list(value)
    return str(value)
