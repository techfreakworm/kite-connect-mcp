"""The instrument master: downloading, caching, staleness, failure handling and search (all against FakeKite)."""

from __future__ import annotations

import asyncio
import contextlib
import csv
import datetime as dt
import gzip
import io
import os
import sqlite3
import stat
import time
from collections import Counter
from decimal import Decimal
from pathlib import Path

import httpx2
import pytest

from conftest import FIXED_NOW
from kite_connect_mcp import instruments as instruments_module
from kite_connect_mcp.errors import InstrumentsUnavailable, ToolInputError
from kite_connect_mcp.instruments import (
    Instrument,
    InstrumentStore,
    build_search,
    expiry_bounds,
    is_stale,
    latest_publication,
    next_publication,
)
from kite_connect_mcp.timeutil import IST

# --- fixtures: small, realistic dumps -------------------------------------------------------------------------------

HEADER = [
    "instrument_token",
    "exchange_token",
    "tradingsymbol",
    "name",
    "last_price",
    "expiry",
    "strike",
    "tick_size",
    "lot_size",
    "instrument_type",
    "segment",
    "exchange",
]
MF_HEADER = [
    "tradingsymbol",
    "amc",
    "name",
    "purchase_allowed",
    "redemption_allowed",
    "minimum_purchase_amount",
    "purchase_amount_multiplier",
    "minimum_additional_purchase_amount",
    "minimum_redemption_quantity",
    "redemption_quantity_multiplier",
    "dividend_type",
    "scheme_type",
    "plan",
    "settlement_type",
    "last_price",
    "last_price_date",
]


