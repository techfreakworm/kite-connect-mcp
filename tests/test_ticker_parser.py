"""Kite's binary tick format and JSON text frames. The packet builders below use struct.pack so they double as a
description of the wire format (https://kite.trade/docs/connect/v3/websocket/); test_ticker.py reuses them."""

from __future__ import annotations

import datetime as dt
import random
import struct

import pytest

from kite_connect_mcp.ticker.parser import (
    divisor_for,
    parse_binary,
    parse_packet,
    parse_text,
    segment_of,
    split_packets,
)
from kite_connect_mcp.timeutil import IST

NSE, NFO, NSE_CD, BSE, BSE_CD, MCX, INDICES = 1, 2, 3, 4, 6, 7, 9


def token_for(number: int, segment: int) -> int:
    """Kite packs the exchange segment into the low byte of an instrument token."""
    return (number << 8) | segment


INFY = token_for(1594, NSE)  # 408065
NIFTY_50 = token_for(1001, INDICES)  # 256265
USDINR = token_for(7000, NSE_CD)
USDINR_BSE = token_for(8000, BSE_CD)
OPENED = int(dt.datetime(2026, 11, 4, 9, 15, 0, tzinfo=IST).timestamp())


# --- builders: one function per packet kind ------------------------------------------------------------------------


def ltp_packet(token: int, last: int) -> bytes:
    return struct.pack(">Ii", token, last)


def index_packet(
    token: int, *, last: int, high: int, low: int, open_: int, close: int, stamp: int | None = None
) -> bytes:
    """28 bytes (quote); 32 with the exchange timestamp (full). Note high/low/open/close, not open/high/low/close."""
    body = struct.pack(">Iiiiiii", token, last, high, low, open_, close, last - close)
    return body if stamp is None else body + struct.pack(">I", stamp)


def quote_packet(
    token: int,
    *,
    last: int,
    last_qty: int = 0,
    average: int = 0,
    volume: int = 0,
    buy_qty: int = 0,
    sell_qty: int = 0,
    open_: int = 0,
    high: int = 0,
    low: int = 0,
    close: int = 0,
) -> bytes:
    """44 bytes: ten int32 fields after the token."""
    return struct.pack(
        ">IiIiIIIiiii", token, last, last_qty, average, volume, buy_qty, sell_qty, open_, high, low, close
    )


def depth_entries(rows: list[tuple[int, int, int]]) -> bytes:
    """Each entry: int32 quantity, int32 price, int16 orders, 2 bytes of padding (deliberately not zero)."""
    return b"".join(struct.pack(">IiHH", quantity, price, orders, 0xFFFF) for quantity, price, orders in rows)


def full_packet(
    token: int,
    *,
    buy: list[tuple[int, int, int]],
    sell: list[tuple[int, int, int]],
    last_trade: int = 0,
    oi: int = 0,
    oi_high: int = 0,
    oi_low: int = 0,
    stamp: int = 0,
    **quote: int,
) -> bytes:
    """184 bytes: the 44-byte quote, last trade time, OI, OI day high / low, exchange timestamp, 5 bids, 5 offers."""
    head = quote_packet(token, **quote)  # type: ignore[arg-type]
    extra = struct.pack(">IIIII", last_trade, oi, oi_high, oi_low, stamp)
    return head + extra + depth_entries(buy) + depth_entries(sell)


def frame(*packets: bytes) -> bytes:
    """A binary message: int16 packet count, then (int16 length, packet) pairs."""
    return struct.pack(">H", len(packets)) + b"".join(struct.pack(">H", len(p)) + p for p in packets)


BIDS = [(100 + i, 150000 - i * 5, 1 + i) for i in range(5)]
OFFERS = [(200 + i, 150010 + i * 5, 11 + i) for i in range(5)]


# --- packet kinds --------------------------------------------------------------------------------------------------


def test_ltp_packet_is_eight_bytes():
    assert len(ltp_packet(INFY, 150005)) == 8
    tick = parse_packet(ltp_packet(INFY, 150005))
    assert tick == {"mode": "ltp", "instrument_token": INFY, "tradable": True, "last_price": 1500.05}


