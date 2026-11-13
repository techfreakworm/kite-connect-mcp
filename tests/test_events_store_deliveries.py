"""Deliveries: per consumer, each order's events one at a time and in order; retries, alerts, pause and resume."""

from __future__ import annotations

import contextlib
import datetime as dt
import sqlite3

import pytest

from conftest import FIXED_NOW
from events_helpers import order_row
from kite_connect_mcp.events.model import OrderView, order_event
from kite_connect_mcp.events.store import EventStore, iso

T0 = FIXED_NOW
SKIP_NOTE = "skipped by resume --skip-backlog"


@pytest.fixture
def store(tmp_path):
    s = EventStore(tmp_path / "events.sqlite3")
    yield s
    s.close()


def add(store, order_id, price, consumers=("notify",), now=T0):
    view = OrderView.from_row(order_row(order_id=order_id, price=price))
    body = order_event(view, change="placed", source="poll", received_at=now, tag_labels={}, previous_event_id="")
    with store.write() as con:
        return store.insert_event(con, body, order_id=order_id, consumers=consumers, order_key=order_id, now=now)


def row(store, consumer, seq):
    """(status, attempts, delayed_sent, last_error) of one delivery, read from the file by a connection of its own;
    None when there is no such delivery."""
    with contextlib.closing(sqlite3.connect(store.path)) as con:
        return con.execute(
            "SELECT status, attempts, delayed_sent, last_error FROM deliveries WHERE consumer = ? AND seq = ?",
            (consumer, seq),
        ).fetchone()


def test_the_next_delivery_follows_seq_and_is_due_at_once(store):
    add(store, "1", 100)
    add(store, "2", 100)
    delivery, later = store.next_delivery("notify", T0)
    assert delivery is not None and delivery.seq == 1 and delivery.attempts == 0 and later is None
    assert store.next_delivery("other", T0) == (None, None)


def test_an_order_in_retry_holds_back_only_its_own_events(store):
    add(store, "1", 100)  # seq 1
    add(store, "1", 101)  # seq 2, the same order
    add(store, "2", 100)  # seq 3, another order
    store.retry_delivery(
        "notify", 1, first_try_at=T0, next_try_at=T0 + dt.timedelta(seconds=10), now=T0, error="exit 75"
    )
    delivery, later = store.next_delivery("notify", T0)
    assert delivery.seq == 3  # seq 2 waits behind seq 1
    assert later == T0 + dt.timedelta(seconds=10)
    store.finish_delivery("notify", 3, "done", T0)
    assert store.next_delivery("notify", T0) == (None, T0 + dt.timedelta(seconds=10))
    delivery, _ = store.next_delivery("notify", T0 + dt.timedelta(seconds=10))
    assert (
        delivery.seq == 1
        and delivery.attempts == 1
        and delivery.first_try_at == T0
        and delivery.last_error == "exit 75"
    )
    store.finish_delivery("notify", 1, "done", T0)
    assert store.next_delivery("notify", T0)[0].seq == 2


def test_alerts_for_deliveries_retrying_past_the_alert_point(store):
    add(store, "1", 100)
    store.retry_delivery(
        "notify", 1, first_try_at=T0, next_try_at=T0 + dt.timedelta(seconds=300), now=T0, error="exit 75"
    )
    assert store.overdue_alerts("notify", T0 + dt.timedelta(seconds=59), 60) == []
    assert store.next_alert_at("notify", 60) == T0 + dt.timedelta(seconds=60)
    due = store.overdue_alerts("notify", T0 + dt.timedelta(seconds=60), 60)
    assert [d.seq for d in due] == [1]
    store.mark_alerted("notify", 1)
    assert store.overdue_alerts("notify", T0 + dt.timedelta(seconds=600), 60) == []
    assert store.next_alert_at("notify", 60) is None


def test_failed_deliveries_can_be_retried(store):
    add(store, "1", 100)
    store.finish_delivery("notify", 1, "failed", T0, "exit 1: boom")
    assert store.next_delivery("notify", T0) == (None, None)
    assert store.retry_failed("notify", T0) == 1
    delivery, _ = store.next_delivery("notify", T0)
    assert delivery.seq == 1 and delivery.attempts == 0 and delivery.first_try_at is None


