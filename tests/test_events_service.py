"""The event service's ingest path: what becomes an event, who it is delivered to, and the counters."""

from __future__ import annotations

import datetime as dt

import pytest

from conftest import FIXED_NOW
from events_helpers import ORDER_ID, PY, Clock, filled_row, make_service, order_row
from kite_connect_mcp.errors import ConfigError
from kite_connect_mcp.events.model import digest
from kite_connect_mcp.events.store import StoreError

NOTIFY = {
    "name": "notify",
    "command": [PY, "-c", "pass"],
    "match": {"kinds": ["order", "session", "router", "selftest"]},
}
FILLS = {"name": "fills", "command": [PY, "-c", "pass"], "match": {"changes": ["filled"], "tags": ["mytag"]}}


@pytest.fixture
def svc(runtime_factory, tmp_path):
    service, _ = make_service(runtime_factory, tmp_path, consumers=[NOTIFY, FILLS], tag_labels={"mytag": "my strategy"})
    yield service
    service.store.close()


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


def test_ring_and_refused(svc):
    svc.ring(ORDER_ID)
    assert svc.store.doorbells() == [(ORDER_ID, 0)]
    assert svc.store.counters()["postback_accepted"]["count"] == 1
    svc.refused("bad checksum")
    assert svc.store.counters()["postback_refused"] == {
        "count": 1,
        "last_at": svc.store.counters()["postback_refused"]["last_at"],
        "note": "bad checksum",
    }


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
    svc.refused("anything")  # never raises


def test_a_service_needs_a_store(runtime_factory, tmp_path):
    with pytest.raises(ConfigError, match="KITE_MCP_EVENTS_DB"):
        make_service(runtime_factory, tmp_path, KITE_MCP_EVENTS_DB=None)


def test_the_account_pin(runtime_factory, tmp_path):
    open_svc, _ = make_service(runtime_factory, tmp_path / "a")
    assert open_svc.user_allowed("ZZ9999") and open_svc.user_allowed(None)
    pinned, _ = make_service(runtime_factory, tmp_path / "b", KITE_MCP_ALLOWED_USER_ID="ab1234")
    assert pinned.user_allowed("AB1234") and pinned.user_allowed(" ab1234 ")
    assert not pinned.user_allowed("ZZ9999") and not pinned.user_allowed(None) and not pinned.user_allowed(5)
    open_svc.store.close()
    pinned.store.close()


def test_the_status(runtime_factory, tmp_path):
    clock = Clock()
    svc, _ = make_service(runtime_factory, tmp_path, consumers=[NOTIFY], clock=clock)
    svc.ingest_order(order_row(), source="poll")
    out = svc.status()
    assert out["consumers"]["notify"]["pending"] == 1
    assert out["configured_consumers"] == ["notify"]
    assert out["store"] == str(svc.store.path)
    assert digest("x") not in str(out)
    assert out["poll"] == {"interval_s": 60, "window": "09:00-16:00", "days": [0, 1, 2, 3, 4], "in_window_now": True}
    assert "warning" not in out
    svc.store.close()


def test_the_status_lists_a_configured_consumer_with_no_deliveries_yet(runtime_factory, tmp_path):
    svc, _ = make_service(runtime_factory, tmp_path, consumers=[NOTIFY, FILLS])
    svc.ingest_order(order_row(), source="poll")  # for notify only
    out = svc.status()
    assert out["configured_consumers"] == ["notify", "fills"]
    assert out["consumers"]["fills"]["pending"] == 0 and out["consumers"]["fills"]["last_error"] is None
    svc.store.close()


def test_the_status_warns_when_no_poll_succeeded_after_the_close(runtime_factory, tmp_path):
    clock = Clock(FIXED_NOW.replace(hour=16, minute=30))  # a Wednesday, after the poll window
    svc, _ = make_service(runtime_factory, tmp_path, clock=clock)
    out = svc.status()
    assert not out["poll"]["in_window_now"]
    assert "no successful poll after the close" in out["warning"]
    svc.store.set_meta("after_close_ok", "2026-11-04")
    assert "warning" not in svc.status()
    clock.now = dt.datetime(2026, 11, 7, 17, 0, tzinfo=FIXED_NOW.tzinfo)  # a Saturday: no poll is due
    assert "warning" not in svc.status()
    svc.store.close()
