"""Kite's WebSocket wire format: binary market-data frames and JSON text frames.

Layout (https://kite.trade/docs/connect/v3/websocket/), all integers big-endian:

    frame   = int16 packet_count, then packet_count x (int16 length, `length` bytes of packet)
    packet  = the quote data of one instrument; its kind is told apart by its LENGTH:

        8    ltp          token, last price
        28   index quote  token, last, high, low, open, close, price change
        32   index full   the 28 bytes + exchange timestamp
        44   quote        token, last, last qty, average, volume, buy qty, sell qty, open, high, low, close
        184  full         the 44 bytes + last trade time, OI, OI day high, OI day low, exchange timestamp
                          + 10 depth entries (5 bids, then 5 offers) of int32 qty, int32 price, int16 orders, 2 pad

A 1-byte frame is Kite's heartbeat and carries nothing. Text frames are JSON `{"type": "order" | "error" | "message",
"data": ...}`.

Prices arrive as integers. The docs say to divide by 100, or by 10_000_000 "for currencies". The page does not say
how to tell a currency instrument apart, nor that BSE currency derivatives use a third divisor. This module follows
the convention of Kite's client libraries: the exchange segment is `instrument_token & 0xff`, 3 is NSE currency
derivatives (10_000_000), 6 is BSE currency derivatives (10_000) and 9 is the indices (not tradable). If prices for
a currency segment ever look wrong, PRICE_DIVISORS is the one table to check.

Choices made where the docs are silent:

* `change` is (last - close) * 100 / close, computed here for every kind of packet; 0 when there is no close.
* Quantities, open interest, order counts and timestamps are read as unsigned (a day's volume can pass 2**31); prices
  as signed (the docs say int32, and exchanges have printed negative prices).
* A timestamp of 0 means "not set" and becomes None.

Nothing in here raises on bad input: a frame that cannot be parsed yields fewer (or no) ticks, never an exception.
"""

from __future__ import annotations

import datetime as dt
import json
import struct
from typing import Any

from ..timeutil import IST

SEGMENT_NSE_CD = 3  # NSE currency derivatives
SEGMENT_BSE_CD = 6  # BSE currency derivatives
SEGMENT_INDICES = 9
SEGMENT_NSE_COM = 12  # NSE commodity derivatives (NCO)

DEFAULT_DIVISOR = 100.0
# Checked against Kite's official Python client (kiteconnect 5.2.2, ticker.py): cds -> 1e7, bcd and nco -> 1e4, else 100
PRICE_DIVISORS: dict[int, float] = {SEGMENT_NSE_CD: 10_000_000.0, SEGMENT_BSE_CD: 10_000.0, SEGMENT_NSE_COM: 10_000.0}

LTP_SIZE = 8
INDEX_QUOTE_SIZE = 28
INDEX_FULL_SIZE = 32
QUOTE_SIZE = 44
FULL_SIZE = 184
KNOWN_SIZES = (LTP_SIZE, INDEX_QUOTE_SIZE, INDEX_FULL_SIZE, QUOTE_SIZE, FULL_SIZE)

DEPTH_OFFSET = 64
DEPTH_ENTRY_SIZE = 12
DEPTH_LEVELS = 5  # per side: five bids, then five offers

_COUNT = struct.Struct(">H")
_LENGTH = struct.Struct(">H")
_TOKEN = struct.Struct(">I")
_LTP = struct.Struct(">Ii")
_INDEX = struct.Struct(">Iiiiiii")  # token, last, high, low, open, close, price change
_STAMP = struct.Struct(">I")  # a unix timestamp
_QUOTE = struct.Struct(">IiIiIIIiiii")  # token, last, last qty, average, volume, buy, sell, open, high, low, close
_FULL_EXTRA = struct.Struct(">IIIII")  # last trade time, oi, oi day high, oi day low, exchange timestamp
_DEPTH = struct.Struct(">IiH2x")  # quantity, price, orders, 2 bytes of padding


def segment_of(instrument_token: int) -> int:
    """The exchange segment encoded in the low byte of an instrument token."""
    return instrument_token & 0xFF


def divisor_for(instrument_token: int) -> float:
    """What to divide the integer prices of this instrument's packets by to get rupees."""
    return PRICE_DIVISORS.get(segment_of(instrument_token), DEFAULT_DIVISOR)


def split_packets(frame: bytes) -> list[bytes]:
    """The packets inside one binary frame. Stops quietly at the first truncated entry (once a length is wrong there
    is no telling where the next packet starts), so a damaged frame still yields what came before the damage. A
    1-byte heartbeat, an empty frame and a count of 0 all give []."""
    if len(frame) < _COUNT.size + _LENGTH.size:
        return []
    (count,) = _COUNT.unpack_from(frame, 0)
    packets: list[bytes] = []
    offset = _COUNT.size
    for _ in range(count):
        if offset + _LENGTH.size > len(frame):
            break
        (length,) = _LENGTH.unpack_from(frame, offset)
        offset += _LENGTH.size
        if offset + length > len(frame):
            break
        packets.append(frame[offset : offset + length])
        offset += length
    return packets