def test_pause_resume_and_skip_the_backlog(store):
    store.pause("notify", T0)
    assert store.is_paused("notify") and store.paused() == {"notify": iso(T0)}
    add(store, "1", 100)
    add(store, "2", 100)
    assert store.resume("notify", skip_backlog=False, now=T0) == 0
    assert not store.is_paused("notify")
    store.pause("notify", T0)
    assert store.resume("notify", skip_backlog=True, now=T0) == 2
    assert store.next_delivery("notify", T0) == (None, None)


def test_the_consumer_names_are_those_with_deliveries_or_paused(store):
    assert store.consumer_names() == set()
    add(store, "1", 100, consumers=("notify", "audit"))
    store.pause("retired", T0)
    store.pause("notify", T0)
    assert store.consumer_names() == {"notify", "audit", "retired"}
    reader = EventStore(store.path, readonly=True)
    assert reader.consumer_names() == {"notify", "audit", "retired"}
    reader.close()


def test_the_summary(store):
    add(store, "1", 100)
    add(store, "2", 100)
    add(store, "3", 100)
    store.finish_delivery("notify", 1, "done", T0)
    store.retry_delivery("notify", 2, first_try_at=T0, next_try_at=T0, now=T0, error="exit 75")
    store.mark_alerted("notify", 2)
    store.finish_delivery("notify", 3, "failed", T0, "exit 2")
    store.ring("9", T0)
    store.pause("other", T0)
    out = store.summary()
    notify = out["consumers"]["notify"]
    assert (notify["done"], notify["pending"], notify["retrying"], notify["delayed"], notify["failed"]) == (
        1,
        1,
        1,
        1,
        1,
    )
    assert notify["last_error"] == "exit 2"
    assert out["doorbells_waiting"] == 1
    assert out["last_seq"] == 3
    assert "other" in out["paused"]


# --- beyond the plan's tests ----------------------------------------------------------------------------------------


def test_a_final_delivery_lets_the_next_event_of_its_order_go(store):
    for price in (100, 101, 102):
        add(store, "1", price)  # seq 1, 2, 3: one order
    store.finish_delivery("notify", 1, "failed", T0, "exit 1")
    assert store.next_delivery("notify", T0)[0].seq == 2  # a failed delivery holds nothing back
    store.finish_delivery("notify", 2, "skipped", T0)
    assert store.next_delivery("notify", T0)[0].seq == 3  # nor does a skipped one


def test_consumers_are_independent(store):
    add(store, "1", 100, consumers=("notify", "log"))
    add(store, "1", 101, consumers=("notify", "log"))
    store.retry_delivery("notify", 1, first_try_at=T0, next_try_at=T0 + dt.timedelta(seconds=30), now=T0, error="x")
    store.finish_delivery("log", 1, "done", T0)
    assert store.next_delivery("log", T0)[0].seq == 2  # notify retrying seq 1 does not hold back log
    assert store.next_delivery("notify", T0) == (None, T0 + dt.timedelta(seconds=30))
    store.finish_delivery("notify", 1, "failed", T0, "exit 1")
    store.pause("notify", T0)
    assert store.resume("notify", skip_backlog=True, now=T0) == 1  # notify's seq 2 only
    assert store.retry_failed("log", T0) == 0
    assert store.next_delivery("log", T0)[0].seq == 2


def test_retries_keep_the_first_try_time_and_count_the_attempts(store):
    add(store, "1", 100)
    t1 = T0 + dt.timedelta(seconds=10)
    store.retry_delivery("notify", 1, first_try_at=T0, next_try_at=t1, now=T0, error="exit 75")
    store.retry_delivery("notify", 1, first_try_at=t1, next_try_at=t1 + dt.timedelta(seconds=20), now=t1, error=None)
    delivery, _ = store.next_delivery("notify", t1 + dt.timedelta(seconds=20))
    assert delivery.attempts == 2 and delivery.first_try_at == T0
    assert delivery.last_error == "exit 75"  # a try without an error keeps the last one
    assert store.next_alert_at("notify", 60) == T0 + dt.timedelta(seconds=60)  # counted from the first try


