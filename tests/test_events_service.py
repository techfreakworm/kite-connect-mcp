"""The event service's ingest path: what becomes an event, who it is delivered to, and the counters."""

from __future__ import annotations

import asyncio
import datetime as dt
import json

import pytest

from conftest import FIXED_NOW
from events_helpers import ORDER_ID, PY, Clock, filled_row, make_service, order_row
from kite_connect_mcp.errors import ConfigError
from kite_connect_mcp.events.model import OrderView, order_event
from kite_connect_mcp.events.service import REFUSALS
from kite_connect_mcp.events.store import EventStore, StoreError, iso
from kite_connect_mcp.timeutil import IST

NOTIFY = {
    "name": "notify",
    "command": [PY, "-c", "pass"],
    "match": {"kinds": ["order", "session", "router", "selftest"]},
}
FILLS = {"name": "fills", "command": [PY, "-c", "pass"], "match": {"changes": ["filled"], "tags": ["mytag"]}}


SELFTEST_ID = "selftest-0a1b2c3d4e5f"


@pytest.fixture
def svc(service_factory):
    service, _ = service_factory(consumers=[NOTIFY, FILLS], tag_labels={"mytag": "my strategy"})
    return service


def deliveries(svc, consumer):
    return [
        row[0] for row in svc.store._read("SELECT seq FROM deliveries WHERE consumer = ? ORDER BY seq", (consumer,))
    ]


def test_a_new_order_then_its_fill(svc):
    seq = svc.ingest_order(order_row(), source="poll")
    assert svc.store.event(seq)["change"] == "placed"
    seq2 = svc.ingest_order(filled_row(), source="postback")
    body = svc.store.event(seq2)
    assert body["change"] == "filled" and body["source"] == "postback"
    assert body["text"] == "BUY 25 ABCD (NSE) filled at 1234.50: complete [my strategy]"
    assert deliveries(svc, "notify") == [seq, seq2]
    assert deliveries(svc, "fills") == [seq2]


def test_the_same_state_from_both_sources_is_stored_once(svc):
    assert svc.ingest_order(filled_row(), source="postback") is not None
    assert svc.ingest_order(filled_row(), source="poll") is None
    assert svc.store.last_seq() == 1


def test_nothing_after_a_final_status_and_nothing_stale(svc):
    svc.ingest_order(order_row(filled_quantity=10, pending_quantity=15), source="poll")
    assert svc.ingest_order(order_row(filled_quantity=0), source="postback") is None
    svc.ingest_order(filled_row(), source="poll")
    assert svc.ingest_order(order_row(status="CANCELLED"), source="poll") is None


def test_a_modification_back_to_an_earlier_state_is_reported(svc):
    a = svc.ingest_order(order_row(price=100, exchange_update_timestamp="2026-11-04 09:30:01"), source="poll")
    b = svc.ingest_order(order_row(price=101, exchange_update_timestamp="2026-11-04 09:31:00"), source="poll")
    c = svc.ingest_order(order_row(price=100, exchange_update_timestamp="2026-11-04 09:32:00"), source="poll")
    assert None not in (a, b, c)
    assert svc.store.event(c)["change"] == "modified"
    assert len({svc.store.event(seq)["event_id"] for seq in (a, b, c)}) == 3
    assert svc.store.previous(ORDER_ID).state[3] == "100"


def test_a_late_read_of_an_older_state_is_dropped(svc):
    svc.ingest_order(order_row(price=100, exchange_update_timestamp="2026-11-04 09:30:01"), source="poll")
    svc.ingest_order(order_row(price=101, exchange_update_timestamp="2026-11-04 09:31:00"), source="poll")
    late = order_row(price=100, exchange_update_timestamp="2026-11-04 09:30:01")
    assert svc.ingest_order(late, source="postback") is None
    assert svc.store.previous(ORDER_ID).state[3] == "101"


def test_each_event_records_the_state_it_reported(svc):
    seq = svc.ingest_order(order_row(), source="poll")
    assert svc.store.previous(ORDER_ID).event_id == svc.store.event(seq)["event_id"]
    seq2 = svc.ingest_order(filled_row(), source="postback")
    assert svc.store.previous(ORDER_ID).event_id == svc.store.event(seq2)["event_id"]
    assert svc.store.previous(ORDER_ID).final


