"""The order-book poll: the window and days, the baseline, a rejected token, the polls after a login and the close."""

from __future__ import annotations

import asyncio
import datetime as dt

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


async def test_waiting_doorbells_go_first(setup):
    svc, poller, kite, _, _ = setup
    svc.store.set_meta("baseline_done", "x")
    kite.on("GET", f"/orders/{ORDER_ID}", [filled_row()])
    kite.on("GET", "/orders", [filled_row()])
    svc.ring(ORDER_ID)
    await poller.poll("login")
    (event,) = svc.store.events()
    assert event["source"] == "postback"  # the doorbell's re-read came first; the poll's same state was a duplicate


def test_due(setup, tmp_path):
    svc, poller, _, clock, rt = setup
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
