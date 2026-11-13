"""The event store: SQLite in WAL mode, mode 600; events stored once; order state; doorbells; counters; retention."""

from __future__ import annotations

import datetime as dt
import os
import sqlite3
import stat
import time

import pytest

from conftest import FIXED_NOW
from events_helpers import ORDER_ID, filled_row, order_row
from kite_connect_mcp.events.model import OrderView, judge, order_event, session_event
from kite_connect_mcp.events.store import MAX_EVENTS_LIMIT, EventStore, StoreError


@pytest.fixture
def store(tmp_path):
    s = EventStore(tmp_path / "state" / "events.sqlite3")
    yield s
    s.close()


def add(store, row, change="placed", *, consumers=(), now=FIXED_NOW):
    view = OrderView.from_row(row)
    body = order_event(view, change=change, source="poll", received_at=now, tag_labels={}, previous_event_id="")
    with store.write() as con:
        seq = store.insert_event(
            con, body, order_id=view.order_id, consumers=consumers, order_key=view.order_id, now=now
        )
        if seq is not None:
            store.save_state(con, view, now, event_id=body["event_id"])
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
    assert add(store, order_row(order_id="250101000000002")) == 2  # a duplicate leaves no gap in seq


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
    body = order_event(view, change="placed", source="poll", received_at=FIXED_NOW, tag_labels={}, previous_event_id="")

    def crash():
        with store.write() as con:
            store.insert_event(
                con, body, order_id=view.order_id, consumers=("notify",), order_key=view.order_id, now=FIXED_NOW
            )
            store.save_state(con, view, FIXED_NOW, event_id=body["event_id"])
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
        names = sorted(p.name for p in store.path.parent.iterdir())
        assert names == ["events.sqlite3", "events.sqlite3-shm", "events.sqlite3-wal"]  # the reader made them again
        for path in store.path.parent.iterdir():
            assert stat.S_IMODE(path.stat().st_mode) == 0o600, path.name
    finally:
        reader.close()


# --- each order's last event id, opening, schema version, and the review's edge cases ------------------------------


def ingest(store, row, *, source="poll", now=FIXED_NOW):
    """What the event service does with one read: judge it against the stored state, store the event and the state."""
    view = OrderView.from_row(row)
    with store.write() as con:
        previous = store.previous(view.order_id, con)
        change = judge(view, previous)
        if change is None:
            return None
        body = order_event(
            view,
            change=change,
            source=source,
            received_at=now,
            tag_labels={},
            previous_event_id=previous.event_id if previous else "",
        )
        seq = store.insert_event(con, body, order_id=view.order_id, consumers=(), order_key=view.order_id, now=now)
        if seq is not None:
            store.save_state(con, view, now, event_id=body["event_id"])
    return seq


def test_an_order_moved_and_moved_back_gives_an_event_each_time(store):
    reads = [
        order_row(),
        order_row(price=1230, exchange_update_timestamp="2026-11-04 09:30:30"),
        order_row(exchange_update_timestamp="2026-11-04 09:31:00"),  # back to the first price
        order_row(price=1230, exchange_update_timestamp="2026-11-04 09:31:30"),  # and to the second again
    ]
    for seq, row in enumerate(reads, start=1):
        assert ingest(store, row) == seq
        stored = store.previous(ORDER_ID)
        assert stored.event_id == store.event(seq)["event_id"]  # the state follows each event
        assert stored.state[3] == ("1234.5" if seq % 2 else "1230")
    events = store.events()
    assert [e["change"] for e in events] == ["placed", "modified", "modified", "modified"]
    assert len({e["event_id"] for e in events}) == 4


def test_a_postback_and_a_poll_of_one_change_are_stored_once(store):
    ingest(store, order_row())
    previous = store.previous(ORDER_ID)
    view = OrderView.from_row(filled_row())
    bodies = [
        order_event(
            view,
            change="filled",
            source=source,
            received_at=FIXED_NOW,
            tag_labels={},
            previous_event_id=previous.event_id,
        )
        for source in ("postback", "poll")
    ]  # both judged against the same stored state, as a postback and the poll racing would be
    with store.write() as con:
        seqs = [
            store.insert_event(con, body, order_id=ORDER_ID, consumers=("notify",), order_key=ORDER_ID, now=FIXED_NOW)
            for body in bodies
        ]
    assert seqs == [2, None]
    assert ingest(store, filled_row(), source="poll") is None  # and a later read of it reports nothing
    assert _deliveries(store) == [("notify", 2, ORDER_ID, "pending")]


def test_a_late_read_with_an_older_exchange_update_is_dropped(store):
    ingest(store, order_row())
    ingest(store, order_row(price=1230, exchange_update_timestamp="2026-11-04 09:30:30"))
    assert ingest(store, order_row()) is None  # the first read again, late: its exchange update is older
    assert store.last_seq() == 2 and store.previous(ORDER_ID).state[3] == "1230"


def test_a_state_recorded_without_an_event_has_no_event_id(store):
    view = OrderView.from_row(order_row())
    with store.write() as con:
        store.save_state(con, view, FIXED_NOW, event_id="")  # the first poll's baseline
    assert store.previous(ORDER_ID).event_id == ""
    assert ingest(store, filled_row()) == 1
    assert store.previous(ORDER_ID).event_id == store.event(1)["event_id"]


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write any folder")
def test_a_folder_it_cannot_write_is_a_store_error(tmp_path):
    locked = tmp_path / "locked"
    locked.mkdir(mode=0o500)
    try:
        with pytest.raises(StoreError, match=r"cannot open the event store .*Permission denied"):
            EventStore(locked / "state" / "events.sqlite3")
    finally:
        locked.chmod(0o700)


