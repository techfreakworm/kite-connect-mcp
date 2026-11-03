"""Tools: market data and the instrument master.

    quotes       get_quotes (full snapshot with depth), get_ohlc, get_ltp
                 https://kite.trade/docs/connect/v3/market-quotes/
    candles      get_historical_data (OHLCV, any interval; long ranges are split into several requests)
                 https://kite.trade/docs/connect/v3/historical/
    instruments  search_instruments, get_instrument, get_instruments_status, refresh_instruments, and (in the `mf`
                 toolset, because it reads the same store) search_mf_instruments

Quotes and candles need the paid Kite Connect plan: on the free Personal plan Kite answers PermissionException, which
the Registrar turns into a message saying so. The instrument master is the daily CSV dump cached by
kite_connect_mcp.instruments; it is free.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import re
from collections.abc import Sequence
from typing import Annotated, Any, Literal

from pydantic import Field

from ..errors import InstrumentsUnavailable, KiteAPIError, KiteMCPError, ToolInputError
from ..instruments import Instrument, InstrumentStore
from ..timeutil import format_kite_datetime, to_ist
from ._base import Registrar, Runtime, parse_instrument, require

Interval = Literal["minute", "3minute", "5minute", "10minute", "15minute", "30minute", "60minute", "day"]

QUOTE_LIMIT = 500  # instruments per /quote call (market-quotes docs, "Limits")
OHLC_LIMIT = 1000  # per /quote/ohlc and /quote/ltp call
SEARCH_LIMIT = 200

# How many days one historical request may span, by interval. Kite's historical page states no numbers, so these are
# the values Zerodha staff give on the Kite Connect forum (the one fact in this module that is not from the docs).
MAX_DAYS_PER_REQUEST: dict[str, int] = {
    "minute": 60,
    "3minute": 100,
    "5minute": 100,
    "10minute": 100,
    "15minute": 200,
    "30minute": 200,
    "60minute": 400,
    "day": 2000,
}
MAX_CANDLES = 2000  # the most candles one answer carries (the newest ones); ~25k tokens. Page with from/to for more
MAX_REQUESTS = 40  # the most historical requests one call makes: at 3 per second this bounds it to about 15 seconds
CANDLE_COLUMNS = ["date", "open", "high", "low", "close", "volume"]
INSTRUMENT_HELP = 'EXCHANGE:TRADINGSYMBOL (e.g. "NSE:INFY", "NSE:NIFTY 50", "NFO:NIFTY25NOVFUT")'


def instrument_store(rt: Runtime) -> InstrumentStore:
    if rt.instruments is None:
        raise KiteMCPError("the instrument master is not available in this runtime")
    return rt.instruments


# --- quotes --------------------------------------------------------------------------------------------------------


def quote_keys(instruments: Sequence[str], limit: int) -> list[str]:
    """The normalised, de-duplicated EXCHANGE:TRADINGSYMBOL keys, after checking the count and every format."""
    require(1 <= len(instruments) <= limit, f"give between 1 and {limit} instruments (got {len(instruments)})")
    keys: dict[str, None] = {}
    for text in instruments:
        exchange, symbol = parse_instrument(text)
        keys[f"{exchange}:{symbol}"] = None
    return list(keys)


async def quote_call(rt: Runtime, path: str, instruments: Sequence[str], limit: int) -> dict[str, Any]:
    """One quote request. Kite leaves out instruments it has no data for (unknown, expired): list them as `missing`
    so that nobody reads an absent key as a zero price."""
    keys = quote_keys(instruments, limit)
    data = await rt.client.get(path, params=[("i", key) for key in keys])
    quotes = data if isinstance(data, dict) else {}
    answered = {str(key).upper() for key in quotes}
    return {"quotes": quotes, "missing": [key for key in keys if key.upper() not in answered]}


# --- finding instruments -------------------------------------------------------------------------------------------

_DIGITS = re.compile(r"[0-9]+")


async def find_instrument(rt: Runtime, text: str) -> Instrument | None:
    """The instrument named by EXCHANGE:TRADINGSYMBOL or by a numeric instrument_token; None if the loaded list does not
    have it. Raises InstrumentsUnavailable when the list cannot be loaded."""
    store = instrument_store(rt)
    stripped = text.strip()
    if _DIGITS.fullmatch(stripped):
        return await store.by_token(int(stripped))
    exchange, symbol = parse_instrument(stripped)
    return await store.lookup(exchange, symbol)


# --- historical candles --------------------------------------------------------------------------------------------


def parse_bound(value: str, *, end: bool, name: str) -> dt.datetime:
    """An IST datetime from 'YYYY-MM-DD' (its first second, or with end=True its last) or 'YYYY-MM-DD HH:MM[:SS]'.
    A timestamp with an explicit offset is converted to IST."""
    text = value.strip()
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            moment = dt.datetime.combine(dt.date.fromisoformat(text), dt.time(23, 59, 59) if end else dt.time.min)
        else:
            moment = dt.datetime.fromisoformat(text)
    except ValueError:
        raise ToolInputError(f"{name} {value!r} must look like 2026-11-03 or 2026-11-03 09:15:00 (IST)") from None
    return to_ist(moment).replace(microsecond=0)


def plan_chunks(start: dt.datetime, end: dt.datetime, max_days: int) -> list[tuple[dt.datetime, dt.datetime]]:
    """Consecutive (from, to) ranges covering [start, end], oldest first, each just under `max_days` long.

    Kite's from and to are both inclusive and candles sit on whole minutes (day candles on midnight), so a range
    ending one second before the next begins loses nothing and double-counts nothing."""
    step, second = dt.timedelta(days=max_days), dt.timedelta(seconds=1)
    chunks: list[tuple[dt.datetime, dt.datetime]] = []
    cursor = start
    while cursor <= end:
        stop = min(end, cursor + step - second)
        chunks.append((cursor, stop))
        cursor = stop + second
    return chunks


def _bad_answer(why: str) -> KiteAPIError:
    return KiteAPIError(f"unexpected historical answer from Kite: {why}", error_type="DataException")


def candle_time(candle: Sequence[Any]) -> dt.datetime:
    try:
        return dt.datetime.fromisoformat(str(candle[0]))  # Kite writes 2017-12-15T09:15:00+0530
    except (ValueError, IndexError):
        raise _bad_answer(f"candle timestamp {candle[0] if candle else None!r}") from None


def clean_chunk(data: Any) -> list[list[Any]]:
    """The candles of one answer, oldest first and one per timestamp."""
    raw = data.get("candles") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        raise _bad_answer("no candles list")
    stamped: list[tuple[dt.datetime, list[Any]]] = []
    for candle in raw:
        if not isinstance(candle, list | tuple) or len(candle) < len(CANDLE_COLUMNS):
            raise _bad_answer(f"a candle that is not [date, open, high, low, close, volume]: {candle!r}"[:120])
        stamped.append((candle_time(candle), list(candle)))
    stamped.sort(key=lambda pair: pair[0])
    out: list[list[Any]] = []
    previous: dt.datetime | None = None
    for when, candle in stamped:
        if previous is None or when > previous:
            out.append(candle)
            previous = when
    return out


def prepend_older(newer: list[list[Any]], older: list[list[Any]]) -> list[list[Any]]:
    """`older` followed by `newer`, dropping any older candle at or after the first newer one (a range edge that Kite
    reports twice)."""
    if newer:
        first = candle_time(newer[0])
        older = [candle for candle in older if candle_time(candle) < first]
    return older + newer


async def fetch_candles(
    rt: Runtime,
    token: int,
    interval: str,
    start: dt.datetime,
    end: dt.datetime,
    *,
    continuous: bool,
    oi: bool,
    want: int,
) -> tuple[list[list[Any]], bool]:
    """(candles oldest first, complete) for [start, end].

    The range is cut into requests Kite accepts (plan_chunks) and fetched newest first, one after the other, so that a
    call that only needs the latest `want` candles stops after the first request or two instead of downloading years
    it would throw away. `complete` is False when it stopped early, because it already had `want` candles or hit
    MAX_REQUESTS; the answer may then have more history behind it."""
    path = f"/instruments/historical/{token}/{interval}"
    candles: list[list[Any]] = []
    chunks = reversed(plan_chunks(start, end, MAX_DAYS_PER_REQUEST[interval]))
    for requests_made, (chunk_from, chunk_to) in enumerate(chunks):
        if len(candles) >= want or requests_made >= MAX_REQUESTS:
            return candles, False
        params = {
            "from": format_kite_datetime(chunk_from),
            "to": format_kite_datetime(chunk_to),
            "continuous": 1 if continuous else 0,
            "oi": 1 if oi else 0,
        }
        data = await rt.client.get(path, params=params)
        candles = prepend_older(candles, clean_chunk(data))
    return candles, True


async def history_target(rt: Runtime, text: str, *, continuous: bool) -> tuple[int, str]:
    """(instrument_token, label) for a historical request. A numeric token needs no instrument list (it works for
    expired contracts too); a symbol is resolved through it."""
    stripped = text.strip()
    if _DIGITS.fullmatch(stripped):
        token = int(stripped)
        require(0 < token < 2**32, f"instrument_token {stripped} is out of range")
        inst: Instrument | None = None
        with contextlib.suppress(KiteMCPError):  # only for the label and the futures check: not needed to proceed
            inst = await find_instrument(rt, stripped)
    else:
        exchange, symbol = parse_instrument(stripped)
        try:
            inst = await find_instrument(rt, stripped)
        except InstrumentsUnavailable as exc:
            raise ToolInputError(
                f"cannot look up {exchange}:{symbol} ({exc}). Pass the numeric instrument_token instead"
            ) from None
        if inst is None:
            raise ToolInputError(
                f"{exchange}:{symbol} is not in today's instrument list (use search_instruments to find the exact "
                "tradingsymbol), or pass its numeric instrument_token"
            )
        token = inst.instrument_token
    if continuous and inst is not None and inst.instrument_type != "FUT":
        raise ToolInputError(f"continuous=true is for futures; {inst.key} is {inst.instrument_type or 'not a future'}")
    return token, inst.key if inst else str(token)


def register(reg: Registrar) -> None:
    rt = reg.rt

    # --- quotes -----------------------------------------------------------------------------------------------------

    @reg.tool("get_quotes", title="Full market quotes", toolset="market", kind="read")
    async def get_quotes(
        instruments: Annotated[
            list[str],
            Field(
                min_length=1,
                max_length=QUOTE_LIMIT,
                description=f"1 to {QUOTE_LIMIT} instruments, each {INSTRUMENT_HELP}",
            ),
        ],
    ) -> Any:
        """Full market snapshot for up to 500 instruments in one call: last_price, last_quantity, volume,
        average_price, buy_quantity / sell_quantity, ohlc (close is the previous session's close), net_change, oi /
        oi_day_high / oi_day_low (F&O), lower / upper circuit limits and the 5-level market depth (buy / sell price,
        quantity, orders). Prices are in INR, timestamps in IST. Returns {"quotes": {instrument: snapshot}, "missing":
        [...]}: `missing` lists requested instruments Kite has no data for (misspelt, expired or not traded). Needs the
        paid Kite Connect plan; quote calls are limited to 1 per second. A full quote with depth is about 1.5 KB: for
        many instruments use get_ltp or get_ohlc."""
        return await quote_call(rt, "/quote", instruments, QUOTE_LIMIT)

    @reg.tool("get_ohlc", title="OHLC quotes", toolset="market", kind="read")
    async def get_ohlc(
        instruments: Annotated[
            list[str],
            Field(
                min_length=1,
                max_length=OHLC_LIMIT,
                description=f"1 to {OHLC_LIMIT} instruments, each {INSTRUMENT_HELP}",
            ),
        ],
    ) -> Any:
        """Last price and the day's open / high / low / close (close is the previous session's close) for up to 1000
        instruments in one call, in INR. Returns {"quotes": {instrument: {instrument_token, last_price, ohlc}},
        "missing": [...]} (`missing`: instruments Kite has no data for). Lighter than get_quotes: no depth, volume or
        open interest. Needs the paid Kite Connect plan."""
        return await quote_call(rt, "/quote/ohlc", instruments, OHLC_LIMIT)

    @reg.tool("get_ltp", title="Last traded prices", toolset="market", kind="read")
    async def get_ltp(
        instruments: Annotated[
            list[str],
            Field(
                min_length=1,
                max_length=OHLC_LIMIT,
                description=f"1 to {OHLC_LIMIT} instruments, each {INSTRUMENT_HELP}",
            ),
        ],
    ) -> Any:
        """Last traded price (INR) for up to 1000 instruments in one call. Returns {"quotes": {instrument:
        {instrument_token, last_price}}, "missing": [...]} (`missing`: instruments Kite has no data for). The cheapest
        price check. Needs the paid Kite Connect plan."""
        return await quote_call(rt, "/quote/ltp", instruments, OHLC_LIMIT)

    # --- historical candles -----------------------------------------------------------------------------------------

    @reg.tool("get_historical_data", title="Historical candles", toolset="market", kind="read")
    async def get_historical_data(
        instrument: Annotated[
            str,
            Field(
                min_length=1,
                max_length=60,
                description=f"{INSTRUMENT_HELP} or a numeric instrument_token (e.g. 408065)",
            ),
        ],
        interval: Annotated[Interval, Field(description="candle size: minute, 3minute ... 60minute, or day")],
        from_date: Annotated[
            str, Field(description='start (IST): "YYYY-MM-DD" (from 00:00:00) or "YYYY-MM-DD HH:MM:SS"')
        ],
        to_date: Annotated[
            str,
            Field(
                description='end (IST, inclusive): "YYYY-MM-DD" (up to 23:59:59 of that day) or "YYYY-MM-DD HH:MM:SS"; '
                "a time in the future means now"
            ),
        ],
        continuous: Annotated[
            bool, Field(description="futures only, interval day: stitch the expired contracts of a future together")
        ] = False,
        oi: Annotated[bool, Field(description="add the open interest column (futures and options)")] = False,
        limit: Annotated[
            int | None,
            Field(
                ge=1, le=MAX_CANDLES, description=f"keep only the newest N candles (default and maximum {MAX_CANDLES})"
            ),
        ] = None,
    ) -> Any:
        """OHLCV candles for one instrument between two IST times, oldest first, in INR. Returns {instrument,
        instrument_token, interval, from, to, columns, candles, count}; each candle is a list in `columns` order
        (date, open, high, low, close, volume, and oi when requested), `date` being the candle's start as Kite sends it
        (e.g. 2026-11-03T09:15:00+0530; day candles are at 00:00).

        Kite limits the date range of one request by interval (minute 60 days, 3/5/10minute 100, 15/30minute 200,
        60minute 400, day 2000). Longer ranges are split into several requests automatically, newest first, and the
        tool stops as soon as it holds `limit` candles (at most 2000, always the newest). When older candles may exist
        in the range, `truncated` is true and `note` explains: to read further back, call again with to_date just
        before the first candle returned. continuous=true gives a future's day candles across its expired contracts
        (interval must be day). oi=true adds open interest (zero or null for plain equities). Needs the paid Kite
        Connect plan; historical requests are limited to 3 per second. Output is large (a candle is about 25 tokens, so
        2000 candles are about 50k): ask for the range you need, or set `limit`."""
        require(interval in MAX_DAYS_PER_REQUEST, f"interval must be one of {', '.join(MAX_DAYS_PER_REQUEST)}")
        require(
            not continuous or interval == "day",
            "continuous=true only works with interval=day: Kite's continuous API returns the day candles of a futures "
            "contract across its expired contracts",
        )
        now = to_ist(rt.clock()).replace(microsecond=0)
        start = parse_bound(from_date, end=False, name="from_date")
        requested_end = parse_bound(to_date, end=True, name="to_date")
        require(start <= now, f"from_date {from_date} is in the future (now: {now:%Y-%m-%d %H:%M} IST)")
        require(start <= requested_end, f"from_date {from_date} is after to_date {to_date}")
        end = min(requested_end, now)
        token, label = await history_target(rt, instrument, continuous=continuous)
        want = min(limit or MAX_CANDLES, MAX_CANDLES)
        candles, complete = await fetch_candles(
            rt, token, interval, start, end, continuous=continuous, oi=oi, want=want
        )

        columns = [*CANDLE_COLUMNS, "oi"] if oi else list(CANDLE_COLUMNS)
        trimmed = len(candles) > want
        if trimmed:
            candles = candles[-want:]
        rows = [[*candle[: len(columns)], *([None] * (len(columns) - len(candle)))] for candle in candles]
        out: dict[str, Any] = {
            "instrument": label,
            "instrument_token": token,
            "interval": interval,
            "from": format_kite_datetime(start),
            "to": format_kite_datetime(end),
            "columns": columns,
            "candles": rows,
            "count": len(rows),
        }
        if trimmed or not complete:
            out["truncated"] = True
            out["note"] = (
                f"Returned the newest {len(rows)} candles; the range from {out['from']} may hold older ones. To read "
                "further back call again with to_date just before the first candle returned (or narrow the range)."
                if rows
                else f"No candles found in the newest part of the range; the search stopped after {MAX_REQUESTS} "
                "requests. Narrow the range (move from_date closer to to_date)."
            )
        elif not rows:
            out["note"] = (
                "Kite returned no candles for this range (weekend or holiday, or the instrument did not trade)."
            )
        return out

    # --- the instrument master --------------------------------------------------------------------------------------

    @reg.tool("search_instruments", title="Search instruments", toolset="market", kind="read")
    async def search_instruments(
        query: Annotated[
            str,
            Field(
                max_length=100,
                description="part of the tradingsymbol or company name, case-insensitive; every word must match, in "
                'any order (e.g. "infy", "nifty bank", "reliance"). May be empty when a filter is given',
            ),
        ] = "",
        exchange: Annotated[str | None, Field(description="NSE, BSE, NFO, BFO, CDS, BCD or MCX")] = None,
        segment: Annotated[
            str | None, Field(description="e.g. NSE, BSE, INDICES, NFO-FUT, NFO-OPT, BFO-OPT, CDS-FUT, MCX-FUT")
        ] = None,
        instrument_type: Annotated[str | None, Field(description="EQ (also indices), FUT, CE or PE")] = None,
        expiry: Annotated[
            str | None, Field(description='derivatives only: "YYYY-MM-DD", or "YYYY-MM" for every expiry in that month')
        ] = None,
        limit: Annotated[int, Field(ge=1, le=SEARCH_LIMIT, description="most results to return")] = 20,
    ) -> Any:
        """Find instruments in Kite's daily instrument list (cached locally; see get_instruments_status). Returns a
        list, best match first (exact tradingsymbol, then prefix, then substring; NSE before NFO before BSE), each with
        key (EXCHANGE:TRADINGSYMBOL, the name every other tool takes), instrument_token, exchange_token, tradingsymbol,
        name, exchange, segment, instrument_type, expiry, strike, lot_size (orders on NFO / BFO must be a multiple
        of it), tick_size (INR; limit prices must be a multiple of it) and last_price (from the daily dump, NOT live:
        use get_ltp). A full list (`limit` entries) may have more behind it: add filters. Examples:
        query="NIFTY", instrument_type="FUT", exchange="NFO" for index futures; query="RELIANCE", expiry="2026-11" for
        that month's contracts."""
        found = await instrument_store(rt).search(
            query, exchange=exchange, segment=segment, instrument_type=instrument_type, expiry=expiry, limit=limit
        )
        return [instrument.as_dict() for instrument in found]

    @reg.tool("get_instrument", title="Instrument details", toolset="market", kind="read")
    async def get_instrument(
        instrument: Annotated[
            str, Field(min_length=1, max_length=60, description=f"{INSTRUMENT_HELP} or a numeric instrument_token")
        ],
    ) -> Any:
        """One instrument from Kite's daily instrument list: key, instrument_token, exchange_token, tradingsymbol, name,
        exchange, segment, instrument_type (EQ, FUT, CE, PE), expiry, strike, lot_size, tick_size (INR) and last_price
        (from the daily dump, not live). Use it to check the lot size and tick size before an order."""
        found = await find_instrument(rt, instrument)
        if found is None:
            raise ToolInputError(f"{instrument.strip()} is not in today's instrument list (use search_instruments)")
        return found.as_dict()

    @reg.tool("get_instruments_status", title="Instrument list status", toolset="market", kind="read")
    async def get_instruments_status() -> Any:
        """What the local instrument cache holds: for the instrument list and the mutual fund list, whether it is
        loaded, row counts (per exchange for the instrument list), when it was loaded, whether it is stale (Kite
        publishes a new dump every day around 08:30 IST), when it will next be refreshed and the last refresh error,
        if any. Never contacts Kite."""
        return await instrument_store(rt).status()

    @reg.tool("refresh_instruments", title="Refresh the instrument list", toolset="market", kind="local")
    async def refresh_instruments(
        force: Annotated[
            bool, Field(description="download both lists again even if today's are cached (default: only stale ones)")
        ] = False,
    ) -> Any:
        """Download Kite's instrument list and mutual fund list into the local cache (a few MB). The cache refreshes
        itself on first use after 08:30 IST each day, so this is only needed after a failed refresh or when a brand-new
        contract is missing. Returns the same as get_instruments_status plus `refreshed` (the lists replaced). A list
        that fails to download keeps its old copy, and the error is shown under last_error."""
        return await instrument_store(rt).refresh(force=force)

    @reg.tool("search_mf_instruments", title="Search mutual fund schemes", toolset="mf", kind="read")
    async def search_mf_instruments(
        query: Annotated[
            str,
            Field(
                max_length=100,
                description="part of the fund name or its ISIN, case-insensitive; every word must match "
                '(e.g. "axis bluechip", "INF846K01DP8"). May be empty when a filter is given',
            ),
        ] = "",
        amc: Annotated[
            str | None,
            Field(description='part of the fund house code, e.g. "AXIS" (codes look like AXISMUTUALFUND_MF)'),
        ] = None,
        plan: Annotated[str | None, Field(description="direct or regular")] = None,
        scheme_type: Annotated[str | None, Field(description="equity, debt, elss ...")] = None,
        limit: Annotated[int, Field(ge=1, le=SEARCH_LIMIT, description="most results to return")] = 20,
    ) -> Any:
        """Find mutual fund schemes on Zerodha Coin in Kite's daily mutual fund list (cached locally). Returns a list,
        best match first, each with tradingsymbol (the scheme's ISIN, which the MF tools take), amc, name,
        purchase_allowed, redemption_allowed, minimum_purchase_amount and purchase_amount_multiplier (INR),
        minimum_additional_purchase_amount, minimum_redemption_quantity and redemption_quantity_multiplier (units),
        dividend_type (growth / payout), scheme_type, plan (direct / regular), settlement_type, last_price (the latest
        NAV, INR) and last_price_date."""
        found = await instrument_store(rt).search_mf(query, amc=amc, plan=plan, scheme_type=scheme_type, limit=limit)
        return [fund.as_dict() for fund in found]
