"""The event store: one SQLite file in WAL mode, mode 600, in the service's state folder (KITE_MCP_EVENTS_DB).

Tables (SCHEMA): events, order_state, doorbells, deliveries, counters, paused, meta. The event service is the only
writer of events and order state. `kite-connect-mcp events pause|resume|retry` change delivery state from another
process, and other MCP processes open the file read-only for the `events` tools.

An event, its order's new state and a delivery row for every matching consumer are written in one transaction
(`write()`), so a crash leaves either all of them or none. Times are ISO 8601 in IST with milliseconds (`iso()`), so
they compare correctly as text.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import sqlite3
import threading
from collections.abc import Iterator, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

from ..errors import KiteMCPError
from ..timeutil import to_ist
from .model import OrderView, Previous

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    source TEXT NOT NULL,
    received_at TEXT NOT NULL,
    order_id TEXT,
    body TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_by_order ON events (order_id, seq);
CREATE INDEX IF NOT EXISTS events_by_time ON events (received_at);
CREATE TABLE IF NOT EXISTS order_state (
    order_id TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    final INTEGER NOT NULL,
    filled TEXT NOT NULL,
    exchange_update_timestamp TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS doorbells (
    order_id TEXT PRIMARY KEY,
    first_rung_at TEXT NOT NULL,
    tries INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS deliveries (
    consumer TEXT NOT NULL,
    seq INTEGER NOT NULL,
    order_key TEXT NOT NULL,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    first_try_at TEXT,
    next_try_at TEXT NOT NULL,
    delayed_sent INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (consumer, seq)
);
CREATE INDEX IF NOT EXISTS deliveries_open ON deliveries (consumer, status, order_key, seq);
CREATE TABLE IF NOT EXISTS counters (name TEXT PRIMARY KEY, value INTEGER NOT NULL, last_at TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS paused (consumer TEXT PRIMARY KEY, since TEXT NOT NULL);
"""


class StoreError(KiteMCPError):
    """The event store cannot be opened, read or written."""


def iso(moment: dt.datetime) -> str:
    return to_ist(moment).isoformat(timespec="milliseconds")


def parse_iso(text: str) -> dt.datetime:
    return dt.datetime.fromisoformat(text)


def _body(row: sqlite3.Row) -> dict[str, Any]:
    body: dict[str, Any] = json.loads(row["body"])
    body["seq"] = row["seq"]
    return body