def test_a_new_error_replaces_the_last_one_and_none_keeps_it(store):
    add(store, "1", 100)
    later = T0 + dt.timedelta(seconds=10)
    store.retry_delivery("notify", 1, first_try_at=T0, next_try_at=later, now=T0, error="exit 75")
    store.retry_delivery("notify", 1, first_try_at=T0, next_try_at=later, now=T0, error="timeout after 30 s")
    assert row(store, "notify", 1) == ("pending", 2, 0, "timeout after 30 s")
    store.finish_delivery("notify", 1, "failed", T0)
    assert row(store, "notify", 1) == ("failed", 2, 0, "timeout after 30 s")


def test_finish_refuses_a_status_that_is_not_final(store):
    add(store, "1", 100)
    with pytest.raises(ValueError, match="not a final delivery status: 'pending'"):
        store.finish_delivery("notify", 1, "pending", T0)
    with pytest.raises(ValueError, match="not a final delivery status"):
        store.finish_delivery("notify", 1, "retrying", T0)
    assert store.next_delivery("notify", T0)[0].attempts == 0


def test_the_summary_of_an_empty_store(store):
    out = store.summary()
    assert (out["last_seq"], out["doorbells_waiting"], out["consumers"], out["paused"]) == (0, 0, {}, {})
    assert out["counters"] == {} and out["baseline_done"] is None and out["after_close_ok"] is None


def test_the_summary_lists_the_consumers_it_is_given_even_with_no_deliveries(store):
    add(store, "1", 100, consumers=("notify",))
    store.finish_delivery("notify", 1, "done", T0)
    consumers = store.summary(["quiet", "notify"])["consumers"]
    assert list(consumers) == ["quiet", "notify"]  # in the order given
    assert consumers["quiet"] == {
        "pending": 0,
        "retrying": 0,
        "delayed": 0,
        "failed": 0,
        "done": 0,
        "skipped": 0,
        "last_done_at": None,
        "last_error": None,
    }
    assert consumers["notify"]["done"] == 1
    assert list(store.summary()["consumers"]) == ["notify"]


def test_the_summary_details_and_a_read_only_store(store):
    for order_id in ("1", "2", "3", "4"):
        add(store, order_id, 100)
    store.finish_delivery("notify", 1, "done", T0)
    store.finish_delivery("notify", 2, "done", T0 + dt.timedelta(seconds=5))
    store.finish_delivery("notify", 3, "failed", T0, "exit 1")
    store.finish_delivery("notify", 4, "failed", T0, "exit 2")  # stamped at the same time: the later seq wins
    store.bump("postback_refused", T0, "bad checksum")
    store.set_meta("baseline_done", "2026-11-04")
    reader = EventStore(store.path, readonly=True)
    try:
        out = reader.summary()
    finally:
        reader.close()
    notify = out["consumers"]["notify"]
    assert (notify["done"], notify["failed"], notify["pending"], notify["skipped"]) == (2, 2, 0, 0)
    assert notify["last_done_at"].startswith("2026-11-04T10:00:05")
    assert notify["last_error"] == "exit 2"
    assert out["counters"]["postback_refused"]["count"] == 1
    assert out["baseline_done"] == "2026-11-04"
    add(store, "5", 100)
    store.pause("notify", T0)
    store.resume("notify", skip_backlog=True, now=T0)
    notify = store.summary()["consumers"]["notify"]
    assert (notify["pending"], notify["skipped"], notify["last_error"]) == (0, 1, "exit 2")  # a skip is not a failure


# --- a delivery decided elsewhere is left alone ---------------------------------------------------------------------


def test_the_updates_say_whether_they_changed_a_pending_delivery(store):
    add(store, "1", 100)
    add(store, "2", 100)
    later = T0 + dt.timedelta(seconds=10)
    assert store.retry_delivery("notify", 1, first_try_at=T0, next_try_at=later, now=T0, error="exit 75") is True
    assert store.mark_alerted("notify", 1) is True
    assert store.finish_delivery("notify", 1, "done", later) is True
    assert store.finish_delivery("notify", 1, "failed", later, "exit 1") is False  # done already
    assert row(store, "notify", 1) == ("done", 1, 1, "exit 75")
    assert store.retry_delivery("notify", 1, first_try_at=T0, next_try_at=later, now=T0, error="x") is False
    assert store.mark_alerted("notify", 2) is True
    assert store.finish_delivery("other", 2, "done", T0) is False  # no delivery of that consumer
    assert store.finish_delivery("notify", 99, "done", T0) is False  # no such event