def test_quote_packet_44_bytes():
    packet = quote_packet(
        INFY,
        last=150005,
        last_qty=7,
        average=149950,
        volume=1_234_567,
        buy_qty=800,
        sell_qty=900,
        open_=149000,
        high=151000,
        low=148500,
        close=148000,
    )
    assert len(packet) == 44
    tick = parse_packet(packet)
    assert tick is not None
    assert tick["mode"] == "quote" and tick["tradable"] is True and tick["instrument_token"] == INFY
    assert tick["last_price"] == 1500.05
    assert tick["last_traded_quantity"] == 7
    assert tick["average_traded_price"] == 1499.5
    assert tick["volume_traded"] == 1_234_567
    assert tick["total_buy_quantity"] == 800 and tick["total_sell_quantity"] == 900
    assert tick["ohlc"] == {"open": 1490.0, "high": 1510.0, "low": 1485.0, "close": 1480.0}
    assert tick["change"] == pytest.approx((1500.05 - 1480.0) * 100 / 1480.0)
    assert "depth" not in tick and "oi" not in tick and "exchange_timestamp" not in tick


def test_full_packet_184_bytes_with_depth_oi_and_times():
    packet = full_packet(
        INFY,
        last=150005,
        last_qty=3,
        average=149900,
        volume=42,
        buy_qty=10,
        sell_qty=20,
        open_=149000,
        high=151000,
        low=148500,
        close=148000,
        last_trade=OPENED + 60,
        oi=5000,
        oi_high=5200,
        oi_low=4800,
        stamp=OPENED + 61,
        buy=BIDS,
        sell=OFFERS,
    )
    assert len(packet) == 184
    tick = parse_packet(packet)
    assert tick is not None
    assert tick["mode"] == "full"
    assert tick["last_trade_time"] == dt.datetime(2026, 11, 4, 9, 16, 0, tzinfo=IST)
    assert tick["exchange_timestamp"] == dt.datetime(2026, 11, 4, 9, 16, 1, tzinfo=IST)
    assert tick["last_trade_time"].utcoffset() == dt.timedelta(hours=5, minutes=30)
    assert (tick["oi"], tick["oi_day_high"], tick["oi_day_low"]) == (5000, 5200, 4800)
    # five bids then five offers, each with its quantity, price in rupees and order count; padding is skipped
    assert tick["depth"]["buy"] == [{"quantity": q, "price": p / 100, "orders": o} for q, p, o in BIDS]
    assert tick["depth"]["sell"] == [{"quantity": q, "price": p / 100, "orders": o} for q, p, o in OFFERS]
    assert tick["depth"]["buy"][0] == {"quantity": 100, "price": 1500.0, "orders": 1}
    assert tick["depth"]["sell"][4] == {"quantity": 204, "price": 1500.3, "orders": 15}


def test_index_quote_packet_28_bytes_uses_the_index_field_order():
    packet = index_packet(NIFTY_50, last=2_450_050, high=2_460_000, low=2_440_000, open_=2_445_000, close=2_430_000)
    assert len(packet) == 28
    tick = parse_packet(packet)
    assert tick is not None
    assert tick["mode"] == "quote" and tick["tradable"] is False
    assert tick["last_price"] == 24500.5
    assert tick["ohlc"] == {"open": 24450.0, "high": 24600.0, "low": 24400.0, "close": 24300.0}
    assert tick["change"] == pytest.approx((24500.5 - 24300.0) * 100 / 24300.0)
    assert "exchange_timestamp" not in tick
    assert "volume_traded" not in tick and "depth" not in tick  # indices have no volume or depth


def test_index_full_packet_32_bytes_adds_the_exchange_timestamp():
    packet = index_packet(
        NIFTY_50, last=2_450_050, high=2_460_000, low=2_440_000, open_=2_445_000, close=2_430_000, stamp=OPENED
    )
    assert len(packet) == 32
    tick = parse_packet(packet)
    assert tick is not None
    assert tick["mode"] == "full"
    assert tick["exchange_timestamp"] == dt.datetime(2026, 11, 4, 9, 15, 0, tzinfo=IST)
    assert tick["last_price"] == 24500.5


def test_every_packet_kind_is_told_apart_by_length_alone():
    modes = {
        8: "ltp",
        28: "quote",
        32: "full",
        44: "quote",
        184: "full",
    }
    packets = [
        ltp_packet(INFY, 1),
        index_packet(NIFTY_50, last=1, high=1, low=1, open_=1, close=1),
        index_packet(NIFTY_50, last=1, high=1, low=1, open_=1, close=1, stamp=OPENED),
        quote_packet(INFY, last=1),
        full_packet(INFY, last=1, buy=BIDS, sell=OFFERS),
    ]
    assert {len(p): parse_packet(p)["mode"] for p in packets} == modes  # type: ignore[index]