def test_the_baseline_records_states_and_tells_nobody(svc):
    assert svc.ingest_order(order_row(), source="poll", baseline=True) is None
    assert svc.store.last_seq() == 0
    assert svc.store.previous(ORDER_ID) is not None
    assert svc.ingest_order(filled_row(), source="poll")  # the next change is reported


def test_the_baseline_leaves_a_known_order_alone(svc):
    """After a restart, a change made while the service was down is still reported by the next read."""
    svc.ingest_order(order_row(), source="poll")
    assert svc.ingest_order(filled_row(), source="poll", baseline=True) is None
    assert svc.store.previous(ORDER_ID).state[0] == "OPEN"
    seq = svc.ingest_order(filled_row(), source="poll")
    assert svc.store.event(seq)["change"] == "filled"


def test_a_bad_row_raises_value_error(svc):
    with pytest.raises(ValueError, match="order_id"):
        svc.ingest_order(order_row(order_id="no good"), source="poll")


def test_token_rejected_is_raised_once_per_token(svc):
    first = svc.note_token_rejected("token-one")
    assert svc.store.event(first)["session"] == "token_rejected"
    assert svc.note_token_rejected("token-one") is None
    assert svc.note_token_rejected("token-two") is not None
    assert "token-one" not in str(svc.store.event(first))
    assert deliveries(svc, "notify") == [1, 2] and deliveries(svc, "fills") == []


def test_a_selftest_reaches_only_the_consumers_that_ask_for_it(svc):
    seq = svc.record_selftest("selftest-0a1b2c3d4e5f")
    assert svc.store.event(seq)["kind"] == "selftest"
    assert deliveries(svc, "notify") == [seq] and deliveries(svc, "fills") == []
    assert svc.store.counters()["selftest_received"]["count"] == 1


def test_a_router_event_never_reaches_its_own_consumer_and_never_cascades(svc):
    about = svc.store.event(svc.ingest_order(filled_row(), source="poll"))
    seq = svc.record_router_event("delivery_failed", consumer="fills", about=about, error="exit 1", seed="t")
    assert deliveries(svc, "notify")[-1] == seq
    assert seq not in deliveries(svc, "fills")
    seq2 = svc.record_router_event("delivery_failed", consumer="notify", about=about, error="exit 1", seed="t")
    assert seq2 not in deliveries(svc, "notify")
    router = svc.store.event(seq)
    assert svc.record_router_event("delivery_failed", consumer="fills", about=router, error="x", seed="t2") is None


def test_fail_delivery_records_the_failure_with_its_router_event(svc):
    svc.ingest_order(order_row(), source="poll")
    seq = svc.ingest_order(filled_row(), source="poll")  # a fill: for fills too
    seen = []
    svc.on_event(lambda: seen.append([e["kind"] for e in svc.store.events()]))
    delivery, _ = svc.store.next_delivery("fills", FIXED_NOW)
    assert svc.fail_delivery("fills", delivery, "exit 1: boom", FIXED_NOW) is True
    (router,) = [e for e in svc.store.events() if e["kind"] == "router"]
    assert router["router"] == "delivery_failed" and router["consumer"] == "fills" and router["about_seq"] == seq
    assert router["error"] == "exit 1: boom"
    assert deliveries(svc, "notify")[-1] == router["seq"] and router["seq"] not in deliveries(svc, "fills")
    assert seen == [["order", "order", "router"]]  # told once, after the commit
    assert svc.store.summary()["consumers"]["fills"]["last_error"] == "exit 1: boom"
    assert svc.fail_delivery("fills", delivery, "exit 1: again", FIXED_NOW) is False  # no longer pending
    assert len([e for e in svc.store.events() if e["kind"] == "router"]) == 1 and len(seen) == 1