class EventStore:
    def __init__(self, path: Path, *, readonly: bool = False, timeout_s: float = 5.0):
        self.path = Path(path)
        self.readonly = readonly
        self._lock = threading.RLock()
        try:
            if readonly:
                if not self.path.exists():
                    raise StoreError(f"no event store at {self.path} (has the event service run?)")
                self._con = sqlite3.connect(
                    f"{self.path.resolve().as_uri()}?mode=ro",
                    uri=True,
                    timeout=timeout_s,
                    isolation_level=None,
                    check_same_thread=False,
                )
            else:
                self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                os.close(os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600))
                os.chmod(self.path, 0o600)
                self._con = sqlite3.connect(self.path, timeout=timeout_s, isolation_level=None, check_same_thread=False)
                self._con.execute("PRAGMA journal_mode=WAL")
                self._con.execute("PRAGMA synchronous=FULL")
                self._con.executescript(SCHEMA)
                self._con.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', '1')")
            self._con.row_factory = sqlite3.Row
            self._con.execute(f"PRAGMA busy_timeout={int(timeout_s * 1000)}")
        except sqlite3.Error as exc:
            raise StoreError(f"cannot open the event store {self.path}: {exc}") from None

    def close(self) -> None:
        with self._lock, contextlib.suppress(sqlite3.Error):
            self._con.close()

    # --- plumbing -------------------------------------------------------------------------------------------------

    @contextlib.contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """One write transaction (BEGIN IMMEDIATE ... COMMIT). Any sqlite error becomes StoreError and rolls back."""
        if self.readonly:
            raise StoreError("the event store is open read-only")
        with self._lock:
            try:
                self._con.execute("BEGIN IMMEDIATE")
            except sqlite3.Error as exc:
                raise StoreError(f"cannot write the event store: {exc}") from None
            try:
                yield self._con
                self._con.execute("COMMIT")
            except sqlite3.Error as exc:
                with contextlib.suppress(sqlite3.Error):
                    self._con.execute("ROLLBACK")
                raise StoreError(f"cannot write the event store: {exc}") from None
            except BaseException:
                with contextlib.suppress(sqlite3.Error):
                    self._con.execute("ROLLBACK")
                raise

    def _read(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            try:
                return self._con.execute(sql, params).fetchall()
            except sqlite3.Error as exc:
                raise StoreError(f"cannot read the event store: {exc}") from None

    # --- meta -----------------------------------------------------------------------------------------------------

    def meta(self, key: str) -> str | None:
        rows = self._read("SELECT value FROM meta WHERE key = ?", (key,))
        return rows[0]["value"] if rows else None

    def set_meta(self, key: str, value: str) -> None:
        with self.write() as con:
            con.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    # --- orders and events ----------------------------------------------------------------------------------------

    def previous(self, order_id: str, con: sqlite3.Connection | None = None) -> Previous | None:
        sql = "SELECT state, final, filled, exchange_update_timestamp FROM order_state WHERE order_id = ?"
        if con is not None:
            row = con.execute(sql, (order_id,)).fetchone()
        else:
            rows = self._read(sql, (order_id,))
            row = rows[0] if rows else None
        if row is None:
            return None
        return Previous(
            tuple(json.loads(row["state"])),
            bool(row["final"]),
            Decimal(row["filled"]),
            row["exchange_update_timestamp"],
            "",
        )

    def save_state(self, con: sqlite3.Connection, view: OrderView, now: dt.datetime) -> None:
        con.execute(
            "INSERT INTO order_state (order_id, state, final, filled, exchange_update_timestamp, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(order_id) DO UPDATE SET state = excluded.state, "
            "final = excluded.final, filled = excluded.filled, exchange_update_timestamp = "
            "coalesce(excluded.exchange_update_timestamp, order_state.exchange_update_timestamp), "
            "updated_at = excluded.updated_at",
            (
                view.order_id,
                json.dumps(view.state()),
                int(view.final),
                str(view.filled),
                view.exchange_update_timestamp,
                iso(now),
            ),
        )

    def insert_event(
        self,
        con: sqlite3.Connection,
        body: dict[str, Any],
        *,
        order_id: str | None,
        consumers: Sequence[str],
        order_key: str,
        now: dt.datetime,
    ) -> int | None:
        """Store `body` and one pending delivery per consumer. None when an event with its event_id is stored
        already."""
        cursor = con.execute(
            "INSERT OR IGNORE INTO events (event_id, kind, source, received_at, order_id, body) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                body["event_id"],
                body["kind"],
                body["source"],
                iso(now),
                order_id,
                json.dumps(body, ensure_ascii=False, separators=(",", ":")),
            ),
        )
        if cursor.rowcount == 0 or cursor.lastrowid is None:
            return None
        seq = int(cursor.lastrowid)
        stamp = iso(now)
        con.executemany(
            "INSERT INTO deliveries (consumer, seq, order_key, status, next_try_at, updated_at) "
            "VALUES (?, ?, ?, 'pending', ?, ?)",
            [(name, seq, order_key, stamp, stamp) for name in consumers],
        )
        return seq

    def event(self, seq: int) -> dict[str, Any] | None:
        rows = self._read("SELECT seq, body FROM events WHERE seq = ?", (seq,))
        return _body(rows[0]) if rows else None

    def events(self, *, order_id: str | None = None, after_seq: int = 0, limit: int = 50) -> list[dict[str, Any]]:
        """Oldest first. With `after_seq`, the first `limit` events after it (paging forward); without, the newest
        `limit`. `order_id` keeps one order's events."""
        where = "seq > ?" + (" AND order_id = ?" if order_id else "")
        params: list[Any] = [after_seq] + ([order_id] if order_id else [])
        if after_seq > 0:
            rows = self._read(f"SELECT seq, body FROM events WHERE {where} ORDER BY seq LIMIT ?", (*params, limit))  # noqa: S608
        else:
            rows = self._read(f"SELECT seq, body FROM events WHERE {where} ORDER BY seq DESC LIMIT ?", (*params, limit))  # noqa: S608
            rows.reverse()
        return [_body(r) for r in rows]

    def last_seq(self) -> int:
        return int(self._read("SELECT coalesce(max(seq), 0) AS n FROM events")[0]["n"])

    # --- doorbells ------------------------------------------------------------------------------------------------

    def ring(self, order_id: str, now: dt.datetime) -> None:
        with self.write() as con:
            con.execute(
                "INSERT OR IGNORE INTO doorbells (order_id, first_rung_at, tries) VALUES (?, ?, 0)",
                (order_id, iso(now)),
            )

    def doorbells(self) -> list[tuple[str, int]]:
        """(order_id, tries), oldest first."""
        rows = self._read("SELECT order_id, tries FROM doorbells ORDER BY first_rung_at, order_id")
        return [(r["order_id"], int(r["tries"])) for r in rows]

    def doorbell_tried(self, order_id: str) -> None:
        with self.write() as con:
            con.execute("UPDATE doorbells SET tries = tries + 1 WHERE order_id = ?", (order_id,))

    def doorbell_done(self, order_id: str) -> None:
        with self.write() as con:
            con.execute("DELETE FROM doorbells WHERE order_id = ?", (order_id,))

    # --- counters -------------------------------------------------------------------------------------------------

    def bump(self, name: str, now: dt.datetime, note: str | None = None) -> None:
        with self.write() as con:
            con.execute(
                "INSERT INTO counters (name, value, last_at, note) VALUES (?, 1, ?, ?) ON CONFLICT(name) DO UPDATE SET "
                "value = counters.value + 1, last_at = excluded.last_at, note = coalesce(excluded.note, counters.note)",
                (name, iso(now), note),
            )

    def counters(self) -> dict[str, dict[str, Any]]:
        rows = self._read("SELECT name, value, last_at, note FROM counters ORDER BY name")
        return {r["name"]: {"count": int(r["value"]), "last_at": r["last_at"], "note": r["note"]} for r in rows}

    # --- retention ------------------------------------------------------------------------------------------------

    def prune(self, before: dt.datetime, *, doorbells_before: dt.datetime) -> int:
        """Delete the events received before `before` with their deliveries, the order states not updated since, and
        doorbells rung before `doorbells_before` (an order book lives for one day). Returns the events deleted."""
        cut = iso(before)
        with self.write() as con:
            con.execute("DELETE FROM deliveries WHERE seq IN (SELECT seq FROM events WHERE received_at < ?)", (cut,))
            removed = con.execute("DELETE FROM events WHERE received_at < ?", (cut,)).rowcount
            con.execute("DELETE FROM order_state WHERE updated_at < ?", (cut,))
            con.execute("DELETE FROM doorbells WHERE first_rung_at < ?", (iso(doorbells_before),))
        return int(removed)
