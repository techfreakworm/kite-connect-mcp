"""The event store: SQLite in WAL mode, mode 600; events stored once; order state; doorbells; counters; retention."""

from __future__ import annotations

import datetime as dt
import sqlite3
import stat

import pytest

from conftest import FIXED_NOW
from events_helpers import ORDER_ID, filled_row, order_row
from kite_connect_mcp.events.model import OrderView, order_event, session_event
from kite_connect_mcp.events.store import EventStore, StoreError


@pytest.fixture
def store(tmp_path):
    s = EventStore(tmp_path / "state" / "events.sqlite3")
    yield s
    s.close()


def add(store, row, change="placed", *, consumers=(), now=FIXED_NOW):
    view = OrderView.from_row(row)
    body = order_event(view, change=change, source="poll", received_at=now, tag_labels={})
    with store.write() as con:
        seq = store.insert_event(
            con, body, order_id=view.order_id, consumers=consumers, order_key=view.order_id, now=now
        )
        if seq is not None:
            store.save_state(con, view, now)
    return seq


def test_the_file_is_private_and_in_wal_mode(store, tmp_path):
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.path.parent.stat().st_mode) == 0o700
    con = sqlite3.connect(store.path)
    assert con.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    con.close()


def test_an_event_is_stored_once_and_read_back_with_its_seq(store):
    seq = add(store, order_row())
    assert seq == 1
    assert add(store, order_row(status="UPDATE")) is None  # the same state: the same event_id
    body = store.event(1)
    assert body["seq"] == 1 and body["order"]["order_id"] == ORDER_ID
    assert store.event(99) is None
    assert store.last_seq() == 1


def test_the_order_state_is_kept(store):
    assert store.previous(ORDER_ID) is None
    add(store, order_row())
    prev = store.previous(ORDER_ID)
    assert prev is not None and prev.final is False and str(prev.filled) == "0"
    add(store, filled_row(), "filled")
    prev = store.previous(ORDER_ID)
    assert prev.final is True and prev.state[0] == "COMPLETE"


def test_events_listing(store):
    for i in range(5):
        add(store, order_row(order_id=f"25010100000000{i}"))
    assert [e["seq"] for e in store.events(limit=3)] == [3, 4, 5]  # the newest three, oldest first
    assert [e["seq"] for e in store.events(after_seq=2, limit=2)] == [3, 4]  # paging forward
    assert [e["seq"] for e in store.events(order_id="250101000000001")] == [2]


def test_doorbells(store):
    store.ring(ORDER_ID, FIXED_NOW)
    store.ring(ORDER_ID, FIXED_NOW)  # ringing twice keeps one
    store.ring("250101000000002", FIXED_NOW + dt.timedelta(seconds=1))
    assert store.doorbells() == [(ORDER_ID, 0), ("250101000000002", 0)]
    store.doorbell_tried(ORDER_ID)
    assert store.doorbells()[0] == (ORDER_ID, 1)
    store.doorbell_done(ORDER_ID)
    assert store.doorbells() == [("250101000000002", 0)]


def test_counters(store):
    store.bump("postback_refused", FIXED_NOW, "bad checksum")
    store.bump("postback_refused", FIXED_NOW + dt.timedelta(seconds=5))
    out = store.counters()["postback_refused"]
    assert out["count"] == 2
    assert out["note"] == "bad checksum"  # kept until a new note comes
    assert out["last_at"].startswith("2026-11-04T10:00:05")


def test_meta(store):
    assert store.meta("baseline_done") is None
    store.set_meta("baseline_done", "x")
    store.set_meta("baseline_done", "y")
    assert store.meta("baseline_done") == "y"
    assert store.meta("schema_version") == "1"


def test_a_session_event_goes_through_the_same_door(store):
    body = session_event("token_rejected", seed="s", received_at=FIXED_NOW)
    with store.write() as con:
        seq = store.insert_event(
            con, body, order_id=None, consumers=("notify",), order_key=body["event_id"], now=FIXED_NOW
        )
    assert store.event(seq)["kind"] == "session"


