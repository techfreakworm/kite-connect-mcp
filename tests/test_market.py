"""The market-data tools (quotes, historical candles) and the instrument tools, end to end through an MCP client.

Kite is FakeKite; the instrument dumps come from test_instruments.py. The client's rate limiter runs on virtual time,
so a call that makes dozens of historical requests does not really wait.
"""

from __future__ import annotations

import datetime as dt

import httpx2
import pytest
from mcp import Client

from conftest import FIXED_NOW, tool_json, tool_text
from kite_connect_mcp.client.ratelimit import RateLimiter
from kite_connect_mcp.errors import ToolInputError
from kite_connect_mcp.server import build_runtime, build_server
from kite_connect_mcp.tools import market
from test_instruments import DUMP_ROWS, MF_ROWS, serve_dumps

NOW = "2026-11-04 10:00:00"  # FIXED_NOW, the way Kite writes it
INFY_TOKEN = 408065
NIFTY_FUT_TOKEN = 12517890


@pytest.fixture
def make_server(fake_kite, settings_factory):
    """(server, registrar, runtime) wired to FakeKite, with a rate limiter that waits in virtual time."""

    def make(**overrides: str | None):
        virtual = [0.0]

        async def sleep(seconds: float) -> None:
            virtual[0] += seconds

        rt = build_runtime(
            settings_factory(**overrides),
            transport=fake_kite.transport(),
            clock=lambda: FIXED_NOW,
            sleep=sleep,
            limiter=RateLimiter(clock=lambda: virtual[0], sleep=sleep),
        )
        server, reg = build_server(rt)
        return server, reg, rt

    return make


async def call(server, name, args=None):
    async with Client(server) as client:
        return await client.call_tool(name, args or {})


# --- the tools that exist ---------------------------------------------------------------------------------------------

EXPECTED_TOOLS = {
    "get_quotes": ("market", "read"),
    "get_ohlc": ("market", "read"),
    "get_ltp": ("market", "read"),
    "get_historical_data": ("market", "read"),
    "search_instruments": ("market", "read"),
    "get_instrument": ("market", "read"),
    "get_instruments_status": ("market", "read"),
    "refresh_instruments": ("market", "local"),
    "search_mf_instruments": ("mf", "read"),
}


async def test_tools_and_their_kinds(make_server):
    _, reg, _ = make_server()
    kinds = {info.name: (info.toolset, info.kind) for info in reg.registered}
    assert {name: kinds[name] for name in kinds if name in EXPECTED_TOOLS} == EXPECTED_TOOLS


async def test_toolsets_decide_which_of_them_exist(make_server):
    _, reg, _ = make_server(KITE_MCP_TOOLSETS="market")
    names = {info.name for info in reg.registered}
    assert set(EXPECTED_TOOLS) - {"search_mf_instruments"} <= names and "search_mf_instruments" not in names
    _, reg, _ = make_server(KITE_MCP_TOOLSETS="mf")
    names = {info.name for info in reg.registered}
    assert "search_mf_instruments" in names and not {"get_quotes", "search_instruments"} & names


async def test_read_only_mode_keeps_every_one_of_them(make_server):
    _, reg, _ = make_server(KITE_MCP_READ_ONLY="true")  # none of them changes the account
    assert set(EXPECTED_TOOLS) <= {info.name for info in reg.registered}


async def test_the_tool_schemas_carry_the_limits(make_server):
    server, _, _ = make_server()
    async with Client(server) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert tools["get_quotes"].input_schema["properties"]["instruments"]["maxItems"] == 500
    assert tools["get_ohlc"].input_schema["properties"]["instruments"]["maxItems"] == 1000
    assert tools["get_ltp"].input_schema["properties"]["instruments"]["maxItems"] == 1000
    history = tools["get_historical_data"].input_schema
    assert history["properties"]["interval"]["enum"] == list(market.MAX_DAYS_PER_REQUEST)
    assert set(history["required"]) == {"instrument", "interval", "from_date", "to_date"}
    assert tools["search_instruments"].input_schema["properties"]["limit"]["maximum"] == 200
    assert tools["refresh_instruments"].annotations.read_only_hint is False
    assert tools["get_quotes"].annotations.read_only_hint is True


# --- quotes -----------------------------------------------------------------------------------------------------------

INFY_QUOTE = {
    "instrument_token": INFY_TOKEN,
    "last_price": 1412.95,
    "ohlc": {"open": 1396, "high": 1421.75, "low": 1395.55, "close": 1389.65},
}