def test_a_folder_that_is_a_file_is_a_store_error(tmp_path):
    (tmp_path / "state").write_text("")
    with pytest.raises(StoreError, match="cannot open the event store"):
        EventStore(tmp_path / "state" / "events.sqlite3")


def test_a_file_that_is_not_a_database_is_a_store_error_and_its_connection_is_closed(tmp_path, monkeypatch):
    path = tmp_path / "events.sqlite3"
    path.write_bytes(b"not a database, " * 64)
    opened = []
    real_connect = sqlite3.connect

    def connect(*args, **kwargs):
        opened.append(real_connect(*args, **kwargs))
        return opened[-1]

    monkeypatch.setattr(sqlite3, "connect", connect)
    for readonly in (False, True):
        with pytest.raises(StoreError, match=r"cannot open the event store .*not a database"):
            EventStore(path, readonly=readonly)
    assert len(opened) == 2
    for con in opened:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            con.execute("SELECT 1")


def test_a_store_of_another_schema_version_is_refused(store):
    store.set_meta("schema_version", "2")
    store.close()
    for readonly in (False, True):
        with pytest.raises(StoreError, match="has schema version 2; this version of kite-connect-mcp needs 1"):
            EventStore(store.path, readonly=readonly)


def test_a_read_only_open_of_a_file_without_the_schema_is_refused(tmp_path):
    path = tmp_path / "events.sqlite3"
    path.touch()
    with pytest.raises(StoreError, match="has schema version none"):
        EventStore(path, readonly=True)


def test_an_event_that_breaks_a_rule_is_an_error_not_a_duplicate(store):
    view = OrderView.from_row(order_row())
    body = order_event(view, change="placed", source="poll", received_at=FIXED_NOW, tag_labels={}, previous_event_id="")
    with pytest.raises(StoreError, match="NOT NULL"), store.write() as con:
        store.insert_event(
            con, {**body, "source": None}, order_id=ORDER_ID, consumers=(), order_key=ORDER_ID, now=FIXED_NOW
        )
    assert store.last_seq() == 0


def test_the_last_seq_does_not_go_back_after_a_prune(store):
    old = FIXED_NOW - dt.timedelta(days=40)
    add(store, order_row(), now=old)
    add(store, order_row(order_id="250101000000002"), now=old)
    assert store.prune(FIXED_NOW - dt.timedelta(days=30), doorbells_before=FIXED_NOW) == 2
    assert store.events() == [] and store.last_seq() == 2
    assert add(store, order_row(order_id="250101000000003")) == 3


def test_a_nested_write_is_refused_and_rolls_back_the_block(store):
    view = OrderView.from_row(order_row())

    def nested():
        with store.write() as con:
            store.save_state(con, view, FIXED_NOW, event_id="")
            store.bump("postback_refused", FIXED_NOW)  # writes on its own: not allowed inside write()

    with pytest.raises(StoreError, match="nested write"):
        nested()
    assert store.previous(ORDER_ID) is None and store.counters() == {}
    store.bump("postback_refused", FIXED_NOW)  # the store still works
    assert store.counters()["postback_refused"]["count"] == 1


def test_events_limit_is_bounded(store):
    with store.write() as con:
        for i in range(MAX_EVENTS_LIMIT + 1):
            body = session_event("token_rejected", seed=str(i), received_at=FIXED_NOW)
            store.insert_event(con, body, order_id=None, consumers=(), order_key=body["event_id"], now=FIXED_NOW)
    assert store.events(limit=0) == [] and store.events(limit=-1) == []
    assert store.events(after_seq=1, limit=-1) == []
    newest = store.events(limit=MAX_EVENTS_LIMIT + 500)
    assert len(newest) == MAX_EVENTS_LIMIT and newest[-1]["seq"] == MAX_EVENTS_LIMIT + 1
    assert len(store.events(after_seq=0, limit=10**9)) == MAX_EVENTS_LIMIT
    assert [e["seq"] for e in store.events(after_seq=1, limit=10**9)][:2] == [2, 3]


def test_a_reader_opened_before_a_write_sees_it(store):
    add(store, order_row())
    reader = EventStore(store.path, readonly=True)
    try:
        assert reader.last_seq() == 1
        add(store, order_row(order_id="250101000000002"))
        assert reader.last_seq() == 2
        assert reader.event(2)["order"]["order_id"] == "250101000000002"
    finally:
        reader.close()


def test_a_second_writer_gets_a_store_error_within_its_timeout(store):
    other = EventStore(store.path, timeout_s=0.2)
    try:
        with store.write():
            started = time.monotonic()
            with pytest.raises(StoreError, match="locked"):
                other.ring(ORDER_ID, FIXED_NOW)
            waited = time.monotonic() - started
        assert 0.1 <= waited < 2  # it waited for the lock, then gave up
        other.ring(ORDER_ID, FIXED_NOW)  # and writes once the lock is free
    finally:
        other.close()
    assert store.doorbells() == [(ORDER_ID, 0)]