def test_prune(store):
    old = FIXED_NOW - dt.timedelta(days=40)
    add(store, order_row(order_id="250101000000009"), consumers=("notify",), now=old)
    add(store, order_row(), consumers=("notify",))
    store.ring("250101000000008", old)
    removed = store.prune(FIXED_NOW - dt.timedelta(days=30), doorbells_before=FIXED_NOW - dt.timedelta(days=2))
    assert removed == 1
    assert [e["order"]["order_id"] for e in store.events()] == [ORDER_ID]
    assert store.previous("250101000000009") is None
    assert store.doorbells() == []


def test_a_read_only_store(store, tmp_path):
    add(store, order_row())
    reader = EventStore(store.path, readonly=True)
    assert reader.event(1)["order"]["order_id"] == ORDER_ID
    with pytest.raises(StoreError, match="read-only"):
        reader.ring(ORDER_ID, FIXED_NOW)
    reader.close()
    with pytest.raises(StoreError, match="no event store"):
        EventStore(tmp_path / "missing.sqlite3", readonly=True)


def test_a_write_that_fails_is_a_store_error(store):
    store.close()
    with pytest.raises(StoreError):
        store.ring(ORDER_ID, FIXED_NOW)


# --- beyond the plan's tests ----------------------------------------------------------------------------------------


def _deliveries(store):
    con = sqlite3.connect(store.path)
    try:
        return con.execute("SELECT consumer, seq, order_key, status FROM deliveries ORDER BY seq, consumer").fetchall()
    finally:
        con.close()


def test_the_files_beside_the_store_are_private_too(store):
    add(store, order_row())  # the WAL and its index exist while the store is open
    names = sorted(p.name for p in store.path.parent.iterdir())
    assert names == ["events.sqlite3", "events.sqlite3-shm", "events.sqlite3-wal"]
    for path in store.path.parent.iterdir():
        assert stat.S_IMODE(path.stat().st_mode) == 0o600, path.name


def test_an_event_brings_one_pending_delivery_per_consumer(store):
    add(store, order_row(), consumers=("notify", "log"))
    assert add(store, order_row(status="UPDATE"), consumers=("notify", "log")) is None  # stored once, delivered once
    assert _deliveries(store) == [("log", 1, ORDER_ID, "pending"), ("notify", 1, ORDER_ID, "pending")]


def test_a_write_that_raises_leaves_nothing(store):
    view = OrderView.from_row(order_row())
    body = order_event(view, change="placed", source="poll", received_at=FIXED_NOW, tag_labels={})

    def crash():
        with store.write() as con:
            store.insert_event(
                con, body, order_id=view.order_id, consumers=("notify",), order_key=view.order_id, now=FIXED_NOW
            )
            store.save_state(con, view, FIXED_NOW)
            raise RuntimeError("crash")

    with pytest.raises(RuntimeError, match="crash"):
        crash()
    assert store.last_seq() == 0
    assert store.previous(ORDER_ID) is None
    assert _deliveries(store) == []
    assert add(store, order_row()) == 1  # the store still works


def test_a_read_without_an_exchange_time_keeps_the_stored_one(store):
    add(store, order_row())
    add(store, order_row(price=1230, exchange_update_timestamp=None), "modified")
    prev = store.previous(ORDER_ID)
    assert prev.state[3] == "1230"
    assert prev.exchange_update_timestamp == "2026-11-04 09:30:01"


def test_prune_takes_the_deliveries_of_the_events_it_deletes(store):
    old = FIXED_NOW - dt.timedelta(days=40)
    add(store, order_row(order_id="250101000000009"), consumers=("notify",), now=old)
    add(store, order_row(), consumers=("notify",))
    store.prune(FIXED_NOW - dt.timedelta(days=30), doorbells_before=FIXED_NOW - dt.timedelta(days=2))
    assert _deliveries(store) == [("notify", 2, ORDER_ID, "pending")]


def test_a_read_only_store_opens_when_the_writer_is_closed(store):
    add(store, order_row())
    store.close()  # SQLite checkpoints and removes the WAL and its index
    reader = EventStore(store.path, readonly=True)
    try:
        assert reader.last_seq() == 1
    finally:
        reader.close()