async def test_get_quotes_sends_one_i_parameter_per_instrument(make_server, fake_kite):
    fake_kite.on("GET", "/quote", {"NSE:INFY": INFY_QUOTE, "NSE:NIFTY 50": {"instrument_token": 256265}})
    server, _, _ = make_server()
    args = {"instruments": ["nse:infy", "NSE:NIFTY 50", "NSE:INFY", "NFO:GONE25NOVFUT"]}
    answer = tool_json(await call(server, "get_quotes", args))
    (request,) = fake_kite.calls("GET", "/quote")
    assert request.url.params.get_list("i") == ["NSE:INFY", "NSE:NIFTY 50", "NFO:GONE25NOVFUT"]  # tidied, no repeats
    assert answer["quotes"]["NSE:INFY"]["last_price"] == 1412.95
    assert answer["missing"] == ["NFO:GONE25NOVFUT"]  # Kite leaves such keys out; the tool says so


@pytest.mark.parametrize(
    ("tool", "path"), [("get_quotes", "/quote"), ("get_ohlc", "/quote/ohlc"), ("get_ltp", "/quote/ltp")]
)
async def test_every_quote_tool_asks_the_matching_endpoint(make_server, fake_kite, tool, path):
    fake_kite.on("GET", path, {"NSE:INFY": INFY_QUOTE, "BSE:SENSEX": {"instrument_token": 265, "last_price": 80000.5}})
    server, _, _ = make_server()
    answer = tool_json(await call(server, tool, {"instruments": ["NSE:INFY", "BSE:SENSEX"]}))
    assert answer == {
        "quotes": {"NSE:INFY": INFY_QUOTE, "BSE:SENSEX": {"instrument_token": 265, "last_price": 80000.5}},
        "missing": [],
    }
    assert fake_kite.calls("GET", path)[0].url.params.get_list("i") == ["NSE:INFY", "BSE:SENSEX"]


@pytest.mark.parametrize(
    ("tool", "path", "limit"),
    [("get_quotes", "/quote", 500), ("get_ohlc", "/quote/ohlc", 1000), ("get_ltp", "/quote/ltp", 1000)],
)
async def test_instrument_count_limits(make_server, fake_kite, tool, path, limit):
    fake_kite.on("GET", path, {})
    server, _, _ = make_server()
    at_limit = [f"NSE:SYM{i}" for i in range(limit)]
    answer = tool_json(await call(server, tool, {"instruments": at_limit}))
    assert fake_kite.calls("GET", path)[0].url.params.get_list("i") == at_limit
    assert answer["missing"] == at_limit

    fake_kite.requests.clear()
    too_many = await call(server, tool, {"instruments": [*at_limit, "NSE:ONEMORE"]})
    assert too_many.is_error
    assert (await call(server, tool, {"instruments": []})).is_error
    assert fake_kite.requests == []  # refused before anything was sent


def test_instrument_count_is_also_checked_inside_the_tool():
    with pytest.raises(ToolInputError, match=r"between 1 and 500 .*got 501"):
        market.quote_keys([f"NSE:S{i}" for i in range(501)], 500)
    assert market.quote_keys(["NSE:A", "nse:a", "NSE:B"], 500) == ["NSE:A", "NSE:B"]


@pytest.mark.parametrize("bad", ["INFY", ":INFY", "NSE:", "  "])
async def test_instruments_must_be_exchange_colon_symbol(make_server, fake_kite, bad):
    server, _, _ = make_server()
    result = await call(server, "get_ltp", {"instruments": ["NSE:INFY", bad]})
    assert result.is_error and "EXCHANGE:TRADINGSYMBOL" in tool_text(result)
    assert fake_kite.requests == []


async def test_quotes_without_the_paid_plan_say_so(make_server, fake_kite):
    fake_kite.on("GET", "/quote/ltp", status=403, error_type="PermissionException", message="Insufficient permission")
    server, _, rt = make_server()
    result = await call(server, "get_ltp", {"instruments": ["NSE:INFY"]})
    assert result.is_error and "paid Kite Connect plan" in tool_text(result)
    assert rt.tokens.current() is not None  # a PermissionException is not a dead session


# --- historical candles -----------------------------------------------------------------------------------------------


