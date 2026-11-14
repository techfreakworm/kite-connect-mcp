"""The doorbell: a verified postback only makes the service re-read that order from Kite."""

from __future__ import annotations

import asyncio

import httpx2
import pytest

from conftest import ACCESS_TOKEN, FIXED_NOW
from events_helpers import ORDER_ID, PY, Clock, filled_row, order_row
from kite_connect_mcp.auth.tokenstore import TokenRecord
from kite_connect_mcp.events import service as service_module
from kite_connect_mcp.events._wait import wait_any
from kite_connect_mcp.events.doorbell import MAX_TRIES, DoorbellWorker
from kite_connect_mcp.events.store import StoreError

NOTIFY = {"name": "notify", "command": [PY, "-c", "pass"], "match": {"kinds": ["order", "session"]}}
OTHER_ID = "250101000000002"
# Kite's answer to GET /orders/{id} for an id it does not know (checked against the real API with a read-only probe).
UNKNOWN_ORDER = {"status": 404, "error_type": "GeneralException", "message": "Couldn't find that `order_id`."}


@pytest.fixture
def setup(service_factory, fake_kite):
    svc, _ = service_factory(consumers=[NOTIFY])
    worker = DoorbellWorker(svc)
    svc.doorbells = worker
    yield svc, worker, fake_kite
    assert fake_kite.writes() == []  # the doorbell only ever reads from Kite


async def test_the_order_is_re_read_and_kites_answer_is_stored(setup):
    svc, worker, kite = setup
    kite.on("GET", f"/orders/{ORDER_ID}", [order_row(), filled_row()])
    svc.ring(ORDER_ID)
    assert await worker.process() == 1
    (event,) = svc.store.events()
    assert event["source"] == "postback" and event["change"] == "seen_final"
    assert event["order"]["status"] == "COMPLETE"
    assert svc.store.doorbells() == []
    assert kite.writes() == []


async def test_an_order_kite_does_not_know_is_dropped_and_counted(setup):
    svc, worker, kite = setup
    kite.on("GET", f"/orders/{ORDER_ID}", **UNKNOWN_ORDER)
    svc.ring(ORDER_ID)
    assert await worker.process() == 1
    assert svc.store.events() == []
    assert svc.store.doorbells() == []
    assert svc.store.counters()["doorbell_unknown_order"]["count"] == 1


@pytest.mark.parametrize(
    "answer",
    [
        None,  # unrouted: FakeKite answers 404 InputException "Route not found", as Kite does for a wrong path
        {"status": 404, "body": b"<html><body>Not Found</body></html>", "content_type": "text/html"},  # a proxy
        {"status": 404, "error_type": "GeneralException", "message": "Not found"},
        {"status": 400, "error_type": "InputException", "message": "Couldn't find that `order_id`."},
        {"status": 500, "error_type": "GeneralException", "message": "Couldn't find that `order_id`."},
    ],
)
async def test_only_kites_unknown_order_answer_drops_a_doorbell(setup, answer):
    """Any other 404 (a wrong base URL or route, a proxy's page) is a failed read: dropping on it would lose every
    doorbell while the setup is wrong."""
    svc, worker, kite = setup
    if answer is not None:
        kite.on("GET", f"/orders/{ORDER_ID}", **answer)
    svc.ring(ORDER_ID)
    assert await worker.process() == 0
    assert svc.store.doorbells() == [(ORDER_ID, 1)]
    assert "doorbell_unknown_order" not in svc.store.counters()


async def test_an_empty_history_is_an_unknown_order(setup):
    svc, worker, kite = setup
    kite.on("GET", f"/orders/{ORDER_ID}", [])
    svc.ring(ORDER_ID)
    await worker.process()
    assert svc.store.doorbells() == [] and svc.store.counters()["doorbell_unknown_order"]["count"] == 1


async def test_without_a_token_nothing_is_read_and_the_doorbell_waits(service_factory, tmp_path, fake_kite):
    svc, _ = service_factory(KITE_TOKEN_FILE=str(tmp_path / "none.json"))
    worker = DoorbellWorker(svc)
    svc.ring(ORDER_ID)
    assert await worker.process() == 0
    assert svc.store.doorbells() == [(ORDER_ID, 0)]
    assert fake_kite.requests == []


async def test_a_rejected_token_raises_token_rejected_and_the_doorbell_waits(setup):
    svc, worker, kite = setup
    kite.on("GET", f"/orders/{ORDER_ID}", status=403, error_type="TokenException", message="Invalid session")
    svc.ring(ORDER_ID)
    await worker.process()
    assert svc.store.doorbells() == [(ORDER_ID, 1)]
    (event,) = svc.store.events()
    assert event["kind"] == "session" and event["session"] == "token_rejected"
    assert ACCESS_TOKEN not in str(event)
    assert svc.tokens.current() is None  # the client marked the token rejected


