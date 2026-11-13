"""The doorbell: a verified postback only makes the service re-read that order from Kite."""

from __future__ import annotations

import asyncio

import pytest

from conftest import ACCESS_TOKEN
from events_helpers import ORDER_ID, PY, filled_row, make_service, order_row
from kite_connect_mcp.events._wait import wait_any
from kite_connect_mcp.events.doorbell import DoorbellWorker
from kite_connect_mcp.events.store import StoreError

NOTIFY = {"name": "notify", "command": [PY, "-c", "pass"], "match": {"kinds": ["order", "session"]}}


@pytest.fixture
def setup(runtime_factory, tmp_path, fake_kite):
    svc, rt = make_service(runtime_factory, tmp_path, consumers=[NOTIFY])
    worker = DoorbellWorker(svc)
    svc.doorbells = worker
    yield svc, worker, fake_kite
    svc.store.close()
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
    kite.on("GET", f"/orders/{ORDER_ID}", status=400, error_type="InputException", message="Couldn't find that order")
    svc.ring(ORDER_ID)
    svc.ring("250101000000002")  # unrouted: FakeKite answers 404 InputException
    assert await worker.process() == 2
    assert svc.store.events() == []
    assert svc.store.doorbells() == []
    assert svc.store.counters()["doorbell_unknown_order"]["count"] == 2


async def test_an_empty_history_is_an_unknown_order(setup):
    svc, worker, kite = setup
    kite.on("GET", f"/orders/{ORDER_ID}", [])
    svc.ring(ORDER_ID)
    await worker.process()
    assert svc.store.doorbells() == [] and svc.store.counters()["doorbell_unknown_order"]["count"] == 1


async def test_without_a_token_nothing_is_read_and_the_doorbell_waits(runtime_factory, tmp_path, fake_kite):
    svc, _ = make_service(runtime_factory, tmp_path, KITE_TOKEN_FILE=str(tmp_path / "none.json"))
    worker = DoorbellWorker(svc)
    svc.ring(ORDER_ID)
    assert await worker.process() == 0
    assert svc.store.doorbells() == [(ORDER_ID, 0)]
    assert fake_kite.requests == []
    svc.store.close()


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


# --- beyond the plan's tests ----------------------------------------------------------------------------------------


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


async def test_an_answer_without_a_valid_order_id_settles_the_doorbell_without_an_event(setup, caplog):
    svc, worker, kite = setup
    kite.on("GET", f"/orders/{ORDER_ID}", [order_row(order_id="not an order id!")])
    svc.ring(ORDER_ID)
    assert await worker.process() == 1
    assert svc.store.events() == [] and svc.store.doorbells() == []
    assert "doorbell_unknown_order" not in svc.store.counters()
    assert "no valid order id" in _doorbell_logs(caplog)


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