# --- prices: segment decides the divisor ------------------------------------------------------------------------------


def test_segment_is_the_low_byte_of_the_token():
    assert segment_of(INFY) == NSE
    assert segment_of(NIFTY_50) == INDICES
    assert segment_of(0x12345603) == NSE_CD


@pytest.mark.parametrize(
    ("token", "raw", "price"),
    [
        (token_for(5, NSE), 150005, 1500.05),  # everything else: paise
        (token_for(5, NFO), 2_345_075, 23450.75),
        (token_for(5, BSE), 7_000_000, 70000.0),
        (token_for(5, MCX), 7_250_000, 72500.0),
        (USDINR, 830_250_000, 83.025),  # NSE currency: 1e7
        (USDINR_BSE, 830_250, 83.025),  # BSE currency: 1e4
        (token_for(5, 12), 725_000, 72.5),  # NSE commodity (NCO, segment 12): 1e4, as in Kite's own client
        (NIFTY_50, 2_450_050, 24500.5),
    ],
)
def test_price_divisor_by_segment(token, raw, price):
    assert parse_packet(ltp_packet(token, raw))["last_price"] == price  # type: ignore[index]
    assert divisor_for(token) in (100.0, 10_000_000.0, 10_000.0)


def test_currency_divisor_applies_to_every_price_field_but_not_to_quantities():
    packet = full_packet(
        USDINR,
        last=830_250_000,
        average=830_000_000,
        volume=5000,
        open_=829_000_000,
        high=831_000_000,
        low=828_500_000,
        close=828_000_000,
        buy=[(10, 830_200_000, 1)] * 5,
        sell=[(20, 830_300_000, 2)] * 5,
    )
    tick = parse_packet(packet)
    assert tick is not None
    assert tick["last_price"] == 83.025 and tick["average_traded_price"] == 83.0
    assert tick["ohlc"] == {"open": 82.9, "high": 83.1, "low": 82.85, "close": 82.8}
    assert tick["depth"]["buy"][0] == {"quantity": 10, "price": 83.02, "orders": 1}
    assert tick["volume_traded"] == 5000


def test_change_is_percent_from_close_and_zero_without_a_close():
    up = parse_packet(quote_packet(INFY, last=11000, close=10000))
    flat = parse_packet(quote_packet(INFY, last=11000, close=0))
    down = parse_packet(ltp_packet(INFY, 5))
    assert up is not None and up["change"] == pytest.approx(10.0)
    assert flat is not None and flat["change"] == 0.0
    assert down is not None and "change" not in down  # ltp packets carry no close to compare with


def test_unsigned_quantities_and_signed_prices():
    big_volume = 3_000_000_000  # above 2**31
    tick = parse_packet(quote_packet(INFY, last=-250, volume=big_volume, buy_qty=big_volume))
    assert tick is not None
    assert tick["volume_traded"] == big_volume and tick["total_buy_quantity"] == big_volume
    assert tick["last_price"] == -2.5  # exchanges have printed negative prices


def test_zero_or_absurd_timestamps_are_none():
    unset = parse_packet(full_packet(INFY, last=1, buy=BIDS, sell=OFFERS))
    assert unset is not None
    assert unset["last_trade_time"] is None and unset["exchange_timestamp"] is None
    far = parse_packet(full_packet(INFY, last=1, stamp=4_294_967_295, buy=BIDS, sell=OFFERS))
    assert far is not None
    assert far["exchange_timestamp"] is None or far["exchange_timestamp"].year >= 2100


# --- frames -----------------------------------------------------------------------------------------------------------


def test_one_frame_carries_many_packets_of_different_kinds():
    packets = [
        ltp_packet(INFY, 150005),
        quote_packet(token_for(2, NSE), last=200000, volume=9),
        index_packet(NIFTY_50, last=2_450_050, high=2, low=1, open_=1, close=2_430_000),
        full_packet(USDINR, last=830_250_000, buy=BIDS, sell=OFFERS),
    ]
    ticks = parse_binary(frame(*packets))
    assert [t["instrument_token"] for t in ticks] == [INFY, token_for(2, NSE), NIFTY_50, USDINR]
    assert [t["mode"] for t in ticks] == ["ltp", "quote", "quote", "full"]
    assert [t["last_price"] for t in ticks] == [1500.05, 2000.0, 24500.5, 83.025]