def test_a_failed_delivery_of_a_router_event_raises_no_other(svc):
    about = svc.store.event(svc.ingest_order(filled_row(), source="poll"))
    svc.record_router_event("delivery_failed", consumer="fills", about=about, error="exit 1", seed="t")
    delivery, _ = svc.store.next_delivery("notify", FIXED_NOW)
    while svc.store.event(delivery.seq)["kind"] != "router":
        svc.store.finish_delivery("notify", delivery.seq, "done", FIXED_NOW)
        delivery, _ = svc.store.next_delivery("notify", FIXED_NOW)
    last = svc.store.last_seq()
    assert svc.fail_delivery("notify", delivery, "exit 1", FIXED_NOW) is True
    assert svc.store.last_seq() == last and svc.store.summary()["consumers"]["notify"]["failed"] == 1


def test_alert_delayed_marks_the_delivery_and_records_its_router_event_once(svc):
    svc.ingest_order(order_row(), source="poll")
    seq = svc.ingest_order(filled_row(), source="poll")
    later = FIXED_NOW + dt.timedelta(seconds=90)
    svc.store.retry_delivery(
        "fills", seq, first_try_at=FIXED_NOW, next_try_at=later, now=FIXED_NOW, error="exit 75: busy"
    )
    (delivery,) = svc.store.overdue_alerts("fills", later, 60)
    assert svc.alert_delayed("fills", delivery, later) is True
    (router,) = [e for e in svc.store.events() if e["kind"] == "router"]
    assert router["router"] == "delivery_delayed" and router["about_seq"] == seq and router["error"] == "exit 75: busy"
    assert router["seq"] in deliveries(svc, "notify") and router["seq"] not in deliveries(svc, "fills")
    assert svc.store.summary()["consumers"]["fills"]["delayed"] == 1
    assert svc.store.overdue_alerts("fills", later, 60) == []
    svc.alert_delayed("fills", delivery, later)  # a repeat records nothing new
    assert len([e for e in svc.store.events() if e["kind"] == "router"]) == 1
    svc.store.finish_delivery("fills", seq, "done", later)
    assert svc.alert_delayed("fills", delivery, later) is False  # no longer pending


def test_ring_and_refused(svc):
    svc.ring(ORDER_ID)
    assert svc.store.doorbells() == [(ORDER_ID, 0)]
    assert svc.store.counters()["postback_accepted"]["count"] == 1
    svc.refused("bad checksum")
    assert svc.store.counters()["postback_refused"] == {"count": 1, "last_at": iso(FIXED_NOW), "note": "bad checksum"}


def test_listeners_are_told_about_new_events_and_doorbells(svc):
    seen = []
    svc.on_event(lambda: seen.append("event"))
    svc.on_doorbell(lambda: seen.append("doorbell"))
    svc.ingest_order(order_row(), source="poll")
    svc.ingest_order(order_row(), source="poll")  # nothing new: nobody is told
    svc.ring(ORDER_ID)
    assert seen == ["event", "doorbell"]


def test_a_failing_listener_neither_stops_the_others_nor_loses_the_event(svc):
    seen = []

    def broken():
        raise RuntimeError("listener bug")

    svc.on_event(broken)
    svc.on_event(lambda: seen.append("event"))
    seq = svc.ingest_order(order_row(), source="poll")
    assert seq == 1 and svc.store.event(seq) is not None
    assert seen == ["event"]


def test_ring_fails_loudly_when_the_store_cannot_write(svc):
    svc.store.close()
    with pytest.raises(StoreError):
        svc.ring(ORDER_ID)
    svc.refused("bad checksum")  # never raises for the store


def test_a_service_needs_a_store(runtime_factory, tmp_path):
    with pytest.raises(ConfigError, match="KITE_MCP_EVENTS_DB"):
        make_service(runtime_factory, tmp_path, KITE_MCP_EVENTS_DB=None)


def test_the_account_pin(service_factory, tmp_path):
    open_svc, _ = service_factory(tmp_path / "a")
    assert open_svc.user_allowed("ZZ9999") and open_svc.user_allowed(None)
    pinned, _ = service_factory(tmp_path / "b", KITE_MCP_ALLOWED_USER_ID="ab1234")
    assert pinned.user_allowed("AB1234") and pinned.user_allowed(" ab1234 ")
    assert not pinned.user_allowed("ZZ9999") and not pinned.user_allowed(None) and not pinned.user_allowed(5)


