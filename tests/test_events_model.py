"""The event model: rows to views, the change a read reports, the one-line text, and the event bodies."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from conftest import FIXED_NOW
from events_helpers import ORDER_ID, filled_row, order_row
from kite_connect_mcp.events.model import (
    CHANGES,
    OrderView,
    Previous,
    canonical,
    clean_error_text,
    clean_reason,
    judge,
    normalize_status,
    order_event,
    order_key,
    router_event,
    selftest_event,
    session_event,
    text_for,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("COMPLETE", "COMPLETE"),
        ("REJECTED", "REJECTED"),
        ("CANCELLED", "CANCELLED"),
        ("CANCEL", "CANCELLED"),
        ("cancelled", "CANCELLED"),
        ("OPEN", "OPEN"),
        ("UPDATE", "OPEN"),
        ("TRIGGER PENDING", "OPEN"),
        ("AMO REQ RECEIVED", "OPEN"),
        ("VALIDATION PENDING", "OPEN"),
        ("MODIFIED", "OPEN"),
        ("", "OPEN"),
        (None, "OPEN"),
    ],
)
def test_normalize_status(raw, expected):
    assert normalize_status(raw) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (5, "5"),
        (5.0, "5"),
        ("5.00", "5"),
        (1234.5, "1234.5"),
        (100, "100"),
        (0, "0"),
        (0.0, "0"),
        (-0.0, "0"),
        (None, ""),
        (True, ""),
        ("abc", ""),
        (float("nan"), ""),
        (float("inf"), ""),
        ("1e30", "1" + "0" * 30),
        ("1e31", ""),  # no price or quantity is this large: a number this far out is not one
        ("1e-31", ""),
        ("1e999999", ""),
        ("1e9999999", ""),  # beyond Decimal's exponent range: no Overflow escapes
    ],
)
def test_canonical(value, expected):
    assert canonical(value) == expected


def test_a_view_keeps_the_documented_fields_and_drops_the_identifiers():
    view = OrderView.from_row(order_row())
    body = view.as_dict()
    assert body["order_id"] == ORDER_ID
    assert body["status"] == "OPEN" and body["status_raw"] == "OPEN"
    assert body["tag"] == "mytag" and body["gtt"] is False
    assert body["quantity"] == 25 and body["price"] == 1234.5
    text = json.dumps(body)
    for gone in ("AB1234", "placed_by", "user_id", "app_id", "checksum", "guid", "meta", "instrument_token"):
        assert gone not in text


def test_a_gtt_order_is_marked():
    assert OrderView.from_row(order_row(meta={"gtt": {"id": 111}})).gtt is True
    assert OrderView.from_row(order_row(meta={"other": 1})).gtt is False
    assert OrderView.from_row(order_row(meta=None)).gtt is False


def test_the_tag_falls_back_to_the_tags_list():
    assert OrderView.from_row(order_row(tag=None, tags=["other"])).tag == "other"
    assert OrderView.from_row(order_row(tag=None, tags=[])).tag is None


@pytest.mark.parametrize("order_id", [None, "", "bad id", "x" * 65, 123])
def test_a_row_without_a_valid_order_id_is_refused(order_id):
    with pytest.raises(ValueError, match="order_id"):
        OrderView.from_row(order_row(order_id=order_id))


def test_the_reason_loses_account_ids_and_links_and_is_short():
    row = order_row(
        status="REJECTED", status_message="RMS:Margin Exceeds for entity account-AB1234 see https://x.example.com/a"
    )
    reason = clean_reason(row["status_message"], row)
    assert reason is not None
    assert "AB1234" not in reason and "https://" not in reason and "example.com" not in reason
    assert len(clean_reason("x" * 500, row) or "") <= 160


def test_int_and_float_give_the_same_state():
    a = OrderView.from_row(order_row(quantity=25, price=1234.5, filled_quantity=0))
    b = OrderView.from_row(order_row(quantity=25.0, price="1234.50", filled_quantity=0.0))
    assert a.state() == b.state()
    assert a.state_id() == b.state_id()


def test_the_state_id_depends_on_the_order_and_the_state():
    base = OrderView.from_row(order_row())
    assert OrderView.from_row(order_row(order_id="250101000000002")).state_id() != base.state_id()
    assert OrderView.from_row(order_row(price=1230)).state_id() != base.state_id()
    assert OrderView.from_row(order_row(status="UPDATE")).state_id() == base.state_id()  # both OPEN


def previous_of(row, event_id: str = "") -> Previous:
    view = OrderView.from_row(row)
    return Previous(view.state(), view.final, view.filled, view.exchange_update_timestamp, event_id)


def test_judge_the_first_sight():
    assert judge(OrderView.from_row(order_row()), None) == "placed"
    assert judge(OrderView.from_row(filled_row()), None) == "seen_final"


def test_judge_a_fill_a_partial_fill_and_a_modification():
    open_ = previous_of(order_row())
    assert judge(OrderView.from_row(filled_row()), open_) == "filled"
    partial = order_row(filled_quantity=10, pending_quantity=15, average_price=1234.5)
    assert judge(OrderView.from_row(partial), open_) == "partial_fill"
    assert judge(OrderView.from_row(order_row(price=1230)), open_) == "modified"


def test_judge_the_other_final_statuses():
    open_ = previous_of(order_row())
    assert judge(OrderView.from_row(order_row(status="CANCELLED")), open_) == "cancelled"
    assert judge(OrderView.from_row(order_row(status="REJECTED")), open_) == "rejected"


def test_judge_drops_duplicates_stale_reads_and_anything_after_a_final_status():
    open_ = previous_of(order_row())
    assert judge(OrderView.from_row(order_row(status="UPDATE")), open_) is None  # the same state
    partial = previous_of(order_row(filled_quantity=10, pending_quantity=15))
    assert judge(OrderView.from_row(order_row(filled_quantity=0)), partial) is None  # the fill went backwards
    late = order_row(price=1230, exchange_update_timestamp="2026-11-04 09:29:00")
    assert judge(OrderView.from_row(late), open_) is None  # an older exchange update
    done = previous_of(filled_row())
    assert judge(OrderView.from_row(order_row(status="CANCELLED")), done) is None


def test_the_text_of_each_change():
    open_view = OrderView.from_row(order_row())
    assert text_for(open_view, "placed") == "BUY 25 ABCD (NSE) placed: LIMIT 1234.50 [mytag]"
    assert (
        text_for(OrderView.from_row(order_row(price=1230)), "modified")
        == "BUY 25 ABCD (NSE) modified: LIMIT 1230.00 [mytag]"
    )
    partial = OrderView.from_row(order_row(filled_quantity=10, average_price=1234.4))
    assert text_for(partial, "partial_fill") == "BUY 25 ABCD (NSE) partly filled: 10 of 25 at 1234.40 [mytag]"
    done = OrderView.from_row(filled_row())
    assert text_for(done, "filled") == "BUY 25 ABCD (NSE) filled at 1234.50: complete [mytag]"
    assert text_for(done, "seen_final") == "BUY 25 ABCD (NSE) filled at 1234.50: complete [mytag]"
    cancelled = OrderView.from_row(order_row(status="CANCELLED", filled_quantity=10, average_price=1234.5))
    assert text_for(cancelled, "cancelled") == "BUY 25 ABCD (NSE) cancelled after 10 of 25 filled at 1234.50 [mytag]"
    rejected = OrderView.from_row(order_row(status="REJECTED", status_message="Insufficient funds"))
    assert text_for(rejected, "rejected") == "BUY 25 ABCD (NSE) rejected: Insufficient funds [mytag]"


def test_the_text_of_other_order_types_a_gtt_and_a_label():
    sl = OrderView.from_row(order_row(order_type="SL", price=1200, trigger_price=1201, tag=None, tags=[]))
    assert text_for(sl, "placed") == "BUY 25 ABCD (NSE) placed: SL 1200.00, trigger 1201.00"
    slm = OrderView.from_row(order_row(order_type="SL-M", trigger_price=1201, tag=None, tags=[]))
    assert text_for(slm, "placed") == "BUY 25 ABCD (NSE) placed: SL-M, trigger 1201.00"
    market = OrderView.from_row(order_row(order_type="MARKET", price=0, tag=None, tags=[]))
    assert text_for(market, "placed") == "BUY 25 ABCD (NSE) placed: MARKET"
    gtt = OrderView.from_row(filled_row(transaction_type="SELL", meta={"gtt": {"id": 1}}))
    assert (
        text_for(gtt, "filled", {"mytag": "my strategy"})
        == "SELL 25 ABCD (NSE) (GTT) filled at 1234.50: complete [my strategy]"
    )
    fx = OrderView.from_row(filled_row(average_price=83.1234, exchange="CDS", tradingsymbol="USDINR"))
    assert "filled at 83.1234" in text_for(fx, "filled")


def test_every_change_has_a_text():
    view = OrderView.from_row(filled_row())
    for change in CHANGES:
        assert text_for(view, change)


def test_the_order_event_body():
    view = OrderView.from_row(filled_row())
    body = order_event(
        view, change="filled", source="postback", received_at=FIXED_NOW, tag_labels={}, previous_event_id=""
    )
    assert body["kind"] == "order" and body["source"] == "postback" and body["change"] == "filled"
    assert body["state_id"] == view.state_id()
    assert len(body["event_id"]) == 32 and int(body["event_id"], 16) >= 0
    assert body["received_at"] == "2026-11-04T10:00:00+05:30"
    assert body["order"]["order_id"] == ORDER_ID
    assert order_key(body) == ORDER_ID
    assert "AB1234" not in json.dumps(body)


def test_the_previous_event_id_cannot_be_left_out():
    """Left out, it would give a state the order goes back to the id of its earlier event: dropped as stored already."""
    view = OrderView.from_row(order_row())
    with pytest.raises(TypeError, match="previous_event_id"):
        order_event(view, change="placed", source="poll", received_at=FIXED_NOW, tag_labels={})


def test_session_router_and_selftest_bodies():
    session = session_event("token_rejected", seed="abc", received_at=FIXED_NOW)
    assert session["kind"] == "session" and session["session"] == "token_rejected" and session["text"]
    assert session_event("token_rejected", seed="abc", received_at=FIXED_NOW)["event_id"] == session["event_id"]
    assert session_event("token_rejected", seed="other", received_at=FIXED_NOW)["event_id"] != session["event_id"]
    about = {"seq": 7, "event_id": "e1", "kind": "order", "text": "BUY 25 ABCD (NSE) filled at 1234.50: complete"}
    failed = router_event(
        "delivery_failed", consumer="notify", about=about, error="exit 1: boom", seed="t1", received_at=FIXED_NOW
    )
    assert failed["kind"] == "router" and failed["consumer"] == "notify" and failed["about_seq"] == 7
    assert failed["text"] == "Delivery to notify failed: BUY 25 ABCD (NSE) filled at 1234.50: complete (exit 1: boom)"
    delayed = router_event(
        "delivery_delayed", consumer="notify", about=about, error=None, seed="t0", received_at=FIXED_NOW
    )
    assert delayed["text"].startswith("Delivery to notify is delayed")
    assert order_key(failed) == failed["event_id"]
    test = selftest_event("selftest-0a1b2c3d4e5f", received_at=FIXED_NOW)
    assert test["kind"] == "selftest" and test["order_id"] == "selftest-0a1b2c3d4e5f"
    assert order_key(test) == test["event_id"]


def test_the_filled_quantity_is_a_decimal():
    assert OrderView.from_row(order_row(filled_quantity=None)).filled == Decimal(0)


def test_the_reason_loses_any_account_id_and_bare_links():
    row = order_row(user_id=None, placed_by=None)
    reason = clean_reason("RMS:Blocked for entity account-XY9876, see www.example.com/help", row)
    assert reason == "RMS:Blocked for entity account-[account], see [link]"


def test_seen_final_names_the_final_status():
    cancelled = OrderView.from_row(order_row(status="CANCELLED", tag=None, tags=[]))
    assert text_for(cancelled, "seen_final") == "BUY 25 ABCD (NSE) cancelled"
    rejected = OrderView.from_row(order_row(status="REJECTED", status_message="Insufficient funds", tag=None, tags=[]))
    assert text_for(rejected, "seen_final") == "BUY 25 ABCD (NSE) rejected: Insufficient funds"


def test_an_unknown_change_or_router_event_is_refused():
    with pytest.raises(ValueError, match="unknown change"):
        text_for(OrderView.from_row(order_row()), "exploded")
    with pytest.raises(ValueError, match="unknown router event"):
        router_event("delivery_lost", consumer="notify", about={}, error=None, seed="t", received_at=FIXED_NOW)


def test_an_unreadable_exchange_time_does_not_drop_a_change():
    open_ = previous_of(order_row())
    garbled = order_row(price=1230, exchange_update_timestamp="not a time")
    assert judge(OrderView.from_row(garbled), open_) == "modified"


def test_a_long_delivery_error_is_clipped():
    about = {"seq": 1, "event_id": "e1", "kind": "order", "text": "BUY 25 ABCD (NSE) placed: MARKET"}
    failed = router_event(
        "delivery_failed", consumer="notify", about=about, error="x" * 500, seed="t", received_at=FIXED_NOW
    )
    assert len(failed["error"]) == 200
    assert failed["text"].endswith("x" * 200 + ")")


def test_judge_the_first_sight_of_an_order_that_has_a_fill_already():
    partial = order_row(filled_quantity=10, pending_quantity=15, average_price=1234.5)
    assert judge(OrderView.from_row(partial), None) == "partial_fill"


def _order(row, change, previous_event_id="", source="poll"):
    view = OrderView.from_row(row)
    return order_event(
        view, change=change, source=source, received_at=FIXED_NOW, tag_labels={}, previous_event_id=previous_event_id
    )


def test_event_ids_chain_so_a_return_to_an_earlier_state_is_a_new_event():
    placed = _order(order_row(), "placed")
    modified = _order(order_row(price=1230), "modified", placed["event_id"])
    back = _order(order_row(), "modified", modified["event_id"])
    again = _order(order_row(price=1230), "modified", back["event_id"])
    assert len({e["event_id"] for e in (placed, modified, back, again)}) == 4
    assert back["state_id"] == placed["state_id"] and again["state_id"] == modified["state_id"]


def test_a_postback_and_a_poll_of_one_change_share_the_event_id():
    by_postback = _order(filled_row(), "filled", "e0", source="postback")
    by_poll = _order(filled_row(), "filled", "e0", source="poll")
    assert by_postback["event_id"] == by_poll["event_id"]
    assert _order(filled_row(), "filled", "e1")["event_id"] != by_poll["event_id"]  # judged against another state


def test_an_order_event_needs_a_known_change_and_source():
    with pytest.raises(ValueError, match="change"):
        _order(order_row(), "exploded")
    with pytest.raises(ValueError, match="source"):
        _order(order_row(), "placed", source="service")


def test_seen_final_needs_a_final_status():
    with pytest.raises(ValueError, match="seen_final needs a final status"):
        text_for(OrderView.from_row(order_row()), "seen_final")


def test_an_unknown_session_event_is_refused():
    with pytest.raises(ValueError, match="unknown session event"):
        session_event("token_lost", seed="s", received_at=FIXED_NOW)


@pytest.mark.parametrize("order_id", ["250101000000001", "selftest-", "selftest-bad id", "selftest-" + "a" * 56])
def test_a_selftest_event_needs_a_selftest_order_id(order_id):
    with pytest.raises(ValueError, match="self-test order id"):
        selftest_event(order_id, received_at=FIXED_NOW)


def test_a_delivery_error_is_one_line_without_links():
    about = {"seq": 1, "event_id": "e1", "kind": "order", "text": "BUY 25 ABCD (NSE) placed: MARKET"}
    error = "exit 1: curl failed\nhttps://hooks.example.com/T000/secret\n\tretrying  later "
    failed = router_event(
        "delivery_failed", consumer="notify", about=about, error=error, seed="t", received_at=FIXED_NOW
    )
    assert failed["error"] == "exit 1: curl failed [link] retrying later"
    assert (
        failed["text"]
        == "Delivery to notify failed: BUY 25 ABCD (NSE) placed: MARKET (exit 1: curl failed [link] retrying later)"
    )
    assert "example.com" not in json.dumps(failed)


def test_a_link_is_removed_before_the_error_is_clipped():
    about = {"seq": 1, "event_id": "e1", "kind": "order", "text": "BUY 25 ABCD (NSE) placed: MARKET"}
    error = "x" * 190 + " https://hooks.example.com/T000/secret"
    failed = router_event(
        "delivery_failed", consumer="notify", about=about, error=error, seed="t", received_at=FIXED_NOW
    )
    assert failed["error"] == "x" * 190 + " [link]"


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        (
            "exit 1: curl failed\nhttps://hooks.example.com/T000/secret\n\tretrying  later ",
            "exit 1: curl failed [link] retrying later",
        ),
        (
            "\x1b[31mERROR\x1b[0m: post\tfailed\r\nsee https://hooks.example.com/T000/secret\x07 now",
            "ERROR: post failed see [link] now",
        ),
        ("a\x00b\u202ec \u2028 d\x1b", "abc d"),  # NUL, a bidi override, a line separator, a lone escape
        (
            "https://hooks.example.com/T0\x1b[0m00/secret and www.example.com/x",
            "[link] and [link]",
        ),  # an escape inside a link
        (" \n\t ", ""),
    ],
)
def test_clean_error_text_is_one_printable_line_without_links(raw, clean):
    assert clean_error_text(raw) == clean
    assert clean_error_text(clean) == clean  # cleaning again changes nothing


def test_a_blank_delivery_error_is_none():
    about = {"seq": 1, "event_id": "e1", "kind": "order", "text": "BUY 25 ABCD (NSE) placed: MARKET"}
    failed = router_event(
        "delivery_failed", consumer="notify", about=about, error=" \n ", seed="t", received_at=FIXED_NOW
    )
    assert failed["error"] is None
    assert failed["text"] == "Delivery to notify failed: BUY 25 ABCD (NSE) placed: MARKET"


def test_a_number_out_of_range_does_not_break_a_view():
    view = OrderView.from_row(order_row(quantity="1e9999999", price="1e999999"))
    assert view.quantity is None and view.price is None