def test_a_skip_by_resume_survives_a_late_finish(store):
    add(store, "1", 100)
    picked, _ = store.next_delivery("notify", T0)  # the router picks seq 1 and runs the command ...
    store.pause("notify", T0)
    assert store.resume("notify", skip_backlog=True, now=T0) == 1  # ... while the operator skips the backlog
    assert store.finish_delivery("notify", picked.seq, "failed", T0, "exit 1") is False
    assert row(store, "notify", 1) == ("skipped", 0, 0, SKIP_NOTE)
    assert store.retry_failed("notify", T0) == 0  # so `events retry` does not send what the operator skipped
    assert store.finish_delivery("notify", picked.seq, "done", T0) is False  # a late success leaves it skipped too
    assert row(store, "notify", 1) == ("skipped", 0, 0, SKIP_NOTE)
    assert store.next_delivery("notify", T0) == (None, None)


def test_a_skip_by_resume_survives_a_late_retry(store):
    add(store, "1", 100)
    picked, _ = store.next_delivery("notify", T0)
    assert store.resume("notify", skip_backlog=True, now=T0) == 1  # works on a consumer that is not paused, too
    later = T0 + dt.timedelta(seconds=10)
    assert (
        store.retry_delivery("notify", picked.seq, first_try_at=T0, next_try_at=later, now=T0, error="exit 75") is False
    )
    assert row(store, "notify", 1) == ("skipped", 0, 0, SKIP_NOTE)
    assert store.mark_alerted("notify", picked.seq) is False
    assert row(store, "notify", 1) == ("skipped", 0, 0, SKIP_NOTE)
    assert store.next_delivery("notify", later) == (None, None)
    assert store.retry_failed("notify", T0) == 0


def test_finish_mark_alerted_and_event_can_join_a_write(store):
    add(store, "1", 100)
    with pytest.raises(RuntimeError), store.write() as con:  # noqa: PT012 (the block is the write)
        assert store.event(1, con=con)["seq"] == 1
        assert store.mark_alerted("notify", 1, con=con) is True
        assert store.finish_delivery("notify", 1, "failed", T0, "exit 1", con=con) is True
        assert store.finish_delivery("notify", 1, "done", T0, con=con) is False  # failed already, in this write
        raise RuntimeError("roll back")
    assert row(store, "notify", 1) == ("pending", 0, 0, None)  # rolled back with the block
    with store.write() as con:
        assert store.finish_delivery("notify", 1, "failed", T0, "exit 1", con=con) is True
    assert row(store, "notify", 1) == ("failed", 0, 0, "exit 1")
    with store.write() as con:
        assert store.event(99, con=con) is None


def test_an_update_of_a_pruned_delivery_changes_nothing(store):
    add(store, "1", 100)
    picked, _ = store.next_delivery("notify", T0)
    assert store.prune(T0 + dt.timedelta(seconds=1), doorbells_before=T0) == 1
    later = T0 + dt.timedelta(seconds=10)
    assert store.retry_delivery("notify", picked.seq, first_try_at=T0, next_try_at=later, now=T0, error="x") is False
    assert store.mark_alerted("notify", picked.seq) is False
    assert store.finish_delivery("notify", picked.seq, "failed", T0, "exit 1") is False
    assert row(store, "notify", picked.seq) is None
    assert store.retry_failed("notify", T0) == 0


def test_a_resent_failed_event_can_come_after_later_events_of_its_order(store):
    add(store, "1", 100)  # seq 1
    add(store, "1", 101)  # seq 2, the same order
    store.finish_delivery("notify", 1, "failed", T0, "exit 1")
    store.finish_delivery("notify", 2, "done", T0)
    add(store, "1", 102)  # seq 3, the same order, not delivered yet
    assert store.retry_failed("notify", T0) == 1
    assert store.next_delivery("notify", T0)[0].seq == 1  # sent again after seq 2: a consumer can tell from seq
    store.finish_delivery("notify", 1, "done", T0)
    assert store.next_delivery("notify", T0)[0].seq == 3  # seq 3 waited behind the re-sent seq 1