async def test_a_kite_failure_keeps_the_doorbell_for_later(setup):
    svc, worker, kite = setup
    kite.on("GET", f"/orders/{ORDER_ID}", status=500, error_type="GeneralException", message="boom")
    svc.ring(ORDER_ID)
    await worker.process()
    assert svc.store.doorbells() == [(ORDER_ID, 1)]
    assert svc.store.events() == []


async def test_ringing_wakes_the_running_worker(setup):
    svc, worker, kite = setup
    kite.on("GET", f"/orders/{ORDER_ID}", [filled_row()])
    stop = asyncio.Event()
    task = asyncio.create_task(worker.run(stop))
    await asyncio.sleep(0)
    svc.ring(ORDER_ID)
    for _ in range(200):
        if not svc.store.doorbells():
            break
        await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(task, 2)
    assert svc.store.doorbells() == [] and len(svc.store.events()) == 1


async def test_wait_any_returns_on_an_event_or_the_timeout():
    a, b = asyncio.Event(), asyncio.Event()
    loop = asyncio.get_running_loop()
    started = loop.time()
    await wait_any(0.05, a, b)
    assert loop.time() - started >= 0.04
    b.set()
    await asyncio.wait_for(wait_any(5, a, b), 1)


async def test_wait_any_with_no_events_waits_the_timeout():
    loop = asyncio.get_running_loop()
    started = loop.time()
    await asyncio.wait_for(wait_any(0.03), 1)
    assert loop.time() - started >= 0.02


# --- replays, failed reads and rings that arrive while an order is read ---------------------------------------------


def _doorbell_logs(caplog) -> str:
    return "\n".join(r.getMessage() for r in caplog.records if r.name == "kite_connect_mcp.events.doorbell")


async def test_a_replayed_postback_costs_one_read_and_stores_nothing_new(setup):
    svc, worker, kite = setup
    kite.on("GET", f"/orders/{ORDER_ID}", [filled_row()])
    svc.ring(ORDER_ID)
    svc.ring(ORDER_ID)  # rung twice before the worker runs: still one doorbell, one read
    assert await worker.process() == 1
    svc.ring(ORDER_ID)  # the same postback replayed after it was settled
    assert await worker.process() == 1
    assert len(svc.store.events()) == 1
    assert len(kite.calls("GET", f"/orders/{ORDER_ID}")) == 2
    assert svc.store.doorbells() == []


async def test_a_rejected_token_is_not_tried_on_the_other_doorbells(setup):
    svc, worker, kite = setup
    kite.on("GET", f"/orders/{ORDER_ID}", status=403, error_type="TokenException", message="Invalid session")
    svc.ring(ORDER_ID)
    svc.ring("250101000000002")
    assert await worker.process() == 0
    assert len(kite.requests) == 1
    assert svc.store.doorbells() == [(ORDER_ID, 1), ("250101000000002", 0)]


@pytest.mark.parametrize("other", ["not an order id!", OTHER_ID, None])
async def test_an_answer_about_another_order_settles_the_doorbell_without_an_event(setup, caplog, other):
    svc, worker, kite = setup
    kite.on("GET", f"/orders/{ORDER_ID}", [filled_row(order_id=other)])
    svc.ring(ORDER_ID)
    assert await worker.process() == 1
    assert svc.store.events() == [] and svc.store.doorbells() == []
    counters = svc.store.counters()
    assert counters["doorbell_wrong_order"]["count"] == 1  # shown in the status
    assert "doorbell_unknown_order" not in counters
    assert "about another order" in _doorbell_logs(caplog)


async def test_the_logs_never_name_the_rung_order(setup, caplog):
    svc, worker, kite = setup
    kite.on("GET", f"/orders/{ORDER_ID}", status=500, error_type="GeneralException", message="boom")
    svc.ring(ORDER_ID)
    await worker.process()
    logs = _doorbell_logs(caplog)
    assert "could not re-read a rung order" in logs and "boom" in logs
    assert ORDER_ID not in logs and ACCESS_TOKEN not in logs


async def test_the_worker_keeps_running_after_a_failed_round(setup, monkeypatch, caplog):
    svc, worker, kite = setup
    kite.on("GET", f"/orders/{ORDER_ID}", [filled_row()])
    real_doorbells = svc.store.doorbells
    calls = 0

    def failing_once() -> list[tuple[str, int]]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise StoreError("cannot read the event store: disk I/O error")
        return real_doorbells()

    monkeypatch.setattr(svc.store, "doorbells", failing_once)
    stop = asyncio.Event()
    task = asyncio.create_task(worker.run(stop))
    for _ in range(200):
        if calls:
            break
        await asyncio.sleep(0.01)
    assert "the doorbell worker failed" in _doorbell_logs(caplog)
    svc.ring(ORDER_ID)
    for _ in range(200):
        if not real_doorbells():
            break
        await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(task, 2)
    assert real_doorbells() == [] and len(svc.store.events()) == 1