def inst_row(
    symbol: str,
    token: int,
    *,
    exchange: str = "NSE",
    name: str = "",
    segment: str | None = None,
    itype: str = "EQ",
    expiry: str = "",
    strike: str = "",
    tick: str = "0.05",
    lot: str = "1",
    last: str = "0",
) -> list[str]:
    """One row of the instrument dump, columns in Kite's order (the exchange token is the token shifted by 8 bits)."""
    return [
        str(token),
        str(token // 256),
        symbol,
        name,
        last,
        expiry,
        strike,
        tick,
        lot,
        itype,
        segment or exchange,
        exchange,
    ]


def to_csv(header: list[str], rows: list[list[str]]) -> str:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    return out.getvalue()


def fut(symbol: str, token: int, expiry: str, *, lot: str = "75", name: str = "NIFTY", **kw: str) -> list[str]:
    return inst_row(
        symbol, token, exchange="NFO", name=name, segment="NFO-FUT", itype="FUT", expiry=expiry, lot=lot, **kw
    )


def option(symbol: str, token: int, expiry: str, itype: str, strike: str, name: str = "NIFTY") -> list[str]:
    return inst_row(
        symbol,
        token,
        exchange="NFO",
        name=name,
        segment="NFO-OPT",
        itype=itype,
        expiry=expiry,
        strike=strike,
        lot="75",
        last="310.35",
    )


DUMP_ROWS = [
    inst_row("INFY", 408065, name="INFOSYS"),
    inst_row("INFY", 128053508, exchange="BSE", name="INFOSYS LTD"),
    inst_row("RELIANCE", 738561, name="RELIANCE INDUSTRIES", tick="0.10"),
    inst_row("TCS", 2953217, name="TATA CONSULTANCY SERVICES"),
    inst_row("TATAMOTORS", 884737, name="TATA MOTORS"),
    inst_row("NIFTY 50", 256265, name="NIFTY 50", segment="INDICES"),
    inst_row("NIFTY BANK", 260105, name="NIFTY BANK", segment="INDICES"),
    fut("NIFTY26NOVFUT", 12517890, "2026-11-24", last="24100.5"),
    fut("NIFTY26DECFUT", 12518146, "2026-12-29", last="24180.0"),
    fut("BANKNIFTY26NOVFUT", 12519170, "2026-11-24", lot="35", name="BANKNIFTY"),
    option("NIFTY26NOV24000CE", 12520450, "2026-11-24", "CE", "24000"),
    option("NIFTY26NOV24000PE", 12520706, "2026-11-24", "PE", "24000"),
    option("NIFTY26NOV9500CE", 12520962, "2026-11-24", "CE", "9500"),
    option("NIFTY26DEC24000CE", 12521218, "2026-12-29", "CE", "24000"),
    inst_row(
        "USDINR26NOVFUT",
        14000001,
        exchange="CDS",
        name="USDINR",
        segment="CDS-FUT",
        itype="FUT",
        expiry="2026-11-27",
        tick="0.0025",
        lot="1000",
    ),
]
MF_ROWS = [
    "INF846K01DP8,AXISMUTUALFUND_MF,Axis Bluechip Fund - Direct Plan - Growth,1,1,5000.0,1.0,100.0,1.0,0.001,"
    "growth,equity,direct,T3,57.1234,2026-11-03",
    "INF846K01EW2,AXISMUTUALFUND_MF,Axis Long Term Equity Fund - Direct Growth,1,0,500.0,500.0,500.0,1.0,0.001,"
    "growth,elss,direct,T3,98.5,2026-11-03",
    "INF846K01131,AXISMUTUALFUND_MF,Axis Bluechip Fund - Regular Plan - Growth,1,1,5000.0,1.0,100.0,1.0,0.001,"
    "growth,equity,regular,T3,52.0,2026-11-03",
    "INF174K01LS2,KOTAKMAHINDRAMF,Kotak Select Focus Fund - Direct Plan - Growth,1,1,5000.0,1.0,1000.0,0.001,0.001,"
    "growth,equity,direct,T3,26.549,2026-11-03",
    "INF179K01XQ0,HDFCMUTUALFUND_MF,HDFC Liquid Fund - Direct Plan - Growth,0,1,5000.0,1.0,1000.0,0.001,0.001,"
    "growth,debt,direct,T1,4987.5,",
]


def serve_dumps(
    fake,
    *,
    rows: list[list[str]] | None = None,
    mf: list[str] | None = None,
    gz: bool = True,
    header: list[str] | None = None,
) -> None:
    """Make FakeKite answer both dump endpoints with these rows (gzipped like Kite's, or plain)."""
    text = to_csv(header or HEADER, DUMP_ROWS if rows is None else rows)
    mf_text = ",".join(MF_HEADER) + "\n" + "\n".join(MF_ROWS if mf is None else mf) + "\n"
    for path, body in (("/instruments", text), ("/mf/instruments", mf_text)):
        data = gzip.compress(body.encode()) if gz else body.encode()
        fake.on("GET", path, body=data, content_type="text/csv")


def downloads(fake, path: str = "/instruments") -> int:
    return len(fake.calls("GET", path))


def count_rows(path: Path, table: str) -> int:
    with contextlib.closing(sqlite3.connect(path)) as con:
        return con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def write_cache(path: Path, *, schema: str, meta: dict[str, str] | None = None) -> None:
    """A cache file as an older or damaged run might have left it: just the meta table."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.closing(sqlite3.connect(path)) as con:
        con.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        con.executemany("INSERT INTO meta VALUES (?, ?)", [("schema", schema), *(meta or {}).items()])
        con.commit()


class Clock:
    """A settable wall clock (IST)."""

    def __init__(self, now: dt.datetime) -> None:
        self.now = now

    def __call__(self) -> dt.datetime:
        return self.now


def ist(day: int, hour: int, minute: int = 0) -> dt.datetime:
    """A moment in November 2026 (the 4th is a Wednesday)."""
    return dt.datetime(2026, 11, day, hour, minute, tzinfo=IST)


@pytest.fixture
def clock() -> Clock:
    return Clock(FIXED_NOW)  # Wednesday 4 November 2026, 10:00 IST


@pytest.fixture
def store(runtime_factory, clock) -> InstrumentStore:
    rt = runtime_factory()
    return InstrumentStore(rt.settings, rt.client, clock=clock)


def keys(found: list[Instrument]) -> list[str]:
    return [i.key for i in found]


# --- loading and caching --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("gz", [True, False], ids=["gzipped", "plain"])
async def test_first_use_loads_the_list_once(store, fake_kite, gz):
    serve_dumps(fake_kite, gz=gz)
    infy = await store.lookup("NSE", "INFY")
    assert infy == Instrument(
        instrument_token=408065,
        exchange_token=1594,
        tradingsymbol="INFY",
        name="INFOSYS",
        last_price=0.0,
        expiry=None,
        strike=0.0,
        tick_size=Decimal("0.05"),
        lot_size=1,
        instrument_type="EQ",
        segment="NSE",
        exchange="NSE",
    )
    assert infy.key == "NSE:INFY"
    assert downloads(fake_kite) == 1
    assert downloads(fake_kite, "/mf/instruments") == 0  # the MF list is only fetched when someone asks for it
    await store.lookup("NSE", "TCS")
    await store.by_token(408065)
    await store.search("nifty")
    assert downloads(fake_kite) == 1  # served from the cache file from now on


async def test_cheap_to_construct(runtime_factory, fake_kite, clock):
    rt = runtime_factory()
    InstrumentStore(rt.settings, rt.client, clock=clock)
    assert fake_kite.requests == []
    assert not (rt.settings.cache_dir / "instruments.sqlite").exists()


async def test_values_are_parsed_into_their_types(store, fake_kite):
    serve_dumps(fake_kite)
    future = await store.lookup("NFO", "NIFTY26NOVFUT")
    assert future is not None
    assert future.expiry == dt.date(2026, 11, 24)
    assert future.lot_size == 75 and isinstance(future.lot_size, int)
    assert future.last_price == 24100.5
    assert (future.instrument_type, future.segment, future.name) == ("FUT", "NFO-FUT", "NIFTY")
    call = await store.lookup("NFO", "NIFTY26NOV24000CE")
    assert call is not None and call.strike == 24000.0 and call.instrument_type == "CE"
    currency = await store.lookup("CDS", "USDINR26NOVFUT")
    assert currency is not None and currency.tick_size == Decimal("0.0025") and currency.lot_size == 1000
    index = await store.lookup("NSE", "NIFTY 50")
    assert index is not None and index.segment == "INDICES" and index.expiry is None
    reliance = await store.lookup("NSE", "RELIANCE")
    assert reliance is not None and reliance.tick_size == Decimal("0.10")
    as_dict = future.as_dict()
    assert as_dict["expiry"] == "2026-11-24" and as_dict["tick_size"] == 0.05 and as_dict["key"] == "NFO:NIFTY26NOVFUT"


async def test_lookup_is_exact_and_case_insensitive(store, fake_kite):
    serve_dumps(fake_kite)
    nse, bse, index = (
        await store.lookup("nse", "infy"),
        await store.lookup(" BSE ", " Infy "),
        await store.lookup("NSE", "nifty 50"),
    )
    assert nse is not None and nse.instrument_token == 408065
    assert bse is not None and bse.instrument_token == 128053508
    assert index is not None and index.instrument_token == 256265
    assert await store.lookup("NSE", "INF") is None  # not a prefix search
    assert await store.lookup("NFO", "INFY") is None  # right symbol, wrong exchange
    assert await store.lookup("NSE", "") is None


async def test_by_token(store, fake_kite):
    serve_dumps(fake_kite)
    found = await store.by_token(12517890)
    assert found is not None and found.key == "NFO:NIFTY26NOVFUT"
    assert await store.by_token(1) is None


async def test_cache_folder_names_may_hold_odd_characters(runtime_factory, fake_kite, clock, tmp_path):
    serve_dumps(fake_kite)
    rt = runtime_factory(KITE_MCP_CACHE_DIR=str(tmp_path / "my cache #1 ?x & y"))
    store = InstrumentStore(rt.settings, rt.client, clock=clock)
    assert await store.lookup("NSE", "INFY") is not None
    clock.now = ist(5, 9)
    assert await store.lookup("NSE", "INFY") is not None  # the second build ATTACHes the first file
    assert downloads(fake_kite) == 2 and store.cache_file.parent.name == "my cache #1 ?x & y"


async def test_cache_file_is_private(store, fake_kite):
    serve_dumps(fake_kite)
    await store.lookup("NSE", "INFY")
    assert store.cache_file.name == "instruments.sqlite"
    assert stat.S_IMODE(store.cache_file.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(store.cache_file.stat().st_mode) == 0o600
    assert [p.name for p in store.cache_file.parent.iterdir()] == ["instruments.sqlite"]  # no temp files left


async def test_concurrent_first_use_downloads_once(store, fake_kite):
    serve_dumps(fake_kite)
    results = await asyncio.gather(*(store.lookup("NSE", "INFY") for _ in range(25)), store.search("tata"))
    assert all(r is not None for r in results[:-1])
    assert downloads(fake_kite) == 1


async def test_a_new_process_reuses_the_cache_file(runtime_factory, fake_kite, clock):
    serve_dumps(fake_kite)
    rt = runtime_factory()
    await InstrumentStore(rt.settings, rt.client, clock=clock).lookup("NSE", "INFY")
    second = InstrumentStore(rt.settings, rt.client, clock=clock)
    status = await second.status()
    assert status["loaded"] is True and status["instruments"]["rows"] == len(DUMP_ROWS)
    assert await second.lookup("NSE", "TCS") is not None
    assert downloads(fake_kite) == 1


async def test_dumps_need_no_access_token(runtime_factory, fake_kite, clock, tmp_path):
    serve_dumps(fake_kite)
    rt = runtime_factory(KITE_TOKEN_FILE=str(tmp_path / "no-such-token.json"))
    store = InstrumentStore(rt.settings, rt.client, clock=clock)
    assert await store.lookup("NSE", "INFY") is not None
    assert "Authorization" not in fake_kite.requests[0].headers


async def test_a_rejected_token_does_not_stop_the_public_dump(runtime_factory, fake_kite, clock):
    plain = to_csv(HEADER, DUMP_ROWS).encode()

    def handler(request: httpx2.Request) -> httpx2.Response:
        if "Authorization" in request.headers:  # Kite refuses the (stale) token ...
            return httpx2.Response(
                403, json={"status": "error", "message": "Invalid token", "error_type": "TokenException", "data": None}
            )
        return httpx2.Response(200, content=plain, headers={"content-type": "text/csv"})  # ... but the dump is public

    fake_kite.on("GET", "/instruments", handler=handler)
    rt = runtime_factory()
    store = InstrumentStore(rt.settings, rt.client, clock=clock)
    assert await store.lookup("NSE", "INFY") is not None
    assert downloads(fake_kite) == 2


# --- search ---------------------------------------------------------------------------------------------------------


async def test_search_ranks_exact_then_prefix_then_substring(store, fake_kite):
    rows = [
        inst_row("XTATAX", 1, name="IN THE MIDDLE"),
        inst_row("TATASTEEL", 2, name="TATA STEEL"),
        inst_row("AUTOCOMP", 3, name="AUTO COMPONENTS OF THE TATA GROUP"),
        inst_row("TATA", 4, name="TATA EXAMPLE"),
        inst_row("TATAMOTORS", 5, name="TATA MOTORS"),
        inst_row("TCS", 6, name="TATA CONSULTANCY SERVICES"),
        inst_row("TATA", 7, exchange="BSE", name="TATA EXAMPLE"),
        inst_row("UNRELATED", 8, name="NOTHING"),
    ]
    serve_dumps(fake_kite, rows=rows)
    found = await store.search("tata")
    assert keys(found) == [
        "NSE:TATA",  # exact tradingsymbol (NSE before BSE)
        "BSE:TATA",
        "NSE:TATASTEEL",  # tradingsymbol prefix, shorter first
        "NSE:TATAMOTORS",
        "NSE:TCS",  # name prefix
        "NSE:XTATAX",  # tradingsymbol substring
        "NSE:AUTOCOMP",  # name substring
    ]


async def test_search_is_case_insensitive_and_matches_the_name(store, fake_kite):
    serve_dumps(fake_kite)
    assert keys(await store.search("InFoSyS")) == ["NSE:INFY", "BSE:INFY"]
    assert keys(await store.search("  reliance   industries ")) == ["NSE:RELIANCE"]


async def test_search_words_may_come_in_any_order(store, fake_kite):
    serve_dumps(fake_kite)
    assert keys(await store.search("bank nifty")) == ["NSE:NIFTY BANK", "NFO:BANKNIFTY26NOVFUT"]
    assert keys(await store.search("nifty 50"))[0] == "NSE:NIFTY 50"  # the exact symbol still comes first


async def test_search_orders_derivatives_by_length_expiry_and_strike(store, fake_kite):
    serve_dumps(fake_kite)
    found = await store.search("nifty", exchange="NFO", instrument_type="FUT")
    # prefix matches first (nearest expiry first), the substring match (BANKNIFTY...) after them
    assert keys(found) == ["NFO:NIFTY26NOVFUT", "NFO:NIFTY26DECFUT", "NFO:BANKNIFTY26NOVFUT"]
    options = await store.search("NIFTY26", exchange="NFO", instrument_type="CE")
    assert keys(options) == ["NFO:NIFTY26NOV9500CE", "NFO:NIFTY26NOV24000CE", "NFO:NIFTY26DEC24000CE"]


async def test_search_filters(store, fake_kite):
    serve_dumps(fake_kite)
    assert {i.exchange for i in await store.search("nifty", exchange="nfo")} == {"NFO"}
    assert keys(await store.search("", segment="indices")) == ["NSE:NIFTY 50", "NSE:NIFTY BANK"]
    assert {i.instrument_type for i in await store.search("", instrument_type="fut")} == {"FUT"}
    assert keys(await store.search("nifty", segment="NFO-OPT", instrument_type="PE")) == ["NFO:NIFTY26NOV24000PE"]
    assert keys(await store.search("infy", exchange="BSE")) == ["BSE:INFY"]


async def test_search_by_expiry_date_or_month(store, fake_kite):
    serve_dumps(fake_kite)
    november_futures = ["NFO:NIFTY26NOVFUT", "NFO:BANKNIFTY26NOVFUT"]
    assert keys(await store.search("nifty", instrument_type="FUT", expiry=dt.date(2026, 11, 24))) == november_futures
    assert keys(await store.search("nifty", instrument_type="FUT", expiry="2026-11-24")) == november_futures
    assert sorted(keys(await store.search("", expiry="2026-11-24", instrument_type="FUT"))) == sorted(november_futures)
    december = await store.search("nifty", expiry="2026-12")
    assert {i.key for i in december} == {"NFO:NIFTY26DECFUT", "NFO:NIFTY26DEC24000CE"}
    assert await store.search("nifty", expiry="2027-01") == []
    assert await store.search("infy", expiry="2026-11") == []  # equities have no expiry


async def test_search_limit_and_empty_results(store, fake_kite):
    serve_dumps(fake_kite)
    assert len(await store.search("nifty", limit=3)) == 3
    assert await store.search("no-such-thing") == []


async def test_search_needs_a_query_or_a_filter(store, fake_kite):
    serve_dumps(fake_kite)
    with pytest.raises(ToolInputError, match="query or at least one filter"):
        await store.search("   ")
    with pytest.raises(ToolInputError, match="limit"):
        await store.search("nifty", limit=0)
    with pytest.raises(ToolInputError, match="expiry"):
        await store.search("nifty", expiry="next month")
    assert fake_kite.requests == []  # bad input is refused before anything is downloaded


async def test_search_treats_wildcards_and_quotes_as_plain_text(store, fake_kite):
    serve_dumps(fake_kite)
    assert await store.search("%") == []
    assert await store.search("_") == []
    assert await store.search("\\") == []
    assert await store.search("x'; DROP TABLE instruments; --") == []
    assert await store.lookup("NSE", "INFY") is not None  # the table is still there


def test_expiry_bounds():
    assert expiry_bounds("2026-11-24") == ("2026-11-24", "2026-11-25")
    assert expiry_bounds(dt.date(2026, 12, 31)) == ("2026-12-31", "2027-01-01")
    assert expiry_bounds("2026-12") == ("2026-12-01", "2027-01-01")
    assert expiry_bounds(" 2026-02 ") == ("2026-02-01", "2026-03-01")
    for bad in ("2026-13", "2026-00", "26-11", "tomorrow", "2026-02-30"):
        with pytest.raises(ToolInputError):
            expiry_bounds(bad)


def test_search_sql_only_ever_binds_user_text():
    sql, params = build_search("a'b", exchange="zzz", expiry="2026-11")
    assert "a'b" not in sql and "ZZZ" not in sql
    assert params["w0"] == "%A'B%" and params["exchange"] == "ZZZ"


# --- when is the cache stale ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("loaded", "now", "stale"),
    [
        (ist(4, 10), ist(4, 15), False),  # same day
        (ist(4, 10), ist(5, 7, 59), False),  # the next dump is not out yet
        (ist(4, 10), ist(5, 8, 29), False),
        (ist(4, 10), ist(5, 8, 30), True),  # ... and now it is
        (ist(4, 10), ist(5, 23), True),
        (ist(4, 8, 20), ist(4, 8, 29), False),  # loaded just before the dump was published ...
        (ist(4, 8, 20), ist(4, 8, 30), True),  # ... so it goes stale the moment it is
        (ist(4, 7), ist(5, 7), True),  # a load before 08:30 holds the previous day's dump: stale at the next check
        (ist(6, 10), ist(8, 7), True),  # two days old, although it is before 08:30
        (ist(6, 10), ist(11, 12), True),
    ],
)
def test_staleness_rule(loaded, now, stale):
    assert is_stale(loaded, now) is stale


def test_staleness_is_judged_in_ist():
    # 04:30 UTC is 10:00 IST
    assert not is_stale(dt.datetime(2026, 11, 4, 4, 30, tzinfo=dt.UTC), dt.datetime(2026, 11, 5, 2, 0, tzinfo=dt.UTC))
    assert is_stale(dt.datetime(2026, 11, 4, 4, 30, tzinfo=dt.UTC), dt.datetime(2026, 11, 5, 3, 0, tzinfo=dt.UTC))
    assert latest_publication(ist(4, 9)) == ist(4, 8, 30)
    assert latest_publication(ist(4, 8)) == ist(3, 8, 30)
    assert next_publication(ist(4, 10)) == ist(5, 8, 30)
    assert next_publication(ist(4, 8, 30)) == ist(5, 8, 30)


async def test_cache_is_reloaded_after_the_next_daily_publication(store, fake_kite, clock):
    serve_dumps(fake_kite)
    await store.lookup("NSE", "INFY")
    clock.now = ist(4, 23, 59)
    await store.lookup("NSE", "INFY")
    clock.now = ist(5, 8, 29)
    await store.lookup("NSE", "INFY")
    assert downloads(fake_kite) == 1
    status = await store.status()
    assert status["stale"] is False and status["instruments"]["next_refresh"].startswith("2026-11-05T08:30")

    serve_dumps(fake_kite, rows=[*DUMP_ROWS, inst_row("NEWCO", 999001, name="NEW LISTING")])  # Kite's new dump
    clock.now = ist(5, 8, 30)
    assert await store.lookup("NSE", "NEWCO") is not None
    assert downloads(fake_kite) == 2
    status = await store.status()
    assert status["stale"] is False and status["instruments"]["loaded_at"] == "2026-11-05T08:30:00+05:30"
    assert status["instruments"]["rows"] == len(DUMP_ROWS) + 1


async def test_refresh_replaces_the_cache_file_atomically(store, fake_kite, clock):
    serve_dumps(fake_kite)
    await store.lookup("NSE", "INFY")
    path = store.cache_file
    inode = path.stat().st_ino
    reader = sqlite3.connect(path)  # somebody is reading the old file while it is replaced
    try:
        serve_dumps(fake_kite, rows=[inst_row("ONLYNEW", 5, name="ONLY IN THE NEW DUMP")])
        clock.now = ist(5, 9)
        assert await store.lookup("NSE", "ONLYNEW") is not None
        assert await store.lookup("NSE", "INFY") is None  # the old rows are gone from the new file
        assert path.stat().st_ino != inode  # a new file was renamed over it
        assert reader.execute("SELECT count(*) FROM instruments").fetchone()[0] == len(DUMP_ROWS)  # old one intact
    finally:
        reader.close()
    assert [p.name for p in path.parent.iterdir()] == ["instruments.sqlite"]


async def test_a_broken_dump_leaves_the_old_file_untouched(store, fake_kite, clock):
    serve_dumps(fake_kite)
    await store.lookup("NSE", "INFY")
    before = store.cache_file.read_bytes()
    clock.now = ist(5, 9)
    serve_dumps(fake_kite, rows=[], header=["foo", "bar"])
    assert await store.lookup("NSE", "INFY") is not None  # still served
    assert store.cache_file.read_bytes() == before
    assert [p.name for p in store.cache_file.parent.iterdir()] == ["instruments.sqlite"]
    assert "columns" in (await store.status())["instruments"]["last_error"]


# --- failures -------------------------------------------------------------------------------------------------------


async def test_failed_refresh_keeps_serving_the_old_copy_and_says_so(store, fake_kite, clock):
    serve_dumps(fake_kite)
    await store.lookup("NSE", "INFY")
    clock.now = ist(5, 9)
    fake_kite.on("GET", "/instruments", status=500, error_type="GeneralException", message="dump generation failed")

    infy = await store.lookup("NSE", "INFY")
    assert infy is not None and infy.tradingsymbol == "INFY"  # the day-old copy is still served
    status = await store.status()
    assert status["loaded"] is True and status["stale"] is True
    assert "dump generation failed" in status["instruments"]["last_error"]
    assert status["instruments"]["loaded_at"] == "2026-11-04T10:00:00+05:30"
    assert await store.search("tata") != []

    # a miss on an out-of-date list proves nothing, so it is not reported as "unknown symbol"
    with pytest.raises(InstrumentsUnavailable, match="could not be refreshed"):
        await store.lookup("NSE", "NEWCO")
    with pytest.raises(InstrumentsUnavailable):
        await store.by_token(999001)

    attempts = downloads(fake_kite)
    for _ in range(5):
        await store.lookup("NSE", "INFY")
    assert downloads(fake_kite) == attempts  # no hammering Kite while it is down

    clock.now = ist(5, 9, 6)  # the back-off has passed, and Kite is back
    serve_dumps(fake_kite, rows=[*DUMP_ROWS, inst_row("NEWCO", 999001)])
    assert await store.lookup("NSE", "NEWCO") is not None
    status = await store.status()
    assert status["stale"] is False and "last_error" not in status["instruments"]


async def test_no_cache_and_no_kite_means_instruments_unavailable(store, fake_kite):
    def unreachable(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("refused", request=request)

    fake_kite.on("GET", "/instruments", handler=unreachable)
    with pytest.raises(InstrumentsUnavailable, match="no cached copy"):
        await store.lookup("NSE", "INFY")
    with pytest.raises(InstrumentsUnavailable):
        await store.search("nifty")
    with pytest.raises(InstrumentsUnavailable):
        await store.by_token(1)
    status = await store.status()
    assert status["loaded"] is False and "could not connect" in status["instruments"]["last_error"]

    attempts = downloads(fake_kite)
    with pytest.raises(InstrumentsUnavailable):
        await store.lookup("NSE", "INFY")
    assert downloads(fake_kite) == attempts  # the failure is remembered for a few minutes

    serve_dumps(fake_kite)
    with pytest.raises(InstrumentsUnavailable):
        await store.lookup("NSE", "INFY")  # still inside the back-off ...
    assert (await store.refresh(force=True))["refreshed"] == ["instruments", "mf_instruments"]  # ... unless asked
    assert await store.lookup("NSE", "INFY") is not None
    assert "last_error" not in (await store.status())["instruments"]


async def test_back_off_ends_by_itself(store, fake_kite, clock):
    fake_kite.on("GET", "/instruments", status=500, error_type="GeneralException", message="down")
    with pytest.raises(InstrumentsUnavailable):
        await store.lookup("NSE", "INFY")
    serve_dumps(fake_kite)
    clock.now = FIXED_NOW + dt.timedelta(minutes=6)
    assert await store.lookup("NSE", "INFY") is not None


async def test_a_hanging_download_times_out(store, monkeypatch):
    async def hang(path: str) -> str:
        await asyncio.sleep(3600)
        return ""

    monkeypatch.setattr(store._client, "get_csv", hang)
    monkeypatch.setattr(instruments_module, "FETCH_TIMEOUT_S", 0.05)
    with pytest.raises(InstrumentsUnavailable, match="timed out"):
        await store.lookup("NSE", "INFY")


@pytest.mark.parametrize(
    ("header", "rows", "message"),
    [
        (["foo", "bar"], [], "columns"),
        (HEADER, [], "no usable rows"),
        (HEADER[:-1], [["1"] * 11], "exchange"),
        (HEADER, [["x", *inst_row("A", 1)[1:]], ["y", *inst_row("B", 2)[1:]], inst_row("C", 3)], "looks broken"),
    ],
    ids=["wrong columns", "no rows", "missing column", "mostly garbage"],
)
async def test_unusable_dumps_are_refused(store, fake_kite, header, rows, message):
    serve_dumps(fake_kite, rows=rows, header=header)
    with pytest.raises(InstrumentsUnavailable, match=message):
        await store.lookup("NSE", "INFY")
    assert not store.cache_file.exists()


async def test_a_few_unreadable_rows_are_skipped_and_counted(store, fake_kite):
    rows = [*DUMP_ROWS, ["not-a-number", *inst_row("BROKEN", 1)[1:]], inst_row("BADTICK", 2, tick="-1"), ["12345"]]
    serve_dumps(fake_kite, rows=rows)
    assert await store.lookup("NSE", "INFY") is not None
    assert await store.lookup("NSE", "BROKEN") is None
    status = await store.status()
    assert status["instruments"]["rows"] == len(DUMP_ROWS) and status["instruments"]["unreadable_rows"] == 3


async def test_whole_numbers_may_be_written_with_a_decimal_point(store, fake_kite):
    row = inst_row("DOTTED", 9, exchange="MCX", segment="MCX-FUT", itype="FUT", lot="100.0", expiry="2026-12-04")
    serve_dumps(fake_kite, rows=[row])
    dotted = await store.lookup("MCX", "DOTTED")
    assert dotted is not None and dotted.lot_size == 100 and isinstance(dotted.lot_size, int)


async def test_float_noise_in_a_tick_size_is_removed(store, fake_kite):
    noisy = inst_row("NOISY", 9, tick="0.0500000007450580596923828125")
    serve_dumps(fake_kite, rows=[noisy, inst_row("COARSE", 10, tick="5"), inst_row("FINE", 11, tick="5E-4")])
    found = {sym: await store.lookup("NSE", sym) for sym in ("NOISY", "COARSE", "FINE")}
    ticks = {sym: inst.tick_size for sym, inst in found.items() if inst is not None}
    assert ticks == {"NOISY": Decimal("0.05"), "COARSE": Decimal(5), "FINE": Decimal("0.0005")}


async def test_rows_without_a_symbol_or_an_exchange_are_skipped(store, fake_kite):
    serve_dumps(fake_kite, rows=[*DUMP_ROWS, inst_row("", 3), inst_row("NOEXCHANGE", 4, exchange="")])
    assert await store.lookup("NSE", "INFY") is not None
    assert await store.lookup("", "NOEXCHANGE") is None
    status = await store.status()
    assert status["instruments"]["rows"] == len(DUMP_ROWS) and status["instruments"]["unreadable_rows"] == 2


async def test_missing_optional_values_get_safe_defaults(store, fake_kite):
    row = ["77", "", "ODD", "", "", "", "", "", "", "EQ", "NSE", "NSE"]
    serve_dumps(fake_kite, rows=[row])
    odd = await store.lookup("NSE", "ODD")
    assert odd is not None
    assert (odd.exchange_token, odd.last_price, odd.strike, odd.expiry) == (0, 0.0, 0.0, None)
    assert odd.tick_size == Decimal(0) and odd.lot_size == 1  # "unknown": the order tools skip those checks


async def test_a_damaged_cache_file_is_rebuilt(store, fake_kite):
    serve_dumps(fake_kite)
    store.cache_file.parent.mkdir(parents=True, exist_ok=True)
    store.cache_file.write_bytes(b"this is not a database")
    assert await store.lookup("NSE", "INFY") is not None
    assert downloads(fake_kite) == 1


@pytest.mark.parametrize("damage", ["garbage", "deleted"])
async def test_a_cache_file_damaged_while_running_is_rebuilt(store, fake_kite, damage):
    serve_dumps(fake_kite)
    await store.lookup("NSE", "INFY")
    if damage == "garbage":
        store.cache_file.write_bytes(b"x" * 5000)
    else:
        store.cache_file.unlink()
    assert await store.lookup("NSE", "TCS") is not None
    assert downloads(fake_kite) == 2


async def test_a_cache_file_from_another_schema_version_is_replaced(store, fake_kite):
    serve_dumps(fake_kite)
    write_cache(store.cache_file, schema="0", meta={"instruments.loaded_at": "2026-11-04T10:00:00+05:30"})
    assert await store.lookup("NSE", "INFY") is not None
    assert downloads(fake_kite) == 1  # the old file's claim to be current was not believed
    with contextlib.closing(sqlite3.connect(store.cache_file)) as con:
        assert con.execute("SELECT value FROM meta WHERE key = 'schema'").fetchone()[0] == "1"


async def test_a_query_that_keeps_failing_gives_up_after_one_reload(store, fake_kite, monkeypatch):
    serve_dumps(fake_kite)
    await store.lookup("NSE", "INFY")

    def broken(sql, params):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(store, "_select", broken)
    with pytest.raises(InstrumentsUnavailable, match="even after reloading"):
        await store.lookup("NSE", "INFY")


async def test_leftovers_of_an_interrupted_build_are_cleaned_up(store, fake_kite):
    serve_dumps(fake_kite)
    folder = store.cache_file.parent
    folder.mkdir(parents=True)
    old, recent, other = folder / ".instruments-old.tmp", folder / ".instruments-recent.tmp", folder / "notes.txt"
    for path in (old, recent, other):
        path.write_text("x")
    two_days_ago = time.time() - 2 * 24 * 3600
    os.utime(old, (two_days_ago, two_days_ago))
    await store.lookup("NSE", "INFY")
    assert sorted(p.name for p in folder.iterdir()) == [".instruments-recent.tmp", "instruments.sqlite", "notes.txt"]


# --- the two dumps are independent ----------------------------------------------------------------------------------


async def test_a_damaged_second_dump_in_the_old_file_is_dropped_and_fetched_again(store, fake_kite):
    serve_dumps(fake_kite)
    meta = {"mf_instruments.loaded_at": "2026-11-04T10:00:00+05:30", "mf_instruments.rows": "5"}
    write_cache(store.cache_file, schema="1", meta=meta)  # claims to hold a current MF list, but has no table for it
    assert (await store.status())["mf_instruments"]["loaded"] is True
    assert await store.lookup("NSE", "INFY") is not None  # building the new file cannot carry the MF list across ...
    assert (await store.status())["mf_instruments"] == {"loaded": False}
    assert downloads(fake_kite, "/mf/instruments") == 0
    assert await store.lookup_mf("INF846K01DP8") is not None  # ... so it is simply downloaded when asked for
    assert downloads(fake_kite, "/mf/instruments") == 1 and downloads(fake_kite) == 1


async def test_a_failing_mf_dump_does_not_block_the_instrument_list(store, fake_kite, clock):
    serve_dumps(fake_kite)
    fake_kite.on("GET", "/mf/instruments", status=500, error_type="GeneralException", message="mf down")
    assert await store.lookup("NSE", "INFY") is not None
    with pytest.raises(InstrumentsUnavailable, match="mutual fund list"):
        await store.lookup_mf("INF846K01DP8")
    status = await store.status()
    assert status["loaded"] is True
    assert status["mf_instruments"]["loaded"] is False and "mf down" in status["mf_instruments"]["last_error"]

    serve_dumps(fake_kite)
    clock.now = FIXED_NOW + dt.timedelta(minutes=6)
    assert await store.lookup_mf("INF846K01DP8") is not None
    assert downloads(fake_kite) == 1  # the instrument list was not downloaded again
    assert await store.lookup("NSE", "INFY") is not None  # ... and survived the rebuild of the cache file
    status = await store.status()
    assert status["instruments"]["loaded"] and status["mf_instruments"]["loaded"]


async def test_refreshing_one_dump_carries_the_other_across(store, fake_kite, clock):
    serve_dumps(fake_kite)
    await store.lookup_mf("INF846K01DP8")
    await store.lookup("NSE", "INFY")
    mf_loaded = (await store.status())["mf_instruments"]["loaded_at"]
    clock.now = ist(5, 9)
    serve_dumps(fake_kite, rows=[*DUMP_ROWS, inst_row("NEWCO", 999001)])
    assert await store.lookup("NSE", "NEWCO") is not None  # only the instrument list is refreshed here
    assert downloads(fake_kite, "/mf/instruments") == 1
    assert count_rows(store.cache_file, "mf_instruments") == len(MF_ROWS)
    status = await store.status()
    assert status["mf_instruments"]["loaded_at"] == mf_loaded and status["mf_instruments"]["stale"] is True


# --- refresh() and status() -----------------------------------------------------------------------------------------


async def test_status_does_not_wait_for_a_download(store, fake_kite, monkeypatch):
    serve_dumps(fake_kite)
    started, gate = asyncio.Event(), asyncio.Event()
    real_get_csv = store._client.get_csv

    async def slow(path: str) -> str:
        started.set()
        await gate.wait()
        return await real_get_csv(path)

    monkeypatch.setattr(store._client, "get_csv", slow)
    task = asyncio.create_task(store.lookup("NSE", "INFY"))
    await asyncio.wait_for(started.wait(), 5)
    status = await asyncio.wait_for(store.status(), 5)  # the download is under way and status answers anyway
    assert status["refreshing"] is True and status["loaded"] is False
    gate.set()
    assert await task is not None
    assert (await store.status())["refreshing"] is False


async def test_status_does_not_wait_for_a_running_refresh_either(store, fake_kite, monkeypatch):
    serve_dumps(fake_kite)
    await store.lookup("NSE", "INFY")
    started, gate = asyncio.Event(), asyncio.Event()
    real_get_csv = store._client.get_csv

    async def slow(path: str) -> str:
        started.set()
        await gate.wait()
        return await real_get_csv(path)

    monkeypatch.setattr(store._client, "get_csv", slow)
    refreshing = asyncio.create_task(store.refresh(force=True))
    await asyncio.wait_for(started.wait(), 5)
    store._opened = False  # as after a damaged cache file: the file's bookkeeping has to be read again ...
    status = await asyncio.wait_for(store.status(), 5)  # ... and that does not queue behind the download
    assert status["refreshing"] is True and status["loaded"] is True
    gate.set()
    assert (await refreshing)["refreshed"] == ["instruments", "mf_instruments"]


async def test_status_before_anything_is_loaded(store, fake_kite):
    status = await store.status()
    assert status["loaded"] is False and status["stale"] is False and status["refreshing"] is False
    assert status["cache_file"] == str(store.cache_file)
    assert status["instruments"] == {"loaded": False} and status["mf_instruments"] == {"loaded": False}
    assert fake_kite.requests == []  # status never downloads


async def test_status_after_loading(store, fake_kite):
    serve_dumps(fake_kite)
    await store.refresh()
    status = await store.status()
    assert status["instruments"]["rows"] == len(DUMP_ROWS)
    assert status["instruments"]["exchanges"] == dict(Counter(row[11] for row in DUMP_ROWS))
    assert status["instruments"]["loaded_at"] == "2026-11-04T10:00:00+05:30"
    assert status["mf_instruments"]["rows"] == len(MF_ROWS) and "exchanges" not in status["mf_instruments"]


async def test_refresh_loads_what_is_missing_or_stale_and_force_reloads_everything(store, fake_kite, clock):
    serve_dumps(fake_kite)
    first = await store.refresh()
    assert first["refreshed"] == ["instruments", "mf_instruments"] and first["loaded"] is True
    assert (await store.refresh())["refreshed"] == []  # both are current
    assert (downloads(fake_kite), downloads(fake_kite, "/mf/instruments")) == (1, 1)
    assert (await store.refresh(force=True))["refreshed"] == ["instruments", "mf_instruments"]
    assert (downloads(fake_kite), downloads(fake_kite, "/mf/instruments")) == (2, 2)
    clock.now = ist(5, 9)
    assert (await store.refresh())["refreshed"] == ["instruments", "mf_instruments"]


async def test_refresh_reports_a_failed_dump_and_raises_only_without_any_instrument_list(
    runtime_factory, fake_kite, clock, tmp_path
):
    rt = runtime_factory()
    store = InstrumentStore(rt.settings, rt.client, clock=clock)
    serve_dumps(fake_kite)
    fake_kite.on("GET", "/mf/instruments", status=500, error_type="GeneralException", message="mf down")
    answer = await store.refresh()
    assert answer["refreshed"] == ["instruments"] and "mf down" in answer["mf_instruments"]["last_error"]

    fake_kite.on("GET", "/instruments", status=500, error_type="GeneralException", message="all down")
    clock.now = ist(5, 9)
    answer = await store.refresh()  # the old instrument list is still there
    assert answer["refreshed"] == [] and answer["stale"] is True and "all down" in answer["instruments"]["last_error"]

    elsewhere = runtime_factory(KITE_MCP_CACHE_DIR=str(tmp_path / "other-cache"))
    empty = InstrumentStore(elsewhere.settings, elsewhere.client, clock=clock)
    with pytest.raises(InstrumentsUnavailable, match="all down"):
        await empty.refresh()


# --- mutual funds ---------------------------------------------------------------------------------------------------


async def test_mf_lookup_and_types(store, fake_kite):
    serve_dumps(fake_kite)
    fund = await store.lookup_mf("inf846k01ew2")
    assert fund is not None
    assert fund.name == "Axis Long Term Equity Fund - Direct Growth" and fund.amc == "AXISMUTUALFUND_MF"
    assert fund.purchase_allowed is True and fund.redemption_allowed is False
    assert fund.minimum_purchase_amount == Decimal("500.0") and fund.purchase_amount_multiplier == Decimal("500.0")
    assert fund.redemption_quantity_multiplier == Decimal("0.001")
    assert (fund.scheme_type, fund.plan, fund.dividend_type, fund.settlement_type) == ("elss", "direct", "growth", "T3")
    assert fund.last_price == Decimal("98.5") and fund.last_price_date == dt.date(2026, 11, 3)
    assert fund.as_dict()["last_price_date"] == "2026-11-03" and fund.as_dict()["minimum_purchase_amount"] == 500.0
    assert await store.lookup_mf("INF000000000") is None
    liquid = await store.lookup_mf("INF179K01XQ0")
    assert liquid is not None and liquid.purchase_allowed is False and liquid.last_price_date is None


async def test_mf_search(store, fake_kite):
    serve_dumps(fake_kite)
    names = [f.tradingsymbol for f in await store.search_mf("axis")]
    assert sorted(names) == ["INF846K01131", "INF846K01DP8", "INF846K01EW2"]
    assert [f.tradingsymbol for f in await store.search_mf("INF846K01DP8")] == ["INF846K01DP8"]  # the ISIN
    assert [f.tradingsymbol for f in await store.search_mf("bluechip axis", plan="direct")] == ["INF846K01DP8"]
    assert [f.tradingsymbol for f in await store.search_mf("", scheme_type="ELSS")] == ["INF846K01EW2"]
    assert [f.tradingsymbol for f in await store.search_mf("", amc="kotak")] == ["INF174K01LS2"]
    assert [f.tradingsymbol for f in await store.search_mf("", plan="regular")] == ["INF846K01131"]
    assert len(await store.search_mf("fund", limit=2)) == 2
    assert await store.search_mf("zzz") == []
    with pytest.raises(ToolInputError):
        await store.search_mf("")


async def test_mf_search_ranks_name_prefix_before_substring(store, fake_kite):
    mf = [
        "INF000A01001,ACMEMF,Zeta Bluechip Fund,1,1,100.0,1.0,100.0,1.0,0.001,growth,equity,direct,T1,10.0,2026-11-03",
        "INF000A01002,ACMEMF,Bluechip Alpha Fund,1,1,100.0,1.0,100.0,1.0,0.001,growth,equity,direct,T1,10.0,2026-11-03",
        "INF000A01003,ACMEMF,Bluechip Beta Fund,1,1,100.0,1.0,100.0,1.0,0.001,growth,equity,direct,T1,10.0,2026-11-03",
    ]
    serve_dumps(fake_kite, mf=mf)
    found = [f.tradingsymbol for f in await store.search_mf("bluechip")]
    assert found == ["INF000A01002", "INF000A01003", "INF000A01001"]


async def test_a_garbled_nav_date_does_not_drop_the_fund(store, fake_kite):
    mf = ["INF000A01001,ACMEMF,Dated Fund,1,1,100.0,1.0,100.0,1.0,0.001,growth,equity,direct,T1,10.0,03/11/2026"]
    serve_dumps(fake_kite, mf=mf)
    fund = await store.lookup_mf("INF000A01001")
    assert fund is not None and fund.last_price == Decimal("10.0") and fund.last_price_date is None


async def test_mf_dump_only_needs_its_identity_columns(store, fake_kite):
    text = "tradingsymbol,name\nINF000A01001,Plain Fund\n"
    fake_kite.on("GET", "/mf/instruments", body=text.encode(), content_type="text/csv")
    fund = await store.lookup_mf("INF000A01001")
    assert fund is not None and fund.minimum_purchase_amount == Decimal(0) and fund.plan == ""
    assert fund.purchase_allowed is False and fund.last_price_date is None
    assert downloads(fake_kite) == 0  # the MF question did not pull the instrument list