def stamp(moment: dt.datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S+0530")


def moment(text: str) -> dt.datetime:
    """A `from` / `to` query value as a datetime."""
    return dt.datetime.strptime(text, "%Y-%m-%d %H:%M:%S")


class Candles:
    """FakeKite's historical endpoint for one instrument and interval. It answers with a candle at 09:15 on every
    calendar day inside the requested range (in the shape Kite sends) and remembers the query of each request."""

    def __init__(self, fake, token: int = INFY_TOKEN, interval: str = "minute", *, transform=None):
        self.queries: list[dict[str, str]] = []
        self.transform = transform
        fake.on("GET", f"/instruments/historical/{token}/{interval}", handler=self._handle)

    def _handle(self, request: httpx2.Request) -> httpx2.Response:
        query = dict(request.url.params)
        self.queries.append(query)
        start, end = moment(query["from"]), moment(query["to"])
        candles = []
        day = start.date()
        while day <= end.date():
            at = dt.datetime.combine(day, dt.time(9, 15))
            if start <= at <= end:
                n = day.toordinal()
                row = [stamp(at), n % 100 + 1000.0, n % 100 + 1001.5, n % 100 + 999.5, n % 100 + 1000.5, n % 1000]
                if query["oi"] == "1":
                    row.append(n * 10)
                candles.append(row)
            day += dt.timedelta(days=1)
        if self.transform:
            candles = self.transform(candles, query)
        return httpx2.Response(200, json={"status": "success", "data": {"candles": candles}})

    @property
    def spans(self) -> list[tuple[str, str]]:
        return [(q["from"], q["to"]) for q in self.queries]


def history_args(**changes):
    args = {"instrument": "NSE:INFY", "interval": "minute", "from_date": "2026-10-01", "to_date": "2026-10-10"}
    return {**args, **changes}


async def test_one_request_for_a_short_range(make_server, fake_kite):
    serve_dumps(fake_kite)
    history = Candles(fake_kite)
    server, _, _ = make_server()
    answer = tool_json(await call(server, "get_historical_data", history_args()))
    assert history.queries == [
        {"from": "2026-10-01 00:00:00", "to": "2026-10-10 23:59:59", "continuous": "0", "oi": "0"}
    ]
    assert fake_kite.calls("GET", "/instruments/historical/408065/minute")[0].headers["X-Kite-Version"] == "3"
    assert {k: v for k, v in answer.items() if k != "candles"} == {
        "instrument": "NSE:INFY",
        "instrument_token": INFY_TOKEN,
        "interval": "minute",
        "from": "2026-10-01 00:00:00",
        "to": "2026-10-10 23:59:59",
        "columns": ["date", "open", "high", "low", "close", "volume"],
        "count": 10,
    }
    assert len(answer["candles"]) == 10
    assert answer["candles"][0][0] == "2026-10-01T09:15:00+0530" and len(answer["candles"][0]) == 6
    assert answer["candles"][-1][0] == "2026-10-10T09:15:00+0530"


async def test_long_ranges_are_split_to_what_kite_accepts_and_merged_in_order(make_server, fake_kite):
    serve_dumps(fake_kite)
    history = Candles(fake_kite)
    server, _, _ = make_server()
    answer = tool_json(
        await call(server, "get_historical_data", history_args(from_date="2026-05-01", to_date="2026-10-31"))
    )
    assert history.spans == [  # newest first; each range is under the 60 days Kite allows for minute candles
        ("2026-10-28 00:00:00", "2026-10-31 23:59:59"),
        ("2026-08-29 00:00:00", "2026-10-27 23:59:59"),
        ("2026-06-30 00:00:00", "2026-08-28 23:59:59"),
        ("2026-05-01 00:00:00", "2026-06-29 23:59:59"),
    ]
    for start, end in history.spans:
        assert moment(end) - moment(start) < dt.timedelta(days=60)
    dates = [candle[0] for candle in answer["candles"]]
    assert answer["count"] == len(dates) == 184  # 1 May .. 31 Oct, every day once
    assert dates == sorted(set(dates))  # oldest first, no duplicates
    assert dates[0] == "2026-05-01T09:15:00+0530" and dates[-1] == "2026-10-31T09:15:00+0530"
    assert "truncated" not in answer and "note" not in answer


async def test_range_edges_that_kite_reports_twice_and_unsorted_answers_are_cleaned(make_server, fake_kite):
    def noisy(candles, query):
        if query["to"] != "2026-10-31 23:59:59":  # the neighbouring (newer) range's first candle again
            following = dt.datetime.strptime(query["to"][:10], "%Y-%m-%d") + dt.timedelta(days=1, hours=9, minutes=15)
            candles = [*candles, [stamp(following), 1, 2, 0.5, 1.5, 7]]
        return [*reversed(candles), candles[0]]  # no promise about order, and a repeated candle

    serve_dumps(fake_kite)
    Candles(fake_kite, transform=noisy)
    server, _, _ = make_server()
    answer = tool_json(
        await call(server, "get_historical_data", history_args(from_date="2026-05-01", to_date="2026-10-31"))
    )
    dates = [candle[0] for candle in answer["candles"]]
    assert answer["count"] == len(dates) == 184
    assert dates == sorted(set(dates))
    boundary = [c for c in answer["candles"] if c[0] == "2026-10-28T09:15:00+0530"]
    assert len(boundary) == 1 and boundary[0][1] >= 1000  # the real candle, not the stray one (open 1)


@pytest.mark.parametrize(
    ("interval", "requests"),
    [
        ("minute", 7),
        ("3minute", 4),
        ("5minute", 4),
        ("10minute", 4),
        ("15minute", 2),
        ("30minute", 2),
        ("60minute", 1),
        ("day", 1),
    ],
)
async def test_each_interval_is_split_by_its_own_limit(make_server, fake_kite, interval, requests):
    serve_dumps(fake_kite)
    history = Candles(fake_kite, interval=interval)
    server, _, _ = make_server()
    args = history_args(interval=interval, from_date="2025-09-01", to_date="2026-10-05")  # 400 days
    answer = tool_json(await call(server, "get_historical_data", args))
    assert len(history.queries) == requests
    assert answer["count"] == 400 and answer["interval"] == interval
    limit_days = market.MAX_DAYS_PER_REQUEST[interval]
    for start, end in history.spans:
        assert moment(end) - moment(start) < dt.timedelta(days=limit_days)


async def test_limit_keeps_the_newest_candles_and_stops_early(make_server, fake_kite):
    serve_dumps(fake_kite)
    history = Candles(fake_kite)
    server, _, _ = make_server()
    args = history_args(from_date="2026-05-01", to_date="2026-10-31")
    few = tool_json(await call(server, "get_historical_data", {**args, "limit": 3}))
    assert history.spans == [("2026-10-28 00:00:00", "2026-10-31 23:59:59")]  # the newest range already has 4
    assert [c[0][:10] for c in few["candles"]] == ["2026-10-29", "2026-10-30", "2026-10-31"]
    assert few["count"] == 3 and few["truncated"] is True and "older" in few["note"]

    history.queries.clear()
    more = tool_json(await call(server, "get_historical_data", {**args, "limit": 10}))
    assert len(history.queries) == 2  # one range is not enough, two are
    assert more["count"] == 10 and more["candles"][-1][0].startswith("2026-10-31")

    history.queries.clear()
    enough = tool_json(await call(server, "get_historical_data", {**args, "limit": 2000}))
    assert len(history.queries) == 4 and enough["count"] == 184 and "truncated" not in enough


async def test_a_call_never_returns_more_than_the_cap(make_server, fake_kite):
    serve_dumps(fake_kite)
    history = Candles(fake_kite, interval="day")
    server, _, _ = make_server()
    args = history_args(interval="day", from_date="2000-01-01", to_date="2026-11-04")  # 9805 days
    answer = tool_json(await call(server, "get_historical_data", args))
    assert answer["count"] == market.MAX_CANDLES == 2000
    assert answer["candles"][-1][0] == "2026-11-04T09:15:00+0530"  # the newest are kept
    assert answer["truncated"] is True
    assert len(history.queries) == 2  # 1805 + 2000 candles: enough, so the three oldest ranges are not fetched
    assert history.spans[0] == ("2021-11-26 00:00:00", NOW)  # 2000-01-01 + 4 x 2000 days, up to now
    assert (await call(server, "get_historical_data", {**args, "limit": 5001})).is_error


async def test_the_number_of_requests_per_call_is_capped(make_server, fake_kite, monkeypatch):
    serve_dumps(fake_kite)
    history = Candles(fake_kite, transform=lambda candles, query: [])  # an instrument with no data at all
    monkeypatch.setattr(market, "MAX_REQUESTS", 3)
    server, _, _ = make_server()
    answer = tool_json(await call(server, "get_historical_data", history_args(from_date="2026-01-01")))
    assert len(history.queries) == 3 and answer["truncated"] is True and answer["count"] == 0
    assert "No candles found" in answer["note"] and "Narrow the range" in answer["note"]


async def test_an_empty_answer_comes_with_a_note(make_server, fake_kite):
    serve_dumps(fake_kite)
    Candles(fake_kite, transform=lambda candles, query: [])
    server, _, _ = make_server()
    answer = tool_json(await call(server, "get_historical_data", history_args()))
    assert answer["count"] == 0 and answer["candles"] == [] and "holiday" in answer["note"]
    assert "truncated" not in answer


async def test_open_interest_and_continuous_flags_reach_kite(make_server, fake_kite):
    serve_dumps(fake_kite)
    history = Candles(fake_kite, token=NIFTY_FUT_TOKEN, interval="day")
    server, _, _ = make_server()
    args = history_args(instrument="NFO:NIFTY26NOVFUT", interval="day", oi=True, continuous=True)
    answer = tool_json(await call(server, "get_historical_data", args))
    assert history.queries[0]["continuous"] == "1" and history.queries[0]["oi"] == "1"
    assert answer["instrument"] == "NFO:NIFTY26NOVFUT" and answer["instrument_token"] == NIFTY_FUT_TOKEN
    assert answer["columns"] == ["date", "open", "high", "low", "close", "volume", "oi"]
    assert all(len(candle) == 7 and candle[6] > 0 for candle in answer["candles"])


async def test_open_interest_is_null_where_kite_sends_none(make_server, fake_kite):
    serve_dumps(fake_kite)
    Candles(fake_kite, transform=lambda candles, query: [c[:6] for c in candles])
    server, _, _ = make_server()
    answer = tool_json(await call(server, "get_historical_data", history_args(oi=True)))
    assert answer["columns"][-1] == "oi" and all(len(c) == 7 and c[6] is None for c in answer["candles"])


async def test_continuous_is_for_day_candles_of_futures(make_server, fake_kite):
    serve_dumps(fake_kite)
    server, _, _ = make_server()
    minute = await call(server, "get_historical_data", history_args(instrument="NFO:NIFTY26NOVFUT", continuous=True))
    assert minute.is_error and "interval=day" in tool_text(minute)
    equity = await call(server, "get_historical_data", history_args(interval="day", continuous=True))
    assert equity.is_error and "futures" in tool_text(equity) and "NSE:INFY" in tool_text(equity)
    option = await call(
        server,
        "get_historical_data",
        history_args(instrument="NFO:NIFTY26NOV24000CE", interval="day", continuous=True),
    )
    assert option.is_error and "futures" in tool_text(option)
    by_token = await call(
        server, "get_historical_data", history_args(instrument=str(INFY_TOKEN), interval="day", continuous=True)
    )
    assert by_token.is_error and "futures" in tool_text(by_token)  # the token is looked up for this check


async def test_intervals_are_validated(make_server, fake_kite):
    serve_dumps(fake_kite)
    Candles(fake_kite)
    server, _, _ = make_server()
    for bad in ("2minute", "1d", "hour", "", "MINUTE"):
        result = await call(server, "get_historical_data", history_args(interval=bad))
        assert result.is_error, bad
    assert not [r for r in fake_kite.requests if "/historical/" in r.url.path]


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"from_date": "yesterday"}, "from_date"),
        ({"to_date": "2026-13-01"}, "to_date"),
        ({"from_date": "2026-10-11", "to_date": "2026-10-10"}, "after to_date"),
        ({"from_date": "2026-11-05"}, "in the future"),
        ({"from_date": "2026-11-04 10:00:01"}, "in the future"),
        ({"instrument": "INFY"}, "EXCHANGE:TRADINGSYMBOL"),
        ({"instrument": "0"}, "out of range"),
        ({"instrument": "99999999999"}, "out of range"),
    ],
)
async def test_bad_arguments_are_refused_before_any_request(make_server, fake_kite, changes, message):
    serve_dumps(fake_kite)
    Candles(fake_kite)
    server, _, _ = make_server()
    fake_kite.requests.clear()
    result = await call(server, "get_historical_data", history_args(**changes))
    assert result.is_error and message in tool_text(result)
    assert fake_kite.calls("GET", "/instruments/historical/408065/minute") == []


