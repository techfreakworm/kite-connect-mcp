"""The order-book poll: the window and days, the baseline, a rejected token, the polls after a login and the close."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging

import pytest

from conftest import FIXED_NOW, write_token_file
from events_helpers import ORDER_ID, PY, Clock, filled_row, make_service, order_row
from kite_connect_mcp.events.doorbell import DoorbellWorker
from kite_connect_mcp.events.poll import Poller
from kite_connect_mcp.events.store import StoreError, iso
from kite_connect_mcp.timeutil import IST

NOTIFY = {"name": "notify", "command": [PY, "-c", "pass"], "match": {"kinds": ["order", "session"]}}
WED = FIXED_NOW  # Wednesday 4 Nov 2026, 10:00 IST


def at(day: int, hour: int, minute: int = 0, second: int = 0) -> dt.datetime:
    """A moment of November 2026 in IST: the 4th is a Wednesday, the 7th a Saturday."""
    return dt.datetime(2026, 11, day, hour, minute, second, tzinfo=IST)


@pytest.fixture
def setup(runtime_factory, tmp_path, fake_kite):
    clock = Clock(WED)
    svc, rt = make_service(runtime_factory, tmp_path, consumers=[NOTIFY], clock=clock)
    svc.doorbells = DoorbellWorker(svc)
    poller = Poller(svc)
    svc.poller = poller
    yield svc, poller, fake_kite, clock, rt
    svc.store.close()


def test_the_window_and_the_days(setup):
    _, poller, _, _, _ = setup
    assert poller.in_window(at(4, 9, 0)) and poller.in_window(at(4, 15, 59))
    assert not poller.in_window(at(4, 8, 59)) and not poller.in_window(at(4, 16, 0))
    assert not poller.in_window(at(7, 10, 0))  # a Saturday


async def test_the_first_poll_ever_is_a_silent_baseline(setup):
    svc, poller, kite, _, _ = setup
    kite.on("GET", "/orders", [order_row(), filled_row(order_id="250101000000002")])
    assert await poller.poll("startup") is True
    assert svc.store.events() == []
    assert svc.store.previous(ORDER_ID) is not None
    assert svc.store.meta("baseline_done")
    kite.on("GET", "/orders", [filled_row(), filled_row(order_id="250101000000002")])
    assert await poller.poll("window") is True
    (event,) = svc.store.events()
    assert event["source"] == "poll" and event["change"] == "filled" and event["order"]["order_id"] == ORDER_ID
    assert svc.store.counters()["poll_ok"]["count"] == 2
    assert kite.writes() == []


async def test_a_rejected_token_raises_token_rejected_once(setup):
    svc, poller, kite, _, _ = setup
    kite.on("GET", "/orders", status=403, error_type="TokenException", message="Invalid session")
    assert await poller.poll("window") is False
    assert await poller.poll("window") is False  # the token is now rejected: nothing is even sent
    assert [e["session"] for e in svc.store.events()] == ["token_rejected"]
    assert svc.store.counters()["poll_failed"]["note"] == "Kite rejected the access token"
    assert len(kite.calls("GET", "/orders")) == 1


async def test_without_a_token_the_poll_skips_quietly(runtime_factory, tmp_path, fake_kite):
    svc, _ = make_service(runtime_factory, tmp_path, KITE_TOKEN_FILE=str(tmp_path / "none.json"))
    assert await Poller(svc).poll("window") is False
    assert fake_kite.requests == [] and "poll_failed" not in svc.store.counters()
    svc.store.close()


async def test_other_failures_are_counted_with_a_short_note(setup):
    svc, poller, kite, _, _ = setup
    kite.on("GET", "/orders", status=500, error_type="GeneralException", message="boom")
    assert await poller.poll("window") is False
    assert "boom" in svc.store.counters()["poll_failed"]["note"]


@pytest.mark.parametrize("reason", ["startup", "login"])
async def test_waiting_doorbells_go_first(setup, reason):
    svc, poller, kite, _, _ = setup
    svc.store.set_meta("baseline_done", "x")
    kite.on("GET", f"/orders/{ORDER_ID}", [filled_row()])
    kite.on("GET", "/orders", [filled_row()])
    svc.ring(ORDER_ID)
    await poller.poll(reason)
    (event,) = svc.store.events()
    assert event["source"] == "postback"  # the doorbell's re-read came first; the poll's same state was a duplicate


@pytest.mark.parametrize("reason", ["window", "after_close"])
async def test_the_other_polls_leave_the_doorbells_to_their_worker(setup, reason):
    svc, poller, kite, _, _ = setup
    svc.store.set_meta("baseline_done", iso(WED))
    kite.on("GET", f"/orders/{ORDER_ID}", [filled_row()])
    kite.on("GET", "/orders", [filled_row()])
    svc.ring(ORDER_ID)
    assert await poller.poll(reason) is True
    assert kite.calls("GET", f"/orders/{ORDER_ID}") == []
    (event,) = svc.store.events()
    assert event["source"] == "poll"
    assert [order_id for order_id, _ in svc.store.doorbells()] == [ORDER_ID]  # still waiting for the worker's round


async def test_a_token_the_doorbells_found_rejected_is_never_sent_by_the_poll(setup):
    svc, poller, kite, _, _ = setup
    kite.on("GET", f"/orders/{ORDER_ID}", status=403, error_type="TokenException", message="Invalid session")
    kite.on("GET", "/orders", [filled_row()])
    svc.ring(ORDER_ID)
    assert await poller.poll("login") is False
    assert len(kite.calls("GET", f"/orders/{ORDER_ID}")) == 1
    assert kite.calls("GET", "/orders") == []
    assert [e["session"] for e in svc.store.events()] == ["token_rejected"]
    assert "poll_failed" not in svc.store.counters()  # no token left to poll with: skipped, like a poll without one
    assert kite.writes() == []


def test_due(setup):
    _, poller, _, _, rt = setup
    poller.prime()  # what run() does at start: remember the current token, poll now
    assert poller.due(at(4, 10, 0)) == "window"  # nothing polled yet
    poller.mark_polled(at(4, 10, 0))
    assert poller.due(at(4, 10, 0)) is None
    assert poller.due(at(4, 10, 1)) == "window"  # 60 s later
    poller.mark_polled(at(4, 10, 1))
    assert poller.due(at(4, 8, 0)) is None  # before the window
    write_token_file(rt.settings.token_file, token="a-brand-new-token-0000000000000002", logged_in_at=FIXED_NOW)
    assert poller.due(at(4, 10, 1)) == "login"  # a new token: poll at once, even mid-interval
    assert poller.due(at(4, 10, 1)) is None


def test_the_poll_after_the_close(setup):
    svc, poller, _, _, _ = setup
    poller.prime()
    poller.mark_polled(at(4, 15, 59, 30))
    assert poller.due(at(4, 16, 0)) is None  # not before the interval has passed
    assert poller.due(at(4, 16, 1)) == "after_close"
    svc.store.set_meta("after_close_ok", "2026-11-04")
    assert poller.due(at(4, 16, 5)) is None
    assert poller.due(at(7, 16, 30)) is None  # Saturday: not a poll day


async def test_a_successful_poll_after_the_close_is_recorded(setup):
    svc, poller, kite, clock, _ = setup
    kite.on("GET", "/orders", [])
    clock.now = at(4, 16, 2)
    await poller.poll("after_close")
    assert svc.store.meta("after_close_ok") == "2026-11-04"


async def test_a_poll_after_the_window_on_a_day_without_polls_is_not_a_close(setup):
    svc, poller, kite, clock, _ = setup
    kite.on("GET", "/orders", [])
    clock.now = at(7, 16, 30)  # Saturday: a login polls, but Saturday has no window to close
    assert await poller.poll("login") is True
    assert svc.store.meta("after_close_ok") is None


def test_no_poll_is_due_once_the_token_is_rejected(setup):
    svc, poller, _, _, _ = setup
    poller.prime()
    assert poller.due(at(4, 10, 0)) == "window"
    svc.tokens.reject(svc.tokens.current().access_token)
    assert poller.due(at(4, 10, 0)) is None


async def test_run_polls_at_start_and_stops(setup):
    svc, poller, kite, _, _ = setup
    kite.on("GET", "/orders", [order_row()])
    poller.TICK_S = 0.01
    stop = asyncio.Event()
    task = asyncio.create_task(poller.run(stop))
    for _ in range(100):
        if kite.calls("GET", "/orders"):
            break
        await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(task, 2)
    assert kite.calls("GET", "/orders")


async def test_an_answer_that_is_not_an_order_list_is_a_failed_poll(setup):
    svc, poller, kite, clock, _ = setup
    kite.on("GET", "/orders", {"orders": []})
    clock.now = at(4, 16, 2)
    assert await poller.poll("after_close") is False
    assert svc.store.counters()["poll_failed"]["note"] == "Kite's answer is not an order list"
    assert "poll_ok" not in svc.store.counters()
    assert svc.store.meta("baseline_done") is None and svc.store.meta("after_close_ok") is None  # nothing was read


async def test_rows_that_are_not_orders_are_skipped(setup):
    svc, poller, kite, _, _ = setup
    svc.store.set_meta("baseline_done", iso(WED))
    kite.on("GET", "/orders", ["junk", None, order_row(order_id=None), order_row(order_id="a/b"), filled_row()])
    assert await poller.poll("window") is True
    (event,) = svc.store.events()
    assert event["order"]["order_id"] == ORDER_ID


async def test_the_status_after_the_first_poll_has_no_warning_about_earlier_days(setup):
    svc, poller, kite, clock, _ = setup
    kite.on("GET", "/orders", [order_row()])
    assert await poller.poll("startup") is True  # Wednesday 10:00: the service watches the book from today
    assert "warning" not in svc.status()  # Tuesday's close is the last one, and the service was not running then
    clock.now = at(4, 16, 30)
    assert "Wed 2026-11-04" in svc.status()["warning"]  # today's close came after the baseline, and was not polled
    assert await poller.poll("after_close") is True
    assert "warning" not in svc.status()


async def test_run_survives_a_tick_that_fails(setup, monkeypatch):
    _, poller, kite, _, _ = setup
    kite.on("GET", "/orders", [])
    ticks = 0

    def due(now: dt.datetime) -> str | None:
        nonlocal ticks
        ticks += 1
        if ticks == 1:
            raise StoreError("cannot read the event store: disk I/O error")
        return "window"

    monkeypatch.setattr(poller, "due", due)
    poller.TICK_S = 0.01
    stop = asyncio.Event()
    task = asyncio.create_task(poller.run(stop))
    for _ in range(200):
        if len(kite.calls("GET", "/orders")) >= 2 or task.done():
            break
        await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(task, 2)
    assert len(kite.calls("GET", "/orders")) >= 2  # the startup poll, then a poll after the tick that failed


async def test_run_survives_a_token_read_that_fails_at_start(setup, monkeypatch):
    svc, poller, kite, _, _ = setup
    kite.on("GET", "/orders", [])
    real_current = svc.tokens.current
    reads = 0

    def current():
        nonlocal reads
        reads += 1
        if reads == 1:  # prime(): a token file that cannot be decoded, say
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
        return real_current()

    monkeypatch.setattr(svc.tokens, "current", current)
    poller.TICK_S = 0.01
    stop = asyncio.Event()
    task = asyncio.create_task(poller.run(stop))
    for _ in range(200):
        if kite.calls("GET", "/orders") or task.done():
            break
        await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(task, 2)
    assert kite.calls("GET", "/orders")  # the startup poll still ran


def _poll_logs(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == "kite_connect_mcp.events.poll"]


async def test_a_failing_poll_is_logged_when_it_starts_failing_and_when_it_recovers(setup, caplog):
    svc, poller, kite, _, _ = setup
    caplog.set_level(logging.INFO, logger="kite_connect_mcp.events.poll")
    kite.on("GET", "/orders", status=500, error_type="GeneralException", message="boom")
    for _ in range(3):
        assert await poller.poll("window") is False
    assert svc.store.counters()["poll_failed"]["count"] == 3  # every failure is counted
    (failed,) = _poll_logs(caplog)  # but logged once
    assert failed.levelno == logging.WARNING and "boom" in failed.getMessage()
    caplog.clear()
    kite.on("GET", "/orders", [])
    assert await poller.poll("window") is True
    assert await poller.poll("window") is True
    (recovered,) = _poll_logs(caplog)
    assert recovered.levelno == logging.INFO and "works again" in recovered.getMessage()
    caplog.clear()
    kite.on("GET", "/orders", status=500, error_type="GeneralException", message="boom")
    assert await poller.poll("window") is False
    assert len(_poll_logs(caplog)) == 1  # a new run of failures is logged again


async def test_a_poll_that_raises_is_logged_once_until_it_recovers(setup, caplog, monkeypatch):
    svc, poller, kite, _, _ = setup
    caplog.set_level(logging.INFO, logger="kite_connect_mcp.events.poll")
    kite.on("GET", "/orders", [])

    def broken(*args, **kwargs):
        raise StoreError("cannot write the event store: disk I/O error")

    monkeypatch.setattr(svc.store, "bump", broken)
    for _ in range(3):
        assert await poller._safe_poll("window") is False
    (failed,) = _poll_logs(caplog)
    assert failed.exc_info is not None
    monkeypatch.undo()
    caplog.clear()
    assert await poller._safe_poll("window") is True
    (recovered,) = _poll_logs(caplog)
    assert "works again" in recovered.getMessage()


async def test_a_poll_that_raises_is_counted_as_failed_with_only_the_kind_of_error(setup, monkeypatch):
    """A bug that breaks the ingest on some row must show in the status, not only leave poll_ok going stale."""
    svc, poller, kite, _, _ = setup
    kite.on("GET", "/orders", [order_row()])

    def broken(*args, **kwargs):
        raise KeyError("a field of the row: 250101000000001")

    monkeypatch.setattr(svc, "ingest_order", broken)
    for _ in range(2):
        assert await poller._safe_poll("window") is False
    failed = svc.store.counters()["poll_failed"]
    assert failed["count"] == 2 and failed["note"] == "an unexpected error: KeyError (see the log)"
    assert "poll_ok" not in svc.store.counters()
    assert kite.writes() == []


async def test_a_poll_whose_failure_cannot_be_counted_still_returns(setup, monkeypatch):
    svc, poller, kite, _, _ = setup
    kite.on("GET", "/orders", [])

    def broken(*args, **kwargs):
        raise StoreError("cannot write the event store: disk I/O error")

    monkeypatch.setattr(svc.store, "bump", broken)
    assert await poller._safe_poll("window") is False  # the count is lost, the poll loop is not


async def test_a_failing_check_logs_its_traceback_once_then_a_short_line_a_minute_at_most(setup, caplog, monkeypatch):
    svc, poller, kite, clock, _ = setup
    caplog.set_level(logging.INFO, logger="kite_connect_mcp.events.poll")
    kite.on("GET", "/orders", [])
    real_due = poller.due

    def failing_due(now: dt.datetime) -> str | None:
        raise StoreError("cannot read the event store: disk I/O error")

    monkeypatch.setattr(poller, "due", failing_due)
    for _ in range(12):  # a minute of ticks, every 5 s
        await poller.tick()
        clock.advance(5)
    (first,) = _poll_logs(caplog)
    assert first.exc_info is not None
    caplog.clear()
    await poller.tick()  # 60 s after the first
    (line,) = _poll_logs(caplog)
    assert line.exc_info is None and "still" in line.getMessage() and "StoreError" in line.getMessage()
    caplog.clear()
    monkeypatch.setattr(poller, "due", real_due)
    clock.advance(5)
    await poller.tick()
    assert kite.calls("GET", "/orders")  # the check works again, and its poll ran
    assert any("works again" in r.getMessage() for r in _poll_logs(caplog))
    caplog.clear()
    monkeypatch.setattr(poller, "due", failing_due)
    await poller.tick()
    (again,) = _poll_logs(caplog)
    assert again.exc_info is not None  # a new run of failures shows its traceback again