def parse_packet(packet: bytes) -> dict[str, Any] | None:
    """One packet as a plain dict, or None if its length is not one Kite documents (the caller counts those)."""
    size = len(packet)
    if size not in KNOWN_SIZES:
        return None
    (token,) = _TOKEN.unpack_from(packet)
    divisor = divisor_for(token)
    tick: dict[str, Any] = {"instrument_token": token, "tradable": segment_of(token) != SEGMENT_INDICES}
    if size == LTP_SIZE:
        tick["mode"] = "ltp"
        tick["last_price"] = _LTP.unpack_from(packet)[1] / divisor
    elif size in (INDEX_QUOTE_SIZE, INDEX_FULL_SIZE):
        _fill_index(tick, packet, divisor)
    else:
        _fill_quote(tick, packet, divisor)
    return tick


def parse_binary(frame: bytes) -> list[dict[str, Any]]:
    """Every tick in a binary frame. Unrecognised packets are skipped."""
    ticks = []
    for packet in split_packets(frame):
        tick = parse_packet(packet)
        if tick is not None:
            ticks.append(tick)
    return ticks


def parse_text(message: str | bytes) -> dict[str, Any] | None:
    """A text frame as {"type", "data"}, or None if it is not a JSON object with a string `type`. Unknown types are
    returned as they are: what to do with them is the caller's decision."""
    try:
        body = json.loads(message)
    except (ValueError, RecursionError):
        return None
    if not isinstance(body, dict) or not isinstance(body.get("type"), str):
        return None
    return {"type": body["type"], "data": body.get("data")}


# --- packet kinds --------------------------------------------------------------------------------------------------


def _fill_index(tick: dict[str, Any], packet: bytes, divisor: float) -> None:
    """Indices list high, low, open, close (tradable instruments: open, high, low, close)."""
    _, last, high, low, open_, close, _price_change = _INDEX.unpack_from(packet)
    full = len(packet) == INDEX_FULL_SIZE
    tick["mode"] = "full" if full else "quote"
    tick["last_price"] = last / divisor
    tick["ohlc"] = _ohlc(open_, high, low, close, divisor)
    tick["change"] = _change_percent(tick["last_price"], tick["ohlc"]["close"])
    if full:
        tick["exchange_timestamp"] = _moment(_STAMP.unpack_from(packet, INDEX_QUOTE_SIZE)[0])


def _fill_quote(tick: dict[str, Any], packet: bytes, divisor: float) -> None:
    _, last, last_qty, average, volume, buy_qty, sell_qty, open_, high, low, close = _QUOTE.unpack_from(packet)
    full = len(packet) == FULL_SIZE
    tick["mode"] = "full" if full else "quote"
    tick["last_price"] = last / divisor
    tick["last_traded_quantity"] = last_qty
    tick["average_traded_price"] = average / divisor
    tick["volume_traded"] = volume
    tick["total_buy_quantity"] = buy_qty
    tick["total_sell_quantity"] = sell_qty
    tick["ohlc"] = _ohlc(open_, high, low, close, divisor)
    tick["change"] = _change_percent(tick["last_price"], tick["ohlc"]["close"])
    if not full:
        return
    last_trade, oi, oi_high, oi_low, stamp = _FULL_EXTRA.unpack_from(packet, QUOTE_SIZE)
    tick["last_trade_time"] = _moment(last_trade)
    tick["oi"] = oi
    tick["oi_day_high"] = oi_high
    tick["oi_day_low"] = oi_low
    tick["exchange_timestamp"] = _moment(stamp)
    tick["depth"] = _depth(packet, divisor)


def _depth(packet: bytes, divisor: float) -> dict[str, list[dict[str, Any]]]:
    entries = []
    for index in range(2 * DEPTH_LEVELS):
        quantity, price, orders = _DEPTH.unpack_from(packet, DEPTH_OFFSET + index * DEPTH_ENTRY_SIZE)
        entries.append({"quantity": quantity, "price": price / divisor, "orders": orders})
    return {"buy": entries[:DEPTH_LEVELS], "sell": entries[DEPTH_LEVELS:]}


# --- small helpers -------------------------------------------------------------------------------------------------


def _ohlc(open_: int, high: int, low: int, close: int, divisor: float) -> dict[str, float]:
    return {"open": open_ / divisor, "high": high / divisor, "low": low / divisor, "close": close / divisor}


def _change_percent(last: float, close: float) -> float:
    """Percent move from the previous close; 0 when there is no close to compare with."""
    return (last - close) * 100 / close if close else 0.0


def _moment(seconds: int) -> dt.datetime | None:
    """Unix seconds as an IST datetime; 0 (Kite's "not set") and anything out of range give None."""
    if seconds <= 0:
        return None
    try:
        return dt.datetime.fromtimestamp(seconds, IST)
    except (OverflowError, OSError, ValueError):
        return None