@pytest.mark.parametrize(
    ("changes", "expected_from", "expected_to"),
    [
        (
            {"from_date": "2026-10-05 09:15", "to_date": "2026-10-05 15:30"},
            "2026-10-05 09:15:00",
            "2026-10-05 15:30:00",
        ),
        ({"from_date": "2026-10-05T09:15:30", "to_date": "2026-10-06"}, "2026-10-05 09:15:30", "2026-10-06 23:59:59"),
        ({"to_date": "2026-10-05T10:00:00+00:00"}, "2026-10-01 00:00:00", "2026-10-05 15:30:00"),  # UTC -> IST
        ({"to_date": "2030-12-31"}, "2026-10-01 00:00:00", NOW),  # the future means now
        ({"from_date": "2026-11-04", "to_date": "2026-11-04"}, "2026-11-04 00:00:00", NOW),
    ],
)
async def test_dates_are_ist_and_dates_alone_cover_the_whole_day(
    make_server, fake_kite, changes, expected_from, expected_to
):
    serve_dumps(fake_kite)
    history = Candles(fake_kite)
    server, _, _ = make_server()
    answer = tool_json(await call(server, "get_historical_data", history_args(**changes)))
    assert history.spans == [(expected_from, expected_to)]
    assert (answer["from"], answer["to"]) == (expected_from, expected_to)


