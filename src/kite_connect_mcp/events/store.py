"""The event store: one SQLite file in WAL mode, mode 600, in the service's state folder (KITE_MCP_EVENTS_DB).

Tables (SCHEMA): events, order_state, doorbells, deliveries, counters, paused, meta. The event service is the only
writer of events and order state. `kite-connect-mcp events pause|resume|retry` change delivery state from another
process, and other MCP processes open the file read-only for the `events` tools.

An event, its order's new state and a delivery row for every matching consumer are written in one transaction
(`write()`), so a crash leaves either all of them or none. Times are ISO 8601 in IST with milliseconds (`iso()`), so
they compare correctly as text.

A delivery stays pending until it is done, failed or skipped. The router's updates (retry_delivery, finish_delivery,
mark_alerted) change only a pending delivery and say whether they did: a delivery decided elsewhere while its command
ran (skipped by `events resume --skip-backlog`, removed by prune) is left as it is. A failure and its delivery_failed
event, and an alert's mark and its delivery_delayed event, are written in one transaction by the event service.

The file records its SCHEMA_VERSION in meta, written in the same transaction as its tables; a file of another
version, or a database with tables but no version (not an event store), is refused when it is opened, not misread or
taken over.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import sqlite3
import threading
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, get_args

from ..errors import KiteMCPError
from ..timeutil import to_ist
from .model import OrderView, Previous

SCHEMA_VERSION = 1
MAX_EVENTS_LIMIT = 1000  # the most events() returns at once
# The tables, one statement per `;`: _open splits SCHEMA at each `;` to create them in one transaction, so no
# statement may hold a `;` of its own (a trigger's BEGIN ... END, a default value or a comment with one would be cut).
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
    event_id TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS doorbells (
    order_id TEXT PRIMARY KEY,
    first_rung_at TEXT NOT NULL,
    rings INTEGER NOT NULL DEFAULT 1,
    tries INTEGER NOT NULL DEFAULT 0,
    tried_at TEXT
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


DoorbellDrop = Literal["doorbell_unknown_order", "doorbell_wrong_order", "doorbell_gave_up"]  # why, and its counter
FinalStatus = Literal["done", "failed", "skipped"]  # a delivery's status once it is no longer pending
FINAL_STATUSES: tuple[FinalStatus, ...] = get_args(FinalStatus)


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


def _bump(con: sqlite3.Connection, name: str, now: dt.datetime, note: str | None = None, by: int = 1) -> None:
    """Add `by` (one or more) to counter `name` in the write that `con` belongs to. A new `note` replaces the last;
    None keeps it."""
    if by < 1:
        raise ValueError("a counter moves by one or more")
    con.execute(
        "INSERT INTO counters (name, value, last_at, note) VALUES (?, ?, ?, ?) ON CONFLICT(name) DO UPDATE SET "
        "value = counters.value + excluded.value, last_at = excluded.last_at, "
        "note = coalesce(excluded.note, counters.note)",
        (name, by, iso(now), note),
    )


def _schema_version(con: sqlite3.Connection) -> str | None:
    """The version a file records, None when it has no meta table (a new file, or not an event store) or no version
    in it."""
    if con.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'meta'").fetchone() is None:
        return None
    row = con.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    return None if row is None else str(row[0])


def _is_empty(con: sqlite3.Connection) -> bool:
    """True for a database with nothing in it: no table, index, view or trigger (a new file)."""
    return con.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone() is None


@dataclass(frozen=True)
class Doorbell:
    """A rung order waiting to be read from Kite. `rings` counts its rings since it was kept: the worker reads it before
    it asks Kite, and settles or drops the doorbell only while it is unchanged (doorbell_done, drop_doorbell), so a
    ring that lands during the read is not lost. Rings are counted, not timed: two rings in one millisecond, or on a
    clock that stepped back, still tell apart. `tries` counts the failed reads counted so far, `tried_at` is when the
    last of them was counted (None before any)."""

    order_id: str
    tries: int
    rings: int
    tried_at: dt.datetime | None


@dataclass(frozen=True)
class Delivery:
    """One consumer's pending delivery of one event (`seq`). `order_key` groups the deliveries that go one at a time
    and in order. `attempts` counts the failed tries since the delivery was queued (retry_failed starts it
    again at 0), `first_try_at` is when the first of them was made (None before any), `next_try_at` when the delivery
    is due, `last_error` the last error a try gave. `delayed_sent` says whether the delivery_delayed alert for it has
    been recorded (mark_alerted), so that alert is sent once."""

    consumer: str
    seq: int
    order_key: str
    attempts: int
    first_try_at: dt.datetime | None
    next_try_at: dt.datetime
    delayed_sent: bool
    last_error: str | None


def _delivery(row: sqlite3.Row) -> Delivery:
    return Delivery(
        consumer=row["consumer"],
        seq=int(row["seq"]),
        order_key=row["order_key"],
        attempts=int(row["attempts"]),
        first_try_at=parse_iso(row["first_try_at"]) if row["first_try_at"] else None,
        next_try_at=parse_iso(row["next_try_at"]),
        delayed_sent=bool(row["delayed_sent"]),
        last_error=row["last_error"],
    )


_HEADS = """
SELECT d.* FROM deliveries d
WHERE d.consumer = ? AND d.status = 'pending'
  AND NOT EXISTS (
    SELECT 1 FROM deliveries e
    WHERE e.consumer = d.consumer AND e.order_key = d.order_key AND e.status = 'pending' AND e.seq < d.seq
  )