async def test_the_token_checked_is_the_token_sent_and_the_one_reported(setup, monkeypatch):
    """Another process can replace the token file between the worker's check and the read: the read still uses the
    token checked, so the client rejects that one and the session event names it."""
    svc, worker, kite = setup
    kite.on("GET", f"/orders/{ORDER_ID}", status=403, error_type="TokenException", message="Invalid session")
    checked = svc.tokens.current()
    replaced = TokenRecord.new("othertesttoken000000000000000002", FIXED_NOW)
    answers = iter([checked])
    monkeypatch.setattr(svc.tokens, "current", lambda: next(answers, replaced))
    svc.ring(ORDER_ID)
    await worker.process()
    (request,) = kite.requests
    assert request.headers["Authorization"].endswith(f":{ACCESS_TOKEN}")
    assert svc.note_token_rejected(ACCESS_TOKEN) is None  # reported already
    assert svc.note_token_rejected(replaced.access_token) is not None


async def test_a_kite_failure_on_one_rung_order_does_not_hold_up_the_next(setup):
    svc, worker, kite = setup
    kite.on("GET", f"/orders/{ORDER_ID}", status=503, error_type="NetworkException", message="busy")
    kite.on("GET", f"/orders/{OTHER_ID}", [filled_row(order_id=OTHER_ID)])
    svc.ring(ORDER_ID)
    svc.ring(OTHER_ID)
    assert await worker.process() == 1
    assert svc.store.doorbells() == [(ORDER_ID, 1)]
    (event,) = svc.store.events()
    assert event["order"]["order_id"] == OTHER_ID


async def test_a_failure_before_kite_is_reached_keeps_the_doorbells(service_factory, fake_kite, caplog):
    svc, _ = service_factory(KITE_API_KEY=None)  # every read fails with a ConfigError, no request is sent
    worker = DoorbellWorker(svc)
    svc.ring(ORDER_ID)
    svc.ring(OTHER_ID)
    assert await worker.process() == 0
    assert svc.store.doorbells() == [(ORDER_ID, 1), (OTHER_ID, 1)]
    assert fake_kite.requests == []
    assert "KITE_API_KEY" in _doorbell_logs(caplog)


@pytest.mark.parametrize("answer", [{"weird": 1}, "text", None, [1, 2], [filled_row(), "junk"]])
async def test_an_answer_that_is_not_an_order_history_keeps_the_doorbell(setup, caplog, answer):
    """Only an empty history says the order is unknown; a malformed answer says nothing about it."""
    svc, worker, kite = setup
    kite.on("GET", f"/orders/{ORDER_ID}", answer)
    svc.ring(ORDER_ID)
    assert await worker.process() == 0
    assert svc.store.doorbells() == [(ORDER_ID, 1)] and svc.store.events() == []
    assert "doorbell_unknown_order" not in svc.store.counters()
    assert "not an order history" in _doorbell_logs(caplog)


async def test_a_bug_with_one_rung_order_keeps_it_and_lets_the_others_settle(setup, monkeypatch, caplog):
    svc, worker, kite = setup
    kite.on("GET", f"/orders/{ORDER_ID}", [filled_row()])
    kite.on("GET", f"/orders/{OTHER_ID}", [filled_row(order_id=OTHER_ID)])
    real = service_module.order_event

    def broken(view, **kwargs):
        if view.order_id == ORDER_ID:
            raise ValueError("unknown change")
        return real(view, **kwargs)

    monkeypatch.setattr(service_module, "order_event", broken)
    svc.ring(ORDER_ID)
    svc.ring(OTHER_ID)
    assert await worker.process() == 1
    assert svc.store.doorbells() == [(ORDER_ID, 1)]
    (event,) = svc.store.events()
    assert event["order"]["order_id"] == OTHER_ID
    logs = _doorbell_logs(caplog)
    assert "could not settle a rung order" in logs and ORDER_ID not in logs


async def test_a_store_that_cannot_write_ends_the_round(setup, monkeypatch):
    svc, worker, kite = setup
    kite.on("GET", f"/orders/{ORDER_ID}", [filled_row()])
    kite.on("GET", f"/orders/{OTHER_ID}", [filled_row(order_id=OTHER_ID)])

    def failing(*args, **kwargs):
        raise StoreError("cannot write the event store: disk I/O error")

    monkeypatch.setattr(svc, "ingest_order", failing)
    svc.ring(ORDER_ID)
    svc.ring(OTHER_ID)
    with pytest.raises(StoreError):
        await worker.process()
    assert svc.store.doorbells() == [(ORDER_ID, 0), (OTHER_ID, 0)]  # kept, and not counted as tried
    assert len(kite.requests) == 1