async def test_numeric_tokens_work_with_or_without_the_instrument_list(make_server, fake_kite):
    Candles(fake_kite)
    server, _, _ = make_server()
    answer = tool_json(await call(server, "get_historical_data", history_args(instrument=str(INFY_TOKEN))))
    assert answer["instrument"] == "408065" and answer["instrument_token"] == INFY_TOKEN and answer["count"] == 10

    serve_dumps(fake_kite)  # the list is available now: the tool can name the token
    server, _, _ = make_server()
    answer = tool_json(await call(server, "get_historical_data", history_args(instrument=f" {INFY_TOKEN} ")))
    assert answer["instrument"] == "NSE:INFY"


async def test_unknown_symbols_and_an_unavailable_list_get_clear_errors(make_server, fake_kite):
    history = Candles(fake_kite)
    server, _, _ = make_server()
    unavailable = await call(server, "get_historical_data", history_args())  # no instrument dump is served
    assert unavailable.is_error
    assert "instrument_token" in tool_text(unavailable) and "NSE:INFY" in tool_text(unavailable)

    serve_dumps(fake_kite)
    server, _, _ = make_server()
    unknown = await call(server, "get_historical_data", history_args(instrument="NSE:NOSUCH"))
    assert unknown.is_error and "not in today's instrument list" in tool_text(unknown)
    assert "search_instruments" in tool_text(unknown)
    assert history.queries == []