def test_split_packets_returns_the_raw_packets_in_order():
    first, second = ltp_packet(INFY, 1), quote_packet(INFY, last=2)
    assert split_packets(frame(first, second)) == [first, second]


def test_heartbeats_and_empty_frames_carry_nothing():
    for heartbeat in (b"", b"\x00", b"\x01", b"\x00\x00", b"\x00\x00\x00"):
        assert split_packets(heartbeat) == []
        assert parse_binary(heartbeat) == []
    assert parse_binary(frame()) == []  # a count of zero


def test_unknown_packet_lengths_are_skipped_not_fatal():
    odd = b"\x00" * 13
    ticks = parse_binary(frame(ltp_packet(INFY, 1), odd, ltp_packet(INFY, 2)))
    assert [t["last_price"] for t in ticks] == [0.01, 0.02]
    assert parse_packet(odd) is None and parse_packet(b"") is None


def test_a_truncated_frame_yields_the_packets_before_the_damage():
    good = frame(ltp_packet(INFY, 1), ltp_packet(INFY, 2), ltp_packet(INFY, 3))
    assert len(parse_binary(good)) == 3
    assert len(parse_binary(good[:-1])) == 2  # the last packet lost a byte
    assert len(parse_binary(good[:-8])) == 2  # the last packet is entirely missing, only its length is there... or less
    assert len(parse_binary(good[: 2 + 2 + 8 + 1])) == 1  # cut in the middle of the second length field
    assert parse_binary(good[:3]) == []


def test_lying_counts_and_lengths_do_not_crash():
    packet = ltp_packet(INFY, 1)
    too_many = struct.pack(">H", 50) + struct.pack(">H", 8) + packet
    assert len(parse_binary(too_many)) == 1  # promised 50 packets, delivered 1
    too_long = struct.pack(">H", 1) + struct.pack(">H", 9999) + packet
    assert parse_binary(too_long) == []
    zero_length = struct.pack(">H", 2) + struct.pack(">H", 0) + struct.pack(">H", 8) + packet
    assert len(parse_binary(zero_length)) == 1
    assert len(parse_binary(struct.pack(">H", 0xFFFF) + b"\x00" * 6)) == 0


def test_every_prefix_of_a_valid_frame_parses_without_raising():
    valid = frame(
        ltp_packet(INFY, 1),
        full_packet(INFY, last=2, buy=BIDS, sell=OFFERS, stamp=OPENED),
        index_packet(NIFTY_50, last=3, high=3, low=3, open_=3, close=3, stamp=OPENED),
    )
    for cut in range(len(valid) + 1):
        parse_binary(valid[:cut])
    assert len(parse_binary(valid)) == 3


def test_random_garbage_never_raises():
    rng = random.Random(20261104)
    for _ in range(500):
        junk = rng.randbytes(rng.randrange(0, 400))
        parse_binary(junk)
        parse_packet(junk)
        parse_text(junk)
    for size in (8, 28, 32, 44, 184):  # every known size, random content
        for _ in range(50):
            assert parse_packet(rng.randbytes(size)) is not None


# --- text frames ------------------------------------------------------------------------------------------------------


def test_text_frames_order_error_and_message():
    order = parse_text('{"type": "order", "data": {"order_id": "1", "status": "COMPLETE"}}')
    assert order == {"type": "order", "data": {"order_id": "1", "status": "COMPLETE"}}
    assert parse_text('{"type":"error","data":"Invalid token"}') == {"type": "error", "data": "Invalid token"}
    assert parse_text(b'{"type":"message","data":"Maintenance at 6 PM"}') == {
        "type": "message",
        "data": "Maintenance at 6 PM",
    }


@pytest.mark.parametrize(
    "junk", ["", "not json", "[1, 2]", '"order"', "null", '{"data": 1}', '{"type": 5}', "{" * 5000]
)
def test_malformed_text_frames_are_none(junk):
    assert parse_text(junk) is None


def test_unknown_text_types_are_passed_through_for_the_caller_to_judge():
    assert parse_text('{"type": "instruments_meta", "data": {"count": 1}}') == {
        "type": "instruments_meta",
        "data": {"count": 1},
    }
    assert parse_text('{"type": "message"}') == {"type": "message", "data": None}