def test_the_status(service_factory):
    clock = Clock()
    svc, _ = service_factory(consumers=[NOTIFY], clock=clock)
    seq = svc.ingest_order(order_row(), source="poll")
    out = svc.status()
    assert out["consumers"]["notify"]["pending"] == 1
    assert out["configured_consumers"] == ["notify"]
    assert out["store"] == str(svc.store.path)
    shown = json.dumps(out)
    assert ORDER_ID not in shown and svc.store.event(seq)["event_id"] not in shown  # no ids of orders or events
    assert out["poll"] == {"interval_s": 60, "window": "09:00-16:00", "days": [0, 1, 2, 3, 4], "in_window_now": True}
    assert "warning" not in out  # the service has not polled yet: no close was missed


def test_the_status_lists_a_configured_consumer_with_no_deliveries_yet(service_factory):
    svc, _ = service_factory(consumers=[NOTIFY, FILLS])
    svc.ingest_order(order_row(), source="poll")  # for notify only
    out = svc.status()
    assert out["configured_consumers"] == ["notify", "fills"]
    assert out["consumers"]["fills"]["pending"] == 0 and out["consumers"]["fills"]["last_error"] is None


def at(day: int, hour: int, minute: int = 0) -> dt.datetime:
    """A moment of November 2026 in IST: the 4th is a Wednesday, the 6th a Friday, the 9th a Monday."""
    return dt.datetime(2026, 11, day, hour, minute, tzinfo=IST)


def test_the_status_warns_when_no_poll_succeeded_after_the_close(service_factory):
    clock = Clock(at(4, 16, 30))  # a Wednesday, after the poll window
    svc, _ = service_factory(clock=clock)
    svc.store.set_meta("baseline_done", "2026-11-04")  # the service polled today
    out = svc.status()
    assert not out["poll"]["in_window_now"]
    assert out["warning"] == (
        "no successful poll after the close on Wed 2026-11-04: fills after the last poll may be missing"
    )
    svc.store.set_meta("after_close_ok", "2026-11-04")
    assert "warning" not in svc.status()
    clock.now = at(7, 17)  # a Saturday: Friday's close is the last one, and no poll after it succeeded
    assert "Fri 2026-11-06" in svc.status()["warning"]


def test_a_missed_friday_close_stays_in_the_warning_until_a_later_close_is_polled(service_factory):
    clock = Clock()
    svc, _ = service_factory(clock=clock)
    svc.store.set_meta("baseline_done", "2026-11-02")
    svc.store.set_meta("after_close_ok", "2026-11-05")  # Thursday's after-close poll worked, Friday's did not
    friday = "no successful poll after the close on Fri 2026-11-06: fills after the last poll may be missing"
    clock.now = at(6, 15, 59)  # Friday, before its window closes: Thursday's close is the last one, and it was polled
    assert "warning" not in svc.status()
    for moment in (at(6, 16), at(6, 17), at(7, 12), at(8, 23, 59), at(9, 0, 1), at(9, 10), at(9, 15, 59)):
        clock.now = moment  # Friday after the close, the weekend, Monday until its own window closes
        assert svc.status().get("warning") == friday, moment
    clock.now = at(9, 16)  # Monday's window has closed: Monday's after-close poll is the one missing now
    assert "Mon 2026-11-09" in svc.status()["warning"]
    svc.store.set_meta("after_close_ok", "2026-11-09")
    assert "warning" not in svc.status()
    clock.now = at(9, 10)
    svc.store.set_meta("after_close_ok", "2026-11-06")  # had Friday's poll worked, Monday morning would be quiet
    assert "warning" not in svc.status()


def test_the_warning_follows_the_configured_poll_days(service_factory):
    clock = Clock(at(4, 10))  # a Wednesday, inside the window
    svc, _ = service_factory(clock=clock, KITE_MCP_ORDER_POLL_DAYS="mon,wed,fri")
    svc.store.set_meta("baseline_done", "2026-10-26")
    svc.store.set_meta("after_close_ok", "2026-10-30")  # the Friday before
    assert "Mon 2026-11-02" in svc.status()["warning"]  # Tuesday is not a poll day: Monday's close is the last one
    svc.store.set_meta("after_close_ok", "2026-11-02")
    assert "warning" not in svc.status()