ORDER BY d.seq
"""


class EventStore:
    """One event store file. The event service, and the `events` commands that change delivery state, open it
    read-write; other processes open it read-only. One connection is shared by the threads of a process under a lock,
    every write goes through `write()`, and any failure to open, read or write it is a StoreError.

    Only the event service makes a store (`create`, the default): a read-only open, or a read-write one with
    create=False (the `events` commands), takes only a store that is there and set up, and says so when it is not.

    `timeout_s` is SQLite's busy timeout: how long a write waits for another process's write lock before it fails.
    """

    def __init__(self, path: Path, *, readonly: bool = False, create: bool = True, timeout_s: float = 5.0):
        self.path = Path(path)
        self.readonly = readonly
        self.create = create and not readonly
        self._lock = threading.RLock()
        try:
            self._con = self._open(timeout_s)
        except (sqlite3.Error, OSError) as exc:
            raise StoreError(f"cannot open the event store {self.path}: {exc}") from None

    def _open(self, timeout_s: float) -> sqlite3.Connection:
        if not self.create:
            try:  # not Path.exists(): a folder it cannot enter is an OSError (cannot open), not a missing store
                self.path.stat()
            except (FileNotFoundError, NotADirectoryError):
                raise StoreError(f"no event store at {self.path} (has the event service run?)") from None
            con = sqlite3.connect(
                # mode=rw, not a plain path: a file removed since the check above is not made anew
                f"{self.path.resolve().as_uri()}?mode={'ro' if self.readonly else 'rw'}",
                uri=True,
                timeout=timeout_s,  # also sets SQLite's busy timeout
                isolation_level=None,
                check_same_thread=False,
            )
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.close(os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600))
            os.chmod(self.path, 0o600)
            con = sqlite3.connect(self.path, timeout=timeout_s, isolation_level=None, check_same_thread=False)
        try:
            con.row_factory = sqlite3.Row
            self._check_version(con)  # before any write
            if not self.readonly:
                con.execute("PRAGMA journal_mode=WAL")
                con.execute("PRAGMA synchronous=FULL")
                # the tables and the version in one transaction: a crash between them would leave a file it refuses
                con.execute("BEGIN IMMEDIATE")
                for statement in SCHEMA.split(";"):  # not executescript(), which commits first
                    if statement.strip():
                        con.execute(statement)
                con.execute(
                    "INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),)
                )
                con.execute("COMMIT")
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                con.close()
            raise
        return con

    def _check_version(self, con: sqlite3.Connection) -> None:
        """Refuse a file of another schema version, a database that is not an event store (tables, no version), and,
        unless it may create the store, a file not set up yet (the event service sets up an empty one)."""
        version = _schema_version(con)
        if version == str(SCHEMA_VERSION):
            return
        if version is not None:
            raise StoreError(
                f"the event store {self.path} has schema version {version}; "
                f"this version of kite-connect-mcp needs {SCHEMA_VERSION}"
            )
        if not _is_empty(con):
            raise StoreError(f"the file {self.path} is not an event store: it has tables but no schema version")
        if not self.create:
            raise StoreError(f"the event store {self.path} is not set up yet (has the event service run?)")

    def close(self) -> None:
        with self._lock, contextlib.suppress(sqlite3.Error):
            self._con.close()

    # --- plumbing -------------------------------------------------------------------------------------------------

    @contextlib.contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """One write transaction (BEGIN IMMEDIATE ... COMMIT) on the connection it yields: what the block does is
        committed together, or rolled back together when it raises. Any SQLite error becomes StoreError.

        Keep the block synchronous (no await inside it), and inside it call only the methods that take `con`, and pass
        it (bump(..., con=con) is fine): a method that writes on its own (ring, set_meta, bump without con, ...) would
        open a second transaction, so it raises StoreError ("nested write") and the whole block rolls back."""
        if self.readonly:
            raise StoreError("the event store is open read-only")
        with self._lock:
            try:
                if self._con.in_transaction:  # the lock is re-entrant: this thread is inside a write() already
                    raise StoreError("nested write: inside write(), call only the methods that take con")
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
        sql = "SELECT state, final, filled, exchange_update_timestamp, event_id FROM order_state WHERE order_id = ?"
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
            row["event_id"],
        )

    def save_state(self, con: sqlite3.Connection, view: OrderView, now: dt.datetime, *, event_id: str) -> None:
        """Record the order's new state and the event that reported it (`event_id`, "" for a state recorded without an
        event, as the baseline is). The next event of the order chains to that id, so it is required, never left out;
        "" keeps the id of the order's last event, when it has one."""
        con.execute(
            "INSERT INTO order_state (order_id, state, final, filled, exchange_update_timestamp, event_id, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(order_id) DO UPDATE SET state = excluded.state, "
            "final = excluded.final, filled = excluded.filled, exchange_update_timestamp = "
            "coalesce(excluded.exchange_update_timestamp, order_state.exchange_update_timestamp), "
            "event_id = CASE WHEN excluded.event_id = '' THEN order_state.event_id ELSE excluded.event_id END, "
            "updated_at = excluded.updated_at",
            (
                view.order_id,
                json.dumps(view.state()),
                int(view.final),
                str(view.filled),
                view.exchange_update_timestamp,
                event_id,
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
        already; a body that breaks another rule (a kind or source of None) raises, and write() makes it a StoreError
        (a body without one of them is a KeyError).

        The duplicate check is a NOT EXISTS, not OR IGNORE or ON CONFLICT DO NOTHING: those advance SQLite's
        AUTOINCREMENT counter even when they insert nothing, which would leave gaps in seq and a last_seq() that no
        event has. Inside write() no other writer can come between the check and the insert."""
        cursor = con.execute(
            "INSERT INTO events (event_id, kind, source, received_at, order_id, body) "
            "SELECT :event_id, :kind, :source, :received_at, :order_id, :body "
            "WHERE NOT EXISTS (SELECT 1 FROM events WHERE event_id = :event_id)",
            {
                "event_id": body["event_id"],
                "kind": body["kind"],
                "source": body["source"],
                "received_at": iso(now),
                "order_id": order_id,
                "body": json.dumps(body, ensure_ascii=False, separators=(",", ":")),
            },
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

    def event(self, seq: int, con: sqlite3.Connection | None = None) -> dict[str, Any] | None:
        """The event `seq` with its seq, None when it is not stored. With `con`, read inside that write()."""
        sql = "SELECT seq, body FROM events WHERE seq = ?"
        if con is not None:
            row = con.execute(sql, (seq,)).fetchone()
        else:
            rows = self._read(sql, (seq,))
            row = rows[0] if rows else None
        return _body(row) if row is not None else None

    def events(self, *, order_id: str | None = None, after_seq: int = 0, limit: int = 50) -> list[dict[str, Any]]:
        """Oldest first, at most `limit` of them (capped at MAX_EVENTS_LIMIT; none for a limit below 1). With
        `after_seq` above 0, the first events after that seq (paging forward); with after_seq=0, the newest. `order_id`
        keeps one order's events."""
        if limit < 1:
            return []
        limit = min(limit, MAX_EVENTS_LIMIT)
        where = "seq > ?" + (" AND order_id = ?" if order_id else "")
        params: list[Any] = [after_seq] + ([order_id] if order_id else [])
        if after_seq > 0:
            rows = self._read(f"SELECT seq, body FROM events WHERE {where} ORDER BY seq LIMIT ?", (*params, limit))  # noqa: S608
        else:
            rows = self._read(f"SELECT seq, body FROM events WHERE {where} ORDER BY seq DESC LIMIT ?", (*params, limit))  # noqa: S608
            rows.reverse()
        return [_body(r) for r in rows]

    def last_seq(self) -> int:
        """The seq of the last event ever stored, 0 before the first. It never goes back, even when a prune empties
        the events table: SQLite's AUTOINCREMENT counter is read, not the highest seq left."""
        rows = self._read("SELECT seq FROM sqlite_sequence WHERE name = 'events'")
        return int(rows[0]["seq"]) if rows else 0

    # --- doorbells ------------------------------------------------------------------------------------------------

    def ring(self, order_id: str, now: dt.datetime) -> None:
        """Keep a doorbell for `order_id` (one per order, however often it rings; each ring adds one to its `rings`)
        and count the ring in `postback_accepted`, in one transaction: on a StoreError neither is kept."""
        with self.write() as con:
            con.execute(
                "INSERT INTO doorbells (order_id, first_rung_at) VALUES (?, ?) "
                "ON CONFLICT(order_id) DO UPDATE SET rings = doorbells.rings + 1",
                (order_id, iso(now)),
            )
            _bump(con, "postback_accepted", now)

    def doorbells(self) -> list[tuple[str, int]]:
        """(order_id, tries), oldest first."""
        rows = self._read("SELECT order_id, tries FROM doorbells ORDER BY first_rung_at, order_id")
        return [(r["order_id"], int(r["tries"])) for r in rows]

    def doorbell(self, order_id: str) -> Doorbell | None:
        """The doorbell of `order_id` as it is now, None when there is none."""
        rows = self._read("SELECT order_id, tries, rings, tried_at FROM doorbells WHERE order_id = ?", (order_id,))
        if not rows:
            return None
        row = rows[0]
        tried_at = parse_iso(row["tried_at"]) if row["tried_at"] else None
        return Doorbell(row["order_id"], tries=int(row["tries"]), rings=int(row["rings"]), tried_at=tried_at)

    def doorbell_tried(self, order_id: str, now: dt.datetime) -> None:
        """Count a failed read of the doorbell of `order_id`, made `now`."""
        with self.write() as con:
            con.execute("UPDATE doorbells SET tries = tries + 1, tried_at = ? WHERE order_id = ?", (iso(now), order_id))

    def doorbell_done(self, order_id: str, *, rings: int) -> bool:
        """Delete the doorbell of `order_id` if it has rung `rings` times, as when its read began (see Doorbell).
        False, with the doorbell kept, when it rang again since (or is gone)."""
        with self.write() as con:
            sql = "DELETE FROM doorbells WHERE order_id = ? AND rings = ?"
            return con.execute(sql, (order_id, rings)).rowcount == 1

    def drop_doorbell(self, order_id: str, now: dt.datetime, counter: DoorbellDrop, *, rings: int) -> bool:
        """doorbell_done, and count why in `counter`, in one transaction. False, with nothing counted, when the
        doorbell rang again since its read began, or is gone."""
        with self.write() as con:
            sql = "DELETE FROM doorbells WHERE order_id = ? AND rings = ?"
            if con.execute(sql, (order_id, rings)).rowcount == 0:
                return False
            _bump(con, counter, now)
            return True

    # --- counters -------------------------------------------------------------------------------------------------

    def bump(
        self,
        name: str,
        now: dt.datetime,
        note: str | None = None,
        *,
        by: int = 1,
        con: sqlite3.Connection | None = None,
    ) -> None:
        """Add `by` (one or more; ValueError otherwise) to counter `name`. A new `note` replaces the last one; None
        keeps it. With `con`, inside that write(), so the count is kept or rolled back with what the block writes;
        without, in a write of its own."""
        if con is not None:
            _bump(con, name, now, note, by)
            return
        with self.write() as own:
            _bump(own, name, now, note, by)

    def counters(self) -> dict[str, dict[str, Any]]:
        rows = self._read("SELECT name, value, last_at, note FROM counters ORDER BY name")
        return {r["name"]: {"count": int(r["value"]), "last_at": r["last_at"], "note": r["note"]} for r in rows}

    # --- deliveries -----------------------------------------------------------------------------------------------

    def next_delivery(self, consumer: str, now: dt.datetime) -> tuple[Delivery | None, dt.datetime | None]:
        """The first delivery of `consumer` that is due now, and when the next of the others becomes due. Each order's
        deliveries go one at a time and in order, so an order still retrying holds back only its own later events."""
        heads = [_delivery(r) for r in self._read(_HEADS, (consumer,))]
        due = [d for d in heads if d.next_try_at <= now]
        later = [d.next_try_at for d in heads if d.next_try_at > now]
        return (due[0] if due else None), (min(later) if later else None)

    def retry_delivery(
        self,
        consumer: str,
        seq: int,
        *,
        first_try_at: dt.datetime,
        next_try_at: dt.datetime,
        now: dt.datetime,
        error: str | None,
    ) -> bool:
        """Record a failed try of a pending delivery, to be tried again at `next_try_at`. `attempts` counts these
        calls. `first_try_at` is kept from the first call, and later values are ignored: the alert and the give-up
        times count from it. A new `error` replaces the last one; None keeps it. False, with nothing changed, when the
        delivery is no longer pending (decided elsewhere, see the module docstring)."""
        with self.write() as con:
            cursor = con.execute(
                "UPDATE deliveries SET attempts = attempts + 1, first_try_at = coalesce(first_try_at, ?), "
                "next_try_at = ?, last_error = coalesce(?, last_error), updated_at = ? "
                "WHERE consumer = ? AND seq = ? AND status = 'pending'",
                (iso(first_try_at), iso(next_try_at), error, iso(now), consumer, seq),
            )
            return cursor.rowcount == 1

    def finish_delivery(
        self,
        consumer: str,
        seq: int,
        status: FinalStatus,
        now: dt.datetime,
        error: str | None = None,
        *,
        con: sqlite3.Connection | None = None,
    ) -> bool:
        """Give a pending delivery its final status: done, failed (`events retry` can queue it again) or skipped. A
        new `error` replaces the last one; None keeps it. False, with nothing changed, when the delivery is no longer
        pending: an operator's skip stays a skip even when the outcome of the try in flight lands after it. With
        `con`, inside that write(), so it is kept or rolled back with what the block writes."""
        if status not in FINAL_STATUSES:
            raise ValueError(f"not a final delivery status: {status!r}")
        sql = (
            "UPDATE deliveries SET status = ?, last_error = coalesce(?, last_error), updated_at = ? "
            "WHERE consumer = ? AND seq = ? AND status = 'pending'"
        )
        params = (status, error, iso(now), consumer, seq)
        if con is not None:
            return con.execute(sql, params).rowcount == 1
        with self.write() as own:
            return own.execute(sql, params).rowcount == 1

    def overdue_alerts(self, consumer: str, now: dt.datetime, alert_after_s: int) -> list[Delivery]:
        """Pending deliveries of `consumer` still retrying `alert_after_s` after their first try, not yet reported."""
        cut = iso(now - dt.timedelta(seconds=alert_after_s))
        rows = self._read(
            "SELECT * FROM deliveries WHERE consumer = ? AND status = 'pending' AND attempts > 0 AND delayed_sent = 0 "
            "AND first_try_at <= ? ORDER BY seq",
            (consumer, cut),
        )
        return [_delivery(r) for r in rows]

    def next_alert_at(self, consumer: str, alert_after_s: int) -> dt.datetime | None:
        """When the next alert of `consumer` falls due: `alert_after_s` after the earliest first try of its retrying
        deliveries not yet reported, None when there are none. It can be a time in the past, when an alert is due
        already (overdue_alerts lists those)."""
        rows = self._read(
            "SELECT min(first_try_at) AS t FROM deliveries WHERE consumer = ? AND status = 'pending' AND attempts > 0 "
            "AND delayed_sent = 0",
            (consumer,),
        )
        first = rows[0]["t"] if rows else None
        return parse_iso(first) + dt.timedelta(seconds=alert_after_s) if first else None

    def mark_alerted(self, consumer: str, seq: int, *, con: sqlite3.Connection | None = None) -> bool:
        """Note that the delivery_delayed alert of a pending delivery has been recorded, so it is sent once. False,
        with nothing changed, when the delivery is no longer pending. With `con`, inside that write(): the event
        service marks it in the write that records the alert (EventService.alert_delayed)."""
        sql = "UPDATE deliveries SET delayed_sent = 1 WHERE consumer = ? AND seq = ? AND status = 'pending'"
        if con is not None:
            return con.execute(sql, (consumer, seq)).rowcount == 1
        with self.write() as own:
            return own.execute(sql, (consumer, seq)).rowcount == 1

    def retry_failed(self, consumer: str, now: dt.datetime) -> int:
        """Put `consumer`'s failed deliveries back in the queue, as new (no tries, no alert, due `now`). Returns how
        many. A re-sent event can arrive after later events of its order that were delivered already; consumers can
        tell from seq. Later events of the order still pending wait behind it, as usual."""
        with self.write() as con:
            return int(
                con.execute(
                    "UPDATE deliveries SET status = 'pending', attempts = 0, first_try_at = NULL, delayed_sent = 0, "
                    "next_try_at = ?, updated_at = ? WHERE consumer = ? AND status = 'failed'",
                    (iso(now), iso(now), consumer),
                ).rowcount
            )

    # --- pause ----------------------------------------------------------------------------------------------------

    def pause(self, consumer: str, now: dt.datetime) -> None:
        with self.write() as con:
            con.execute("INSERT OR IGNORE INTO paused (consumer, since) VALUES (?, ?)", (consumer, iso(now)))

    def resume(self, consumer: str, *, skip_backlog: bool, now: dt.datetime) -> int:
        """Deliver again; with `skip_backlog` the waiting deliveries are marked skipped. Returns how many were
        skipped."""
        with self.write() as con:
            con.execute("DELETE FROM paused WHERE consumer = ?", (consumer,))
            if not skip_backlog:
                return 0
            return int(
                con.execute(
                    "UPDATE deliveries SET status = 'skipped', last_error = 'skipped by resume --skip-backlog', "
                    "updated_at = ? WHERE consumer = ? AND status = 'pending'",
                    (iso(now), consumer),
                ).rowcount
            )

    def is_paused(self, consumer: str) -> bool:
        return bool(self._read("SELECT 1 FROM paused WHERE consumer = ?", (consumer,)))

    def paused(self) -> dict[str, str]:
        return {r["consumer"]: r["since"] for r in self._read("SELECT consumer, since FROM paused ORDER BY consumer")}

    def consumer_names(self) -> set[str]:
        """Every consumer the store knows: with a delivery, or paused (configured or not)."""
        return {r["consumer"] for r in self._read("SELECT consumer FROM deliveries UNION SELECT consumer FROM paused")}

    # --- status ---------------------------------------------------------------------------------------------------

    def summary(self, consumers: Iterable[str] = ()) -> dict[str, Any]:
        """Counts for `events status` (no payloads): deliveries per consumer, doorbells, counters, the last seq.
        `pending` counts every pending delivery, the retrying ones too; `retrying` and `delayed` are subsets of it.
        Each consumer named in `consumers` (the configured ones) is listed first, in that order, with zeros when it has
        no deliveries yet; a consumer with deliveries but no longer configured follows."""
        listed: dict[str, dict[str, Any]] = {}

        def entry(name: str) -> dict[str, Any]:
            return listed.setdefault(
                name,
                {
                    "pending": 0,
                    "retrying": 0,
                    "delayed": 0,
                    "failed": 0,
                    "done": 0,
                    "skipped": 0,
                    "last_done_at": None,
                    "last_error": None,
                },
            )

        for name in consumers:
            entry(name)
        for r in self._read(
            "SELECT consumer, status, attempts > 0 AS retrying, delayed_sent, count(*) AS n FROM deliveries "
            "GROUP BY consumer, status, retrying, delayed_sent"
        ):
            item = entry(r["consumer"])
            item[r["status"]] = item.get(r["status"], 0) + int(r["n"])
            if r["status"] == "pending" and r["retrying"]:
                item["retrying"] += int(r["n"])
                if r["delayed_sent"]:
                    item["delayed"] += int(r["n"])
        for r in self._read(
            "SELECT consumer, max(updated_at) AS t FROM deliveries WHERE status = 'done' GROUP BY consumer"
        ):
            entry(r["consumer"])["last_done_at"] = r["t"]
        for r in self._read(
            "SELECT consumer, last_error FROM deliveries d WHERE status = 'failed' AND updated_at = "
            "(SELECT max(updated_at) FROM deliveries e WHERE e.consumer = d.consumer AND e.status = 'failed') "
            "ORDER BY seq"  # of failures stamped at the same time, the last one's error wins
        ):
            entry(r["consumer"])["last_error"] = r["last_error"]
        return {
            "last_seq": self.last_seq(),
            "counters": self.counters(),
            "doorbells_waiting": len(self.doorbells()),
            "baseline_done": self.meta("baseline_done"),
            "after_close_ok": self.meta("after_close_ok"),
            "consumers": listed,
            "paused": self.paused(),
        }

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