@pytest.fixture
def timed(service_factory, fake_kite):
    """A service whose clock the test moves, and its doorbell worker."""
    clock = Clock()
    svc, _ = service_factory(consumers=[NOTIFY], clock=clock)
    yield svc, svc.doorbells, fake_kite, clock
    assert fake_kite.writes() == []


async def test_a_doorbell_that_failed_max_tries_times_is_given_up(timed, caplog):
    svc, worker, kite, clock = timed
    kite.on("GET", f"/orders/{ORDER_ID}", status=500, error_type="GeneralException", message="boom")
    svc.ring(ORDER_ID)
    for _ in range(MAX_TRIES - 1):
        assert await worker.process() == 0
        clock.advance(DoorbellWorker.IDLE_S)
    assert svc.store.doorbells() == [(ORDER_ID, MAX_TRIES - 1)]
    assert await worker.process() == 1
    assert svc.store.doorbells() == [] and svc.store.events() == []
    assert svc.store.counters()["doorbell_gave_up"]["count"] == 1
    assert "doorbell_unknown_order" not in svc.store.counters()
    assert len(kite.calls("GET", f"/orders/{ORDER_ID}")) == MAX_TRIES
    assert "gave up" in _doorbell_logs(caplog)


async def test_a_ring_while_its_order_is_read_keeps_the_doorbell_for_another_read(setup):
    """Kite's answer can predate the fill whose postback lands while the order is read: that ring is not lost with the
    doorbell, so the next round reads the order again and stores the fill (the clock does not move: rings are told
    apart without it)."""
    svc, worker, kite = setup
    answers = iter([[order_row()], [filled_row()]])

    def answer(request: httpx2.Request) -> httpx2.Response:
        rows = next(answers)
        if rows[-1]["status"] == "OPEN":
            svc.ring(ORDER_ID)  # the fill's postback, while Kite still answers OPEN
        return httpx2.Response(200, json={"status": "success", "data": rows})

    kite.on("GET", f"/orders/{ORDER_ID}", handler=answer)
    svc.ring(ORDER_ID)
    assert await worker.process() == 0  # the open order is stored, and the doorbell stays for the new ring
    assert svc.store.doorbells() == [(ORDER_ID, 0)]
    assert await worker.process() == 1
    assert [e["order"]["status"] for e in svc.store.events()] == ["OPEN", "COMPLETE"]
    assert svc.store.doorbells() == []


async def test_a_ring_while_its_order_is_read_keeps_a_doorbell_that_would_be_dropped(setup):
    svc, worker, kite = setup

    def unknown(request: httpx2.Request) -> httpx2.Response:
        svc.ring(ORDER_ID)
        return httpx2.Response(200, json={"status": "success", "data": []})

    kite.on("GET", f"/orders/{ORDER_ID}", handler=unknown)
    svc.ring(ORDER_ID)
    assert await worker.process() == 0
    assert svc.store.doorbells() == [(ORDER_ID, 0)] and "doorbell_unknown_order" not in svc.store.counters()


async def test_a_doorbell_counts_at_most_one_failed_read_every_idle_s(timed):
    """Every ring starts a round, which reads every waiting doorbell again: during a Kite outage on a busy day the tries
    would otherwise be used up in minutes."""
    svc, worker, kite, clock = timed
    kite.on("GET", f"/orders/{ORDER_ID}", status=503, error_type="NetworkException", message="busy")
    svc.ring(ORDER_ID)
    for _ in range(5):
        svc.ring(ORDER_ID)
        assert await worker.process() == 0
        clock.advance(DoorbellWorker.IDLE_S / 10)
    assert svc.store.doorbells() == [(ORDER_ID, 1)]
    assert len(kite.calls("GET", f"/orders/{ORDER_ID}")) == 5  # each round still reads it
    clock.advance(DoorbellWorker.IDLE_S)
    await worker.process()
    assert svc.store.doorbells() == [(ORDER_ID, 2)]
    clock.advance(-3600)  # a clock that went back holds nothing
    await worker.process()
    assert svc.store.doorbells() == [(ORDER_ID, 3)]


async def test_a_give_up_on_a_rejected_token_counts_as_settled(setup):
    svc, worker, kite = setup
    kite.on("GET", f"/orders/{ORDER_ID}", status=403, error_type="TokenException", message="Invalid session")
    svc.ring(ORDER_ID)
    svc.ring(OTHER_ID)
    with svc.store.write() as con:
        con.execute("UPDATE doorbells SET tries = ? WHERE order_id = ?", (MAX_TRIES - 1, ORDER_ID))
    assert await worker.process() == 1
    assert svc.store.doorbells() == [(OTHER_ID, 0)]  # the round still ended on the rejected token
    assert svc.store.counters()["doorbell_gave_up"]["count"] == 1
    assert len(kite.requests) == 1