def test_no_warning_about_a_close_before_the_first_poll(service_factory):
    clock = Clock(at(7, 11))  # a Saturday
    svc, _ = service_factory(clock=clock)
    svc.store.set_meta("baseline_done", iso(at(7, 11)))  # the first poll ever ran this Saturday morning
    for moment in (at(7, 11), at(8, 12), at(9, 10), at(9, 15, 59)):
        clock.now = moment  # Friday's close is the last one until Monday's: the service did not run on Friday
        assert "warning" not in svc.status(), moment
    clock.now = at(9, 16)  # Monday's close came after the baseline, and no poll after it succeeded
    assert "Mon 2026-11-09" in svc.status()["warning"]
    svc.store.set_meta("after_close_ok", "2026-11-09")
    assert "warning" not in svc.status()
    clock.now = at(9, 10)  # Monday morning: Friday's close is the last one
    svc.store.set_meta("after_close_ok", "2026-11-05")
    assert "warning" not in svc.status()  # the baseline on Saturday came after it
    svc.store.set_meta("baseline_done", "not a time")  # a baseline time that cannot be read hides no warning
    assert "Fri 2026-11-06" in svc.status()["warning"]


def test_no_warning_before_the_service_has_polled(service_factory):
    svc, _ = service_factory(clock=Clock(at(6, 17)))
    assert svc.store.meta("baseline_done") is None and svc.store.meta("after_close_ok") is None
    assert "warning" not in svc.status()


# --- each counter is written with what it counts --------------------------------------------------------------------


def drop_table(svc, table):
    with svc.store.write() as con:
        con.execute(f"DROP TABLE {table}")


def count_rows(svc, table):
    return svc.store._read(f"SELECT count(*) FROM {table}")[0][0]


@pytest.mark.parametrize("table", ["counters", "doorbells"])
def test_a_ring_keeps_its_doorbell_and_its_count_together_or_neither(svc, table):
    rung = []
    svc.on_doorbell(lambda: rung.append(1))
    drop_table(svc, table)
    with pytest.raises(StoreError):
        svc.ring(ORDER_ID)
    assert rung == []
    if table != "doorbells":
        assert svc.store.doorbells() == []  # a StoreError means nothing was kept
    if table != "counters":
        assert "postback_accepted" not in svc.store.counters()


@pytest.mark.parametrize("table", ["counters", "events"])
def test_a_selftest_is_counted_and_stored_together_or_neither(svc, table):
    told = []
    svc.on_event(lambda: told.append(1))
    drop_table(svc, table)
    with pytest.raises(StoreError):
        svc.record_selftest(SELFTEST_ID)
    assert told == []
    if table != "events":
        assert count_rows(svc, "events") == 0
    if table != "counters":
        assert "selftest_received" not in svc.store.counters()


def test_a_repeated_selftest_is_counted_each_time_and_stored_once(svc):
    """selftest_received counts verified selftest postbacks, as postback_accepted counts every ring."""
    told = []
    svc.on_event(lambda: told.append(1))
    assert svc.record_selftest(SELFTEST_ID) == 1
    assert svc.record_selftest(SELFTEST_ID) is None
    assert svc.store.counters()["selftest_received"]["count"] == 2
    assert svc.store.last_seq() == 1 and told == [1]


def test_a_selftest_with_a_bad_id_raises_and_counts_nothing(svc):
    for bad in (ORDER_ID, "selftest-", "selftest-no good"):
        with pytest.raises(ValueError, match="self-test"):
            svc.record_selftest(bad)
    assert "selftest_received" not in svc.store.counters() and svc.store.last_seq() == 0


@pytest.mark.parametrize("bad", ["", "no good", "../orders", "x" * 65, None, 5])
def test_ring_refuses_what_is_not_an_order_id(svc, bad):
    rung = []
    svc.on_doorbell(lambda: rung.append(1))
    with pytest.raises(ValueError, match="order id"):
        svc.ring(bad)
    assert svc.store.doorbells() == [] and "postback_accepted" not in svc.store.counters() and rung == []


def test_refused_takes_only_the_known_reasons(svc):
    assert REFUSALS == (
        "no API secret",
        "body over the cap",
        "body not JSON",
        "body not a JSON object",
        "bad checksum",
        "bad order id",
        "another account",
    )
    for reason in REFUSALS:
        svc.refused(reason)
    with pytest.raises(ValueError, match="REFUSALS"):
        svc.refused("user AB1234 sent this")  # text from a request never reaches the status
    svc.flush_refusals()  # all in one second: the first was written at once, the rest waited in memory
    out = svc.store.counters()["postback_refused"]
    assert out["count"] == len(REFUSALS) and out["note"] == "another account"