async def test_historical_data_without_the_paid_plan_says_so(make_server, fake_kite):
    serve_dumps(fake_kite)
    fake_kite.on(
        "GET",
        "/instruments/historical/408065/minute",
        status=403,
        error_type="PermissionException",
        message="Insufficient permission",
    )
    server, _, _ = make_server()
    result = await call(server, "get_historical_data", history_args())
    assert result.is_error and "paid Kite Connect plan" in tool_text(result)


async def test_a_malformed_historical_answer_is_reported(make_server, fake_kite):
    serve_dumps(fake_kite)
    fake_kite.on("GET", "/instruments/historical/408065/minute", {"candles": [["not a time", 1, 2, 3, 4, 5]]})
    server, _, _ = make_server()
    result = await call(server, "get_historical_data", history_args())
    assert result.is_error and "unexpected historical answer" in tool_text(result)
    fake_kite.on("GET", "/instruments/historical/408065/minute", {"candles": [["2026-10-01T09:15:00+0530", 1]]})
    result = await call(server, "get_historical_data", history_args())
    assert result.is_error and "unexpected historical answer" in tool_text(result)


# --- the instrument tools ---------------------------------------------------------------------------------------------


async def test_search_instruments_tool(make_server, fake_kite):
    serve_dumps(fake_kite)
    server, _, _ = make_server()
    found = tool_json(
        await call(
            server,
            "search_instruments",
            {"query": "nifty", "exchange": "NFO", "instrument_type": "FUT", "expiry": "2026-11"},
        )
    )
    assert [item["key"] for item in found] == ["NFO:NIFTY26NOVFUT", "NFO:BANKNIFTY26NOVFUT"]
    assert found[0] == {
        "instrument_token": NIFTY_FUT_TOKEN,
        "exchange_token": NIFTY_FUT_TOKEN // 256,
        "tradingsymbol": "NIFTY26NOVFUT",
        "name": "NIFTY",
        "last_price": 24100.5,
        "expiry": "2026-11-24",
        "strike": 0.0,
        "tick_size": 0.05,
        "lot_size": 75,
        "instrument_type": "FUT",
        "segment": "NFO-FUT",
        "exchange": "NFO",
        "key": "NFO:NIFTY26NOVFUT",
    }
    assert len(tool_json(await call(server, "search_instruments", {"query": "nifty", "limit": 2}))) == 2
    assert tool_json(await call(server, "search_instruments", {"query": "no-such-thing"})) == []
    by_filter = tool_json(await call(server, "search_instruments", {"segment": "INDICES"}))
    assert [item["tradingsymbol"] for item in by_filter] == ["NIFTY 50", "NIFTY BANK"]


