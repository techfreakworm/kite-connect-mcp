"""Tools: the live WebSocket ticker and order updates (https://kite.trade/docs/connect/v3/websocket/).

These tools only change this server's own state (which instruments it streams, whether the connection is open), so they
are kind "local"; reading what has arrived is kind "read". None of them touches the Kite account, so none goes through
the write pipeline. The connection itself lives in ticker/manager.py and outlives individual tool calls.

Instruments are EXCHANGE:TRADINGSYMBOL names or numeric instrument tokens. Names are resolved here, through the
instrument master when the manager does not already know them; numeric tokens always work.

With KITE_MCP_TICKER_ENABLED=false only `ticker_status` exists, and it says why nothing else does. With
KITE_MCP_POSTBACK_PATH set, this module also adds the postback HTTP route (see postback.py); both feed the same
order-update buffer that `ticker_order_updates` reads.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from typing import Annotated, Any

from pydantic import Field

from ..errors import InstrumentsUnavailable, KiteMCPError, ToolInputError
from ..postback import PostbackRoute
from ..ticker.manager import PLAN_NOTE, CachedTick, Mode, Target, TickerManager
from ..timeutil import to_ist
from ._base import Registrar, Runtime, parse_instrument

MAX_INSTRUMENTS_PER_CALL = 3000
MAX_TOKEN = 2**32 - 1

DISABLED_MESSAGE = (
    "The WebSocket ticker is disabled on this server (KITE_MCP_TICKER_ENABLED=false), so there is no live streaming, "
    "no cached ticks and no order-update feed. Live streaming needs the paid Kite Connect plan; the free Personal plan "
    "has no WebSocket access. Set KITE_MCP_TICKER_ENABLED=true and restart the server to use it."
)

INSTRUMENTS_HELP = (
    "EXCHANGE:TRADINGSYMBOL names (NSE:INFY, NSE:NIFTY 50, NFO:NIFTY25NOVFUT) and/or numeric instrument tokens "
    "(408065). Names are looked up in the instrument list; if that cannot be loaded, use tokens."
)
Instruments = Annotated[list[str | int], Field(max_length=MAX_INSTRUMENTS_PER_CALL, description=INSTRUMENTS_HELP)]


# --- resolving instruments ---------------------------------------------------------------------------------------


def manager_of(rt: Runtime) -> TickerManager:
    if rt.ticker is None:
        raise KiteMCPError("the WebSocket ticker is not available in this server")
    return rt.ticker


def _is_number(text: str) -> bool:
    text = text.strip()
    return text.isascii() and text.isdecimal()


def token_of_number(value: int) -> int:
    if isinstance(value, bool) or not 0 < value <= MAX_TOKEN:
        raise ToolInputError(f"instrument token {value!r} is not a positive 32-bit number")
    return value


async def resolve(
    rt: Runtime, ticker: TickerManager, items: Sequence[str | int], *, strict: bool
) -> tuple[list[Target], list[str]]:
    """The targets for `items`, in order and without duplicates, plus the names that could not be resolved.

    With `strict` (subscribing) an unknown name or an unavailable instrument list is an error; without it (reading or
    unsubscribing) such names are just returned as unresolved, since they cannot be subscribed anyway."""
    targets: dict[int, Target] = {}
    unresolved: list[str] = []
    for item in items:
        if isinstance(item, int) or _is_number(str(item)):
            token = token_of_number(int(item))
            target = Target(token, await key_of_token(rt, ticker, token, lookup=strict))
        else:
            exchange, symbol = parse_instrument(str(item))
            found = await target_of_name(rt, ticker, exchange, symbol, strict=strict)
            if found is None:
                unresolved.append(f"{exchange}:{symbol}")
                continue
            target = found
        targets.setdefault(target.token, target)
    return list(targets.values()), unresolved


async def key_of_token(rt: Runtime, ticker: TickerManager, token: int, *, lookup: bool) -> str | None:
    """The EXCHANGE:TRADINGSYMBOL of a token for display: what the manager knows, else (when `lookup`) the
    instrument list, else None. Never fails: a label is a nicety, so whatever goes wrong loading the list (not just
    "unavailable": also a session or network error) must not stop a subscription by token."""
    known = ticker.name_of(token)
    if known or not lookup or rt.instruments is None:
        return known
    try:
        instrument = await rt.instruments.by_token(token)
    except KiteMCPError:
        return None
    return instrument.key if instrument else None


async def target_of_name(
    rt: Runtime, ticker: TickerManager, exchange: str, symbol: str, *, strict: bool
) -> Target | None:
    key = f"{exchange}:{symbol}"
    known = ticker.token_of(key)
    if known is not None:
        return Target(known, ticker.name_of(known) or key)
    try:
        if rt.instruments is None:
            raise InstrumentsUnavailable("no instrument list is configured")
        instrument = await rt.instruments.lookup(exchange, symbol)
    except InstrumentsUnavailable:
        if strict:
            raise ToolInputError(
                f"cannot look up {key}: the instrument list is not available right now. Use numeric instrument "
                "tokens instead (for example 408065)."
            ) from None
        return None
    if instrument is None:
        if strict:
            raise ToolInputError(
                f"{key} is not in the instrument list: check the exchange and tradingsymbol (search_instruments finds "
                "them)"
            )
        return None
    return Target(instrument.instrument_token, instrument.key)


def label(ticker: TickerManager, token: int) -> str:
    return ticker.name_of(token) or str(token)


def tick_view(ticker: TickerManager, cached: CachedTick) -> dict[str, Any]:
    view: dict[str, Any] = {}
    key = ticker.name_of(cached.token)
    if key:
        view["instrument"] = key
    view.update(cached.tick)
    if "change" in view:
        view["change"] = round(view["change"], 4)  # a percentage; 17 digits help nobody
    view["received_at"] = cached.received_at
    view["age_s"] = round(ticker.age_s(cached), 1)
    return view


def parse_since(value: str | None) -> dt.datetime | None:
    if value is None or not value.strip():
        return None
    try:
        return to_ist(dt.datetime.fromisoformat(value.strip()))
    except ValueError:
        raise ToolInputError(
            f"since {value!r} is not a time: use ISO 8601, e.g. 2026-11-04T09:30:00+05:30 (IST if no offset)"
        ) from None


def help_for(status: dict[str, Any]) -> str | None:
    """One sentence on what to do next, for the states where the model would otherwise be guessing."""
    state = status["state"]
    if state == "idle":
        return "Not connected. ticker_subscribe opens the connection (it needs the paid Kite Connect plan)."
    if state == "waiting_for_login":
        return "There is no usable access token. Log in; the ticker then connects on its own."
    if state == "blocked":
        return "Kite refused the connection; see blocked. A new login makes it try again by itself."
    if state == "failed":
        return "The ticker cannot work with this configuration; see blocked, fix it and restart the server."
    if state in ("retrying", "connecting"):
        return "The connection is not up yet; the ticker retries by itself. last_error says what went wrong."
    return None


# --- the tools ---------------------------------------------------------------------------------------------------


def register(reg: Registrar) -> None:
    rt = reg.rt
    settings = rt.settings

    if not settings.ticker_enabled:

        @reg.tool("ticker_status", title="WebSocket ticker status", toolset="ticker", kind="read")
        async def ticker_status_disabled() -> Any:
            """Whether live streaming is available. On this server the WebSocket ticker is DISABLED
            (KITE_MCP_TICKER_ENABLED=false): the answer explains how to enable it and what it needs."""
            answer: dict[str, Any] = {"enabled": False, "message": DISABLED_MESSAGE}
            if settings.postback_path:
                answer["postback"] = (
                    f"KITE_MCP_POSTBACK_PATH is set ({settings.postback_path}) but the postback endpoint is off too: "
                    "order updates are only readable through the ticker tools."
                )
            return answer

        return

    if settings.postback_path and reg.enabled("ticker_order_updates", "ticker", "read") is None:
        hooks = rt.extras.setdefault("http_routes", [])
        if not any(isinstance(hook, PostbackRoute) for hook in hooks):
            hooks.append(PostbackRoute(rt))

    @reg.tool("ticker_subscribe", title="Stream live ticks", toolset="ticker", kind="local")
    async def ticker_subscribe(
        instruments: Instruments,
        mode: Annotated[
            Mode,
            Field(
                description="ltp = last price only; quote = price, volume, OHLC, buy/sell quantity; "
                "full = quote plus open interest and 5-level market depth"
            ),
        ] = "quote",
    ) -> Any:
        """Start streaming live market data for instruments over Kite's WebSocket. The server opens one connection in
        the background on the first call (it needs the paid Kite Connect plan and a current login), reconnects and
        resubscribes by itself, and keeps the latest tick of every subscribed instrument for ticker_get_ticks.

        Modes: ltp (last price), quote (default: last price, last quantity, average price, volume, buy/sell quantity,
        OHLC, change %), full (quote plus open interest, last trade time, exchange timestamp and 5 levels of bid/offer
        depth). Indices have no volume or depth. Subscribing an instrument again with another mode switches its mode.
        At most KITE_MCP_TICKER_MAX_SUBSCRIPTIONS instruments (default 1000; Kite allows 3000) can be subscribed.

        An empty instruments list only opens the connection, which is how to receive order updates
        (ticker_order_updates) without market data. This changes only the server's own streaming state; it places no
        orders."""
        ticker = manager_of(rt)
        targets, _ = await resolve(rt, ticker, instruments, strict=True)
        outcome = await ticker.subscribe(targets, mode)
        status = ticker.status()
        answer: dict[str, Any] = {
            "mode": mode,
            "connected": status["connected"],
            "state": status["state"],
            "subscribed_total": status["subscriptions"]["total"],
            "limit": status["subscriptions"]["limit"],
            "instruments": [
                {"instrument": label(ticker, t.token), "instrument_token": t.token, "result": outcome[t.token]}
                for t in targets
            ],
        }
        if not status["connected"]:
            answer["note"] = help_for(status)
            answer["last_error"] = status["last_error"]
        return answer

    @reg.tool("ticker_unsubscribe", title="Stop streaming instruments", toolset="ticker", kind="local")
    async def ticker_unsubscribe(instruments: Instruments) -> Any:
        """Stop streaming instruments and drop their cached ticks. Instruments that were not subscribed are listed
        under not_subscribed. The WebSocket connection stays open (order updates keep arriving); ticker_stop closes
        it."""
        ticker = manager_of(rt)
        targets, unresolved = await resolve(rt, ticker, instruments, strict=False)
        labels = {t.token: label(ticker, t.token) for t in targets}  # before the names are forgotten
        removed = await ticker.unsubscribe(t.token for t in targets)
        answer: dict[str, Any] = {
            "unsubscribed": [labels[token] for token in removed],
            "not_subscribed": [labels[t.token] for t in targets if t.token not in removed] + unresolved,
            "subscribed_total": len(ticker.subscriptions()),
        }
        return answer

    @reg.tool("ticker_get_ticks", title="Latest cached ticks", toolset="ticker", kind="read")
    async def ticker_get_ticks(
        instruments: Annotated[
            list[str | int] | None,
            Field(
                max_length=MAX_INSTRUMENTS_PER_CALL,
                description="which instruments (names and/or tokens); default: every subscribed instrument",
            ),
        ] = None,
        max_age_s: Annotated[
            float | None,
            Field(gt=0, description="leave out ticks older than this many seconds; they are listed under stale"),
        ] = None,
    ) -> Any:
        """The latest tick received for each subscribed instrument, from the server's cache (nothing is requested from
        Kite). Each tick has instrument (when known), instrument_token, mode, last_price and, for quote and full,
        last_traded_quantity, average_traded_price, volume_traded, total_buy_quantity, total_sell_quantity,
        ohlc {open, high, low, close} and change (% from the previous close); full adds oi, oi_day_high, oi_day_low,
        last_trade_time, exchange_timestamp and depth {buy, sell} (5 levels of {quantity, price, orders}). Prices are
        in rupees, times in IST. Every tick also has received_at and age_s (seconds since it arrived): ticks stop
        outside market hours or when the connection is down, so check age_s, or pass max_age_s to be told which
        instruments are stale. Instruments subscribed but without a tick yet are listed under no_tick_yet."""
        ticker = manager_of(rt)
        unresolved: list[str] = []
        if instruments is None:
            tokens = list(ticker.subscriptions())
        else:
            targets, unresolved = await resolve(rt, ticker, instruments, strict=False)
            tokens = [t.token for t in targets]
        subscribed = ticker.subscriptions()
        cached = {c.token: c for c in ticker.latest(tokens)}

        fresh: list[dict[str, Any]] = []
        stale: dict[str, float] = {}
        for token in tokens:
            if token not in cached:
                continue
            view = tick_view(ticker, cached[token])
            if max_age_s is not None and view["age_s"] > max_age_s:
                stale[label(ticker, token)] = view["age_s"]
            else:
                fresh.append(view)
        status = ticker.status()
        answer: dict[str, Any] = {"state": status["state"], "connected": status["connected"], "count": len(fresh)}
        answer["ticks"] = fresh
        if stale:
            answer["stale"] = stale
        waiting = [label(ticker, t) for t in tokens if t in subscribed and t not in cached]
        if waiting:
            answer["no_tick_yet"] = waiting
        absent = [label(ticker, t) for t in tokens if t not in subscribed] + unresolved
        if absent:
            answer["not_subscribed"] = absent
        if not subscribed:
            answer["note"] = "Nothing is subscribed: call ticker_subscribe first."
        elif not fresh and not status["connected"]:
            answer["note"] = help_for(status)
        return answer

    @reg.tool("ticker_status", title="WebSocket ticker status", toolset="ticker", kind="read")
    async def ticker_status() -> Any:
        """The state of the WebSocket ticker: state (idle, connecting, connected, retrying, waiting_for_login, blocked,
        failed), connected, connected_since, last_message_at (any frame, heartbeats included), last_tick_at,
        reconnects, subscriptions by mode, how many ticks and order updates are cached, packets_skipped (frames this
        server could not parse), last_error, and blocked (why the ticker stopped retrying: Kite refused the token, for
        example because the app has no paid WebSocket plan). Nothing in it is secret."""
        ticker = manager_of(rt)
        status = ticker.status()
        session = rt.tokens.status()
        answer: dict[str, Any] = {"enabled": True, **status}
        answer["logged_in"] = bool(session.get("logged_in"))
        answer["postback_path"] = settings.postback_path
        hint = help_for(status)
        if hint:
            answer["help"] = hint
        if status["state"] in ("blocked", "failed", "retrying") or not answer["logged_in"]:
            answer["requirements"] = PLAN_NOTE
        return answer

    @reg.tool("ticker_order_updates", title="Order updates and broker messages", toolset="ticker", kind="read")
    async def ticker_order_updates(
        limit: Annotated[int, Field(ge=1, le=500, description="at most this many items")] = 50,
        since: Annotated[
            str | None,
            Field(description="ISO 8601 time (IST if no offset): only items received after it, oldest first"),
        ] = None,
    ) -> Any:
        """Order updates and broker messages received by this server: the WebSocket stream (live updates for orders
        placed anywhere on the account: web, mobile or API) and, when KITE_MCP_POSTBACK_PATH is set, Kite postbacks
        sent to the HTTP endpoint. Each item has seq, received_at, source ("ticker" or "postback"), type ("order";
        "error" or "message" for Kite's own text) and data. For orders, data has order_id, status (COMPLETE, REJECTED,
        CANCELLED, UPDATE...), status_message, exchange, tradingsymbol, transaction_type, order_type, product, variety,
        quantity, filled_quantity, pending_quantity, price, trigger_price, average_price and timestamps.

        Only the last 500 items are kept, in memory, and items arrive only while the WebSocket is open
        (ticker_subscribe, even with an empty list, opens it) or the postback endpoint is receiving. Without since:
        the latest items, oldest first. With since: the first items after it, so you can page forward (more=true
        means there are further ones). These are notifications, not the book of record: a postback is authenticated
        only by a checksum over order_id and order_timestamp, so confirm what matters with get_order_history."""
        ticker = manager_of(rt)
        items, more = ticker.events.recent(limit, parse_since(since))
        answer: dict[str, Any] = {"count": len(items), "buffered": len(ticker.events), "items": items}
        if more:
            answer["more"] = True
        if not items and ticker.state != "connected":
            answer["note"] = (
                f"Nothing received yet. The WebSocket is not connected (state: {ticker.state}); ticker_subscribe "
                "opens it"
                + ("." if settings.postback_path else ", or set KITE_MCP_POSTBACK_PATH to receive postbacks.")
            )
        return answer

    @reg.tool("ticker_stop", title="Stop the WebSocket ticker", toolset="ticker", kind="local")
    async def ticker_stop() -> Any:
        """Close the WebSocket connection and forget all subscriptions and cached ticks (order updates already
        received stay available). Calling ticker_subscribe later starts afresh, which is also how to retry after Kite
        refused the connection with the current token. Safe to call when nothing is running."""
        ticker = manager_of(rt)
        dropped = len(ticker.subscriptions())
        await ticker.stop()
        return {"stopped": True, "subscriptions_dropped": dropped, "state": ticker.state}