def refused_count(svc):
    return svc.store.counters()["postback_refused"]["count"]


def test_a_burst_of_refusals_is_written_once_a_second_and_ends_with_the_exact_count(service_factory, monkeypatch):
    clock = Clock()
    svc, _ = service_factory(clock=clock)
    writes = []
    bump = svc.store.bump
    monkeypatch.setattr(svc.store, "bump", lambda *a, **k: (writes.append(k.get("by", 1)), bump(*a, **k)))
    svc.refused("bad checksum")  # the first after a quiet second: written at once
    assert svc.store.counters()["postback_refused"] == {"count": 1, "last_at": iso(clock.now), "note": "bad checksum"}
    for _ in range(99):
        svc.refused("body not JSON")
    clock.advance(0.5)
    svc.refused("bad checksum")
    assert refused_count(svc) == 1  # the rest of that second waits in memory
    last = clock.advance(0.5)  # a second after the write
    svc.refused("another account")
    assert svc.store.counters()["postback_refused"] == {"count": 102, "last_at": iso(last), "note": "another account"}
    assert writes == [1, 101]


def test_a_clock_that_went_back_does_not_hold_the_writes(service_factory):
    clock = Clock()
    svc, _ = service_factory(clock=clock)
    svc.refused("bad checksum")
    clock.advance(-3600)
    svc.refused("bad checksum")
    assert refused_count(svc) == 2


async def test_refusals_left_in_memory_are_written_a_moment_later(service_factory):
    svc, _ = service_factory()
    svc.REFUSAL_WRITE_S = 0.05
    svc.refused("bad checksum")
    svc.refused("bad checksum")
    svc.refused("body not JSON")
    assert refused_count(svc) == 1
    await asyncio.sleep(0.2)
    out = svc.store.counters()["postback_refused"]
    assert out["count"] == 3 and out["note"] == "body not JSON"


async def test_stop_writes_the_refusals_still_in_memory(service_factory):
    svc, _ = service_factory()
    for _ in range(50):
        svc.refused("bad checksum")
    assert refused_count(svc) == 1
    await svc.stop()
    store = EventStore(svc.store.path, readonly=True)
    try:
        assert store.counters()["postback_refused"]["count"] == 50
    finally:
        store.close()


def test_refusals_the_store_could_not_take_wait_for_the_next_write(service_factory, monkeypatch, caplog):
    clock = Clock()
    svc, _ = service_factory(clock=clock)
    bump = svc.store.bump

    def broken(*args, **kwargs):
        raise StoreError("cannot write the event store: disk I/O error")

    monkeypatch.setattr(svc.store, "bump", broken)
    svc.refused("bad checksum")  # never raises for the store
    svc.refused("bad checksum")
    assert "could not count" in caplog.text
    monkeypatch.setattr(svc.store, "bump", bump)
    clock.advance(1)
    svc.refused("bad checksum")
    assert refused_count(svc) == 3


def test_an_event_stored_already_brings_the_order_state_up_to_date(svc):
    """judge() drops a change reported twice; an event with the same id is found only when the order's state row is
    behind its events, and then the state catches up."""
    view = OrderView.from_row(order_row())
    body = order_event(view, change="placed", source="poll", received_at=FIXED_NOW, tag_labels={}, previous_event_id="")
    with svc.store.write() as con:
        svc.store.insert_event(con, body, order_id=ORDER_ID, consumers=[], order_key=ORDER_ID, now=FIXED_NOW)
    assert svc.store.previous(ORDER_ID) is None
    told = []
    svc.on_event(lambda: told.append(1))
    assert svc.ingest_order(order_row(), source="poll") is None
    assert svc.store.previous(ORDER_ID).event_id == body["event_id"]
    assert svc.store.last_seq() == 1 and told == []
    seq = svc.ingest_order(filled_row(), source="poll")  # the next change chains to that event
    assert svc.store.event(seq)["change"] == "filled"