async def test_search_instruments_refuses_bad_input(make_server, fake_kite):
    serve_dumps(fake_kite)
    server, _, _ = make_server()
    for args in ({}, {"query": "x", "limit": 0}, {"query": "x", "limit": 201}, {"query": "x", "expiry": "soon"}):
        assert (await call(server, "search_instruments", args)).is_error, args
    nothing = await call(server, "search_instruments", {})
    assert "query or at least one filter" in tool_text(nothing)


async def test_get_instrument_by_symbol_or_token(make_server, fake_kite):
    serve_dumps(fake_kite)
    server, _, _ = make_server()
    by_symbol = tool_json(await call(server, "get_instrument", {"instrument": "nfo:nifty26novfut"}))
    assert by_symbol["key"] == "NFO:NIFTY26NOVFUT" and by_symbol["lot_size"] == 75 and by_symbol["tick_size"] == 0.05
    by_token = tool_json(await call(server, "get_instrument", {"instrument": str(NIFTY_FUT_TOKEN)}))
    assert by_token == by_symbol
    index = tool_json(await call(server, "get_instrument", {"instrument": "NSE:NIFTY 50"}))
    assert index["segment"] == "INDICES" and index["expiry"] is None
    for missing in ("NSE:NOSUCH", "999", "INFY"):
        result = await call(server, "get_instrument", {"instrument": missing})
        assert result.is_error, missing


async def test_instrument_tools_report_an_unavailable_list(make_server, fake_kite):
    server, _, _ = make_server()  # no dump is served
    for name, args in (
        ("search_instruments", {"query": "nifty"}),
        ("get_instrument", {"instrument": "NSE:INFY"}),
    ):
        result = await call(server, name, args)
        assert result.is_error and "instrument list could not be loaded" in tool_text(result), name


async def test_status_and_refresh_tools(make_server, fake_kite):
    server, _, _ = make_server()
    status = tool_json(await call(server, "get_instruments_status"))
    assert status["loaded"] is False and status["instruments"] == {"loaded": False}
    assert fake_kite.requests == []

    serve_dumps(fake_kite)
    refreshed = tool_json(await call(server, "refresh_instruments"))
    assert refreshed["refreshed"] == ["instruments", "mf_instruments"] and refreshed["loaded"] is True
    assert refreshed["instruments"]["rows"] == len(DUMP_ROWS) and refreshed["mf_instruments"]["rows"] == len(MF_ROWS)
    status = tool_json(await call(server, "get_instruments_status"))
    assert status["instruments"]["loaded_at"] == "2026-11-04T10:00:00+05:30" and status["stale"] is False
    assert tool_json(await call(server, "refresh_instruments"))["refreshed"] == []
    assert tool_json(await call(server, "refresh_instruments", {"force": True}))["refreshed"] == [
        "instruments",
        "mf_instruments",
    ]
    assert len(fake_kite.calls("GET", "/instruments")) == 2


async def test_refresh_works_in_read_only_mode(make_server, fake_kite):
    serve_dumps(fake_kite)
    server, _, _ = make_server(KITE_MCP_READ_ONLY="true")
    assert tool_json(await call(server, "refresh_instruments"))["loaded"] is True
    assert fake_kite.writes() == []


async def test_refresh_failure_is_an_error_only_without_a_cache(make_server, fake_kite):
    server, _, _ = make_server()
    result = await call(server, "refresh_instruments")  # nothing served, nothing cached
    assert result.is_error and "no cached copy" in tool_text(result)


async def test_search_mf_instruments_tool(make_server, fake_kite):
    serve_dumps(fake_kite)
    server, _, _ = make_server()
    found = tool_json(await call(server, "search_mf_instruments", {"query": "bluechip", "plan": "direct"}))
    assert [fund["tradingsymbol"] for fund in found] == ["INF846K01DP8"]
    fund = found[0]
    assert fund["amc"] == "AXISMUTUALFUND_MF" and fund["purchase_allowed"] is True
    assert fund["minimum_purchase_amount"] == 5000.0 and fund["last_price"] == 57.1234
    assert fund["last_price_date"] == "2026-11-03" and fund["scheme_type"] == "equity"
    by_filter = tool_json(await call(server, "search_mf_instruments", {"amc": "kotak"}))
    assert [f["tradingsymbol"] for f in by_filter] == ["INF174K01LS2"]
    assert (await call(server, "search_mf_instruments", {})).is_error
    assert fake_kite.calls("GET", "/instruments") == []  # the MF list does not pull the instrument list


# --- the order tools use the same instrument master ------------------------------------------------------------------

ORDER = {
    "exchange": "NSE",
    "tradingsymbol": "INFY",
    "transaction_type": "BUY",
    "quantity": 1,
    "product": "CNC",
    "order_type": "LIMIT",
    "price": 1500.0,
}
FUTURE_ORDER = {
    "exchange": "NFO",
    "tradingsymbol": "NIFTY26NOVFUT",
    "transaction_type": "BUY",
    "quantity": 75,
    "product": "NRML",
    "order_type": "LIMIT",
    "price": 24100.05,
}
BIG_CAPS = {"KITE_MCP_MAX_ORDER_VALUE": "100000000", "KITE_MCP_MAX_DAILY_VALUE": "100000000"}


async def test_place_order_dry_run_checks_tick_and_lot_size_against_the_instrument_master(make_server, fake_kite):
    serve_dumps(fake_kite)
    fake_kite.on("POST", "/margins/orders", [])
    server, _, rt = make_server(**BIG_CAPS)

    ok = tool_json(await call(server, "place_order", ORDER))
    assert ok["dry_run"] is True and ok["checks"]["ok"] is True and ok["checks"]["warnings"] == []
    off_tick = await call(server, "place_order", {**ORDER, "price": 1500.03})
    assert off_tick.is_error and "price 1500.03 is not a multiple of the tick size 0.05 for NSE:INFY" in tool_text(
        off_tick
    )

    future = tool_json(await call(server, "place_order", FUTURE_ORDER))
    assert future["checks"]["ok"] is True
    assert tool_json(await call(server, "place_order", {**FUTURE_ORDER, "quantity": 150}))["checks"]["ok"] is True
    off_lot = await call(server, "place_order", {**FUTURE_ORDER, "quantity": 80})
    assert off_lot.is_error and "quantity 80 is not a multiple of the lot size 75 for NFO:NIFTY26NOVFUT" in tool_text(
        off_lot
    )
    both = await call(server, "place_order", {**FUTURE_ORDER, "quantity": 10, "price": 24100.02})
    assert both.is_error and "tick size" in tool_text(both) and "lot size" in tool_text(both)

    unknown = await call(server, "place_order", {**ORDER, "tradingsymbol": "NOSUCH"})
    assert unknown.is_error and "NSE:NOSUCH is not in today's instrument list" in tool_text(unknown)
    assert fake_kite.writes() == []
    assert all(event["event"] == "dry_run" for event in rt.audit.read())


async def test_place_order_dry_run_still_works_when_the_instrument_list_is_unavailable(make_server, fake_kite):
    fake_kite.on("POST", "/margins/orders", [])
    server, _, _ = make_server(**BIG_CAPS)
    answer = tool_json(await call(server, "place_order", {**ORDER, "price": 1500.03}))  # not checked: no list
    assert answer["checks"]["ok"] is True
    assert any("instrument master could not be read" in w for w in answer["checks"]["warnings"])
    assert fake_kite.writes() == []


async def test_place_order_dry_run_with_a_stale_list_does_not_call_a_new_symbol_unknown(
    make_server, fake_kite, monkeypatch
):
    serve_dumps(fake_kite)
    fake_kite.on("POST", "/margins/orders", [])
    server, _, rt = make_server(**BIG_CAPS)
    assert tool_json(await call(server, "place_order", ORDER))["checks"]["ok"] is True  # loads the list
    moment = [FIXED_NOW + dt.timedelta(days=1)]  # tomorrow: the cached list is a day old, and Kite is down
    monkeypatch.setattr(rt.instruments, "_clock", lambda: moment[0])
    fake_kite.on("GET", "/instruments", status=500, error_type="GeneralException", message="down")

    infy = tool_json(await call(server, "place_order", ORDER))  # known symbols are still checked against the old list
    assert infy["checks"]["ok"] is True
    new = tool_json(await call(server, "place_order", {**ORDER, "tradingsymbol": "NEWLISTING"}))
    assert any("instrument master could not be read" in w for w in new["checks"]["warnings"])  # warning, not an error
