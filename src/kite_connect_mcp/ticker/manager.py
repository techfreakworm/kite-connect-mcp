"""The WebSocket ticker: one long-lived connection to Kite, started on the first subscription and shared by every tool.

How it is put together, and why:

* **Cheap to construct, lazy to connect.** `build_runtime` creates a TickerManager for every server, so the constructor
  does no I/O and needs no event loop. The first `subscribe()` starts one background task (`_run`) with
  `asyncio.create_task`, which (unlike a task spawned in an anyio task group) is not cancelled when the MCP request that
  started it ends. That is what lets one connection outlive individual tool calls under both stdio and stateless HTTP.
* **Desired state, not a command log.** `_subs` (token -> mode) is what the caller wants. Each live connection keeps
  `applied`, what Kite has been told on that connection. `_sync()` sends the difference (and does so, under a lock, from
  tool calls and from the connection task alike), so a reconnect is just "applied is empty again" and the same code
  resubscribes everything with each token's mode. Racing subscribe/unsubscribe calls cannot leave the two out of step.
* **The connection task owns the connection.** It reads frames, keeps the latest tick per token, and appends order
  updates and Kite's text messages to a bounded buffer. It reconnects with exponential backoff and jitter, and
  reconnects at once when the token provider returns a different access token (the next day's login).
* **An auth refusal stops the retries.** An HTTP 401/403 on the handshake, or Kite closing the socket right after
  connecting twice in a row, means this token will not work; hammering Kite with it helps nobody. The task then only
  watches for a *different* token, and status() says so. (The token provider is deliberately left alone: a 403 on the
  WebSocket can simply mean the app has no streaming plan, and marking the token rejected would break the REST tools.)
* **Dead links are noticed by silence.** Kite sends a 1-byte heartbeat every couple of seconds, so no frame at all for
  `idle_timeout_s` means a half-open connection: close it and reconnect.
* **Secrets stay out of everything.** The access token travels in the URL query, so the URL is never logged or put in
  an error; every message that can reach status() or a log goes through `_redact`.
* **stop() is safe at any time**: never started, already stopped, or called from another event loop at shutdown (the CLI
  closes the runtime with a fresh `asyncio.run` after the server's loop is gone).

Connection contract for the injectable `connect` (tests, alternative transports): `await connect(url)` returns an object
with `await recv() -> str | bytes`, `await send(str)` and `await close()`, raising websockets' `ConnectionClosed` from
`recv`/`send` once the link is gone. `websockets.asyncio.client.connect` itself satisfies it.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import math
import random
import re
import time
import traceback
from collections import Counter, deque
from collections.abc import Awaitable, Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol
from urllib.parse import urlencode, urlsplit

import websockets.asyncio.client
from websockets.exceptions import ConnectionClosed, InvalidStatus, InvalidURI, WebSocketException

from ..auth.tokenstore import TokenProvider
from ..config import Settings
from ..errors import KiteMCPError, NotLoggedIn, ToolInputError
from ..timeutil import now_ist
from .parser import parse_packet, parse_text, split_packets

log = logging.getLogger(__name__)

# websockets logs the whole request line, query string (and so the access token) included, at DEBUG. This logger is
# pinned above that level, so no logging configuration can make the token appear in a log.
_WS_LOGGER = logging.getLogger("kite_connect_mcp.ticker.websocket")
_WS_LOGGER.setLevel(logging.WARNING)

Mode = Literal["ltp", "quote", "full"]
MODES: tuple[Mode, ...] = ("ltp", "quote", "full")

KITE_MAX_SUBSCRIPTIONS = 3000  # Kite's hard limit per connection
EVENT_BUFFER_SIZE = 500
MAX_FRAME_BYTES = 4 * 1024 * 1024  # 3000 instruments in full mode are about 560 KB per frame
EARLY_CLOSE_LIMIT = 2  # Kite hanging up on us straight after connecting this many times in a row = refused
MAX_TEXT_CHARS = 500  # longest free-text value kept from an order update or a Kite message
MAX_ERROR_CHARS = 400

# connection states (status()["state"])
IDLE = "idle"  # never started, or stopped
CONNECTING = "connecting"
CONNECTED = "connected"
RETRYING = "retrying"  # the link dropped or could not be made; waiting to try again
WAITING_FOR_LOGIN = "waiting_for_login"  # there is no usable access token right now
BLOCKED = "blocked"  # Kite refused this token; waiting for a different one
FAILED = "failed"  # the configuration cannot work (bad KITE_TICKER_URL); needs a restart

PLAN_NOTE = (
    "The WebSocket needs the paid Kite Connect plan (the free Personal plan has no streaming) and a current access "
    "token."
)
REFUSED_MESSAGE = (
    "Kite refused the WebSocket connection (HTTP {status}). " + PLAN_NOTE + " Not retrying with this token: log in "
    "again to get a new one, or call ticker_stop and subscribe again to retry with the same token."
)
EARLY_CLOSE_MESSAGE = (
    "Kite closed the WebSocket right after connecting, twice in a row. That usually means the access token was "
    "rejected or the app has no streaming access. " + PLAN_NOTE + " Not retrying with this token: log in again, or "
    "call ticker_stop and subscribe again to retry with the same token."
)

# The order-update fields worth keeping. Anything else is dropped: it keeps the buffer small, and it keeps the
# identifiers (user_id, placed_by, app_id), the checksum and the guid out of tool output and memory. Free text is
# clipped, so a forged postback cannot stuff a long message into what the model reads.
ORDER_FIELDS = (
    "order_id",
    "exchange_order_id",
    "parent_order_id",
    "status",
    "status_message",
    "exchange",
    "tradingsymbol",
    "instrument_token",
    "transaction_type",
    "order_type",
    "product",
    "variety",
    "validity",
    "quantity",
    "disclosed_quantity",
    "price",
    "trigger_price",
    "average_price",
    "filled_quantity",
    "pending_quantity",
    "cancelled_quantity",
    "unfilled_quantity",
    "market_protection",
    "order_timestamp",
    "exchange_update_timestamp",
    "exchange_timestamp",
    "tag",
)

_QUERY_SECRET = re.compile(r"(access_token|api_key|api_secret)=[^&\s'\"]+", re.IGNORECASE)


class TickerError(KiteMCPError):
    """The ticker cannot do what was asked: Kite refused the connection, or the configuration cannot work."""


class Connection(Protocol):
    """What the manager needs from a WebSocket connection (see the module docstring)."""

    async def recv(self) -> str | bytes: ...

    async def send(self, message: str) -> None: ...

    async def close(self) -> None: ...


Connector = Callable[[str], Awaitable[Connection]]


@dataclass(frozen=True)
class Timings:
    """Every duration the manager uses, so tests can run in milliseconds. The defaults suit Kite."""

    connect_timeout_s: float = 15.0  # the whole opening handshake
    close_timeout_s: float = 3.0  # waiting for Kite's close frame when we hang up
    idle_timeout_s: float = 30.0  # no frame of any kind (Kite's heartbeat comes every couple of seconds) = dead link
    poll_s: float = 5.0  # how often a running or waiting connection looks for a different access token
    early_close_s: float = 5.0  # a close sooner than this after connecting, before any frame, is "right away"
    stable_after_s: float = 10.0  # a connection this old that delivered data counts as healthy: backoff starts over
    connect_wait_s: float = 5.0  # how long subscribe() waits to learn whether the connection could be made
    backoff_base_s: float = 1.0
    backoff_cap_s: float = 60.0


def default_backoff(timings: Timings) -> Callable[[int], float]:
    """Exponential backoff with jitter: base * 2**attempt, capped, then scaled by a random 0.5..1.0 so that several
    servers (or a restart storm) do not retry in lockstep."""

    def backoff(attempt: int) -> float:
        ceiling = min(timings.backoff_cap_s, timings.backoff_base_s * 2 ** min(max(attempt, 0), 30))
        return ceiling * random.uniform(0.5, 1.0)  # noqa: S311 - jitter, not security

    return backoff


@dataclass(frozen=True)
class Target:
    """An instrument to stream: its token, and its EXCHANGE:TRADINGSYMBOL key when the caller knows it."""

    token: int
    key: str | None = None


@dataclass(frozen=True)
class CachedTick:
    token: int
    tick: dict[str, Any]
    received_at: dt.datetime


def clean_order(payload: Mapping[str, Any]) -> dict[str, Any]:
    """The documented, useful fields of an order update (WebSocket frame or postback), with free text clipped."""
    out: dict[str, Any] = {}
    for name in ORDER_FIELDS:
        value = payload.get(name)
        if isinstance(value, bool) or value is None:
            continue
        if isinstance(value, int) or (isinstance(value, float) and math.isfinite(value)):
            out[name] = value
        elif isinstance(value, str) and value:
            out[name] = value[:MAX_TEXT_CHARS]
    return out


class EventBuffer:
    """The last few hundred order updates and Kite text messages, oldest dropped first. Each entry:
    {"seq", "received_at", "source" ("ticker" or "postback"), "type" ("order", "error", "message"), "data"}."""

    def __init__(self, clock: Callable[[], dt.datetime], maxlen: int = EVENT_BUFFER_SIZE):
        self._clock = clock
        self._items: deque[dict[str, Any]] = deque(maxlen=maxlen)
        self._seq = 0

    def add(self, kind: str, data: Any, source: str) -> dict[str, Any]:
        self._seq += 1
        entry = {"seq": self._seq, "received_at": self._clock(), "source": source, "type": kind, "data": data}
        self._items.append(entry)
        return dict(entry)

    def recent(self, limit: int, since: dt.datetime | None = None) -> tuple[list[dict[str, Any]], bool]:
        """(entries, more). Without `since`: the latest `limit` entries, oldest first. With `since`: the first
        `limit` entries received after it, so that a caller can page forward; `more` says there are further ones."""
        items = list(self._items)
        if since is None:
            return [dict(e) for e in items[-limit:]], False
        newer = [e for e in items if e["received_at"] > since]
        return [dict(e) for e in newer[:limit]], len(newer) > limit

    def __len__(self) -> int:
        return len(self._items)


def plan_messages(applied: Mapping[int, Mode], wanted: Mapping[int, Mode]) -> list[dict[str, Any]]:
    """The requests that turn what a connection has been told (`applied`) into what is wanted: unsubscribe the
    dropped tokens, subscribe the new ones, then one `mode` request per mode for every token that is new or changed.
    The mode is always sent explicitly rather than relying on Kite's default of "quote"."""
    messages: list[dict[str, Any]] = []
    gone = [token for token in applied if token not in wanted]
    if gone:
        messages.append({"a": "unsubscribe", "v": gone})
    new = [token for token in wanted if token not in applied]
    if new:
        messages.append({"a": "subscribe", "v": new})
    by_mode: dict[Mode, list[int]] = {}
    for token, mode in wanted.items():
        if applied.get(token) != mode:
            by_mode.setdefault(mode, []).append(token)
    messages.extend({"a": "mode", "v": [mode, by_mode[mode]]} for mode in MODES if mode in by_mode)
    return messages


# --- internals -------------------------------------------------------------------------------------------------------


@dataclass
class _Link:
    """One live connection. `applied` lives here, not on the manager, so that a straggler from an old connection can
    never mark something as sent on the new one."""

    ws: Connection
    applied: dict[int, Mode] = field(default_factory=dict)
    frames: int = 0


@dataclass(frozen=True)
class _End:
    """How a connection that was up came to an end. `kind`: "closed" (Kite or the network ended it), "idle" (we gave
    up on a silent link) or "token" (a different access token appeared)."""

    frames: int
    lived_s: float
    kind: Literal["closed", "idle", "token"]
    reason: str


class _Blocked(Exception):
    """Kite refused the access token."""


class _Unusable(Exception):
    """The configuration cannot work; retrying is pointless."""


class TickerManager:
    """See the module docstring. Public surface: subscribe, unsubscribe, latest, status, events, stop."""

    def __init__(
        self,
        settings: Settings,
        tokens: TokenProvider,
        *,
        clock: Callable[[], dt.datetime] = now_ist,
        connect: Connector | None = None,
        timings: Timings | None = None,
        backoff: Callable[[int], float] | None = None,
    ):
        self.settings = settings
        self.tokens = tokens
        self.clock = clock
        self.timings = timings or Timings()
        self.events = EventBuffer(clock)
        self.max_subscriptions = min(settings.ticker_max_subscriptions, KITE_MAX_SUBSCRIPTIONS)
        self._backoff = backoff or default_backoff(self.timings)
        self._connect: Connector = connect or self._websockets_connect

        self._subs: dict[int, Mode] = {}
        self._names: dict[int, str] = {}
        self._by_key: dict[str, int] = {}
        self._ticks: dict[int, CachedTick] = {}

        self._task: asyncio.Task[None] | None = None
        self._link: _Link | None = None
        self._sync_lock: asyncio.Lock | None = None
        self._wake: asyncio.Event | None = None
        self._waiters: list[asyncio.Future[None]] = []
        self._state = IDLE
        self._token_in_use: str | None = None
        self._blocked_token: str | None = None
        self._blocked_reason: str | None = None
        self._unusable_reason: str | None = None
        self._attempt = 0  # consecutive unhealthy outcomes, for the backoff
        self._early = 0  # consecutive closes right after connecting
        self._reset_counters()

    def _reset_counters(self) -> None:
        self._connections = 0
        self._connected_since: dt.datetime | None = None
        self._last_message_at: dt.datetime | None = None
        self._last_tick_at: dt.datetime | None = None
        self._last_error: str | None = None
        self._last_error_at: dt.datetime | None = None
        self._ticks_received = 0
        self._packets_skipped = 0

    # --- the public surface ------------------------------------------------------------------------------------

    @property
    def state(self) -> str:
        return self._state

    def subscriptions(self) -> dict[int, Mode]:
        """token -> mode, in subscription order."""
        return dict(self._subs)

    def name_of(self, token: int) -> str | None:
        """The EXCHANGE:TRADINGSYMBOL a subscribed token was subscribed under, if it was known."""
        return self._names.get(token)

    def token_of(self, key: str) -> int | None:
        """The token of a subscribed EXCHANGE:TRADINGSYMBOL (no instrument list needed)."""
        return self._by_key.get(key.upper())

    def latest(self, tokens: Iterable[int] | None = None) -> list[CachedTick]:
        """The cached latest tick of each token (default: every subscribed one that has produced a tick)."""
        wanted = list(self._subs) if tokens is None else list(tokens)
        return [self._ticks[t] for t in wanted if t in self._ticks]

    def age_s(self, cached: CachedTick) -> float:
        """Seconds since this tick arrived, by the manager's clock."""
        return max(0.0, (self.clock() - cached.received_at).total_seconds())

    def record_order_update(self, payload: Mapping[str, Any], *, source: str) -> dict[str, Any]:
        """Add an order update (from the WebSocket, or a verified postback) to the shared buffer."""
        return self.events.add("order", clean_order(payload), source)

    async def subscribe(self, targets: Sequence[Target], mode: Mode = "quote") -> dict[int, str]:
        """Stream `targets` in `mode`, starting the connection if needed. Returns token -> "subscribed",
        "mode_changed" or "unchanged". An empty list just opens the connection (order updates need one).

        Raises NotLoggedIn without a usable token, ToolInputError past the subscription limit, and TickerError if Kite
        refuses the connection (then nothing from this call stays subscribed). Anything transient (Kite unreachable)
        is not an error here: the subscriptions are kept and applied when the link comes up; status() shows why not
        yet."""
        if mode not in MODES:
            raise ToolInputError(f"mode must be one of {', '.join(MODES)}, not {mode!r}")
        record = self.tokens.current()
        if record is None:
            raise NotLoggedIn("the WebSocket ticker needs a current access token and there is none")
        self.settings.require_api_key()
        self._refuse_if_unusable(record.access_token)

        unique = list({t.token: t for t in targets}.values())
        new = [t for t in unique if t.token not in self._subs]
        if len(self._subs) + len(new) > self.max_subscriptions:
            raise ToolInputError(
                f"{len(self._subs)} instruments are subscribed and {len(new)} more were asked for, which is over the "
                f"limit of {self.max_subscriptions} (KITE_MCP_TICKER_MAX_SUBSCRIPTIONS; Kite allows at most "
                f"{KITE_MAX_SUBSCRIPTIONS} per connection). Unsubscribe some first."
            )

        outcome, undo = self._record(unique, mode)
        self._ensure_started()
        if self._link is not None:
            await self._sync()
        else:
            await self.wait_for_state({CONNECTED, RETRYING, WAITING_FOR_LOGIN, BLOCKED, FAILED})
        if self._state in (BLOCKED, FAILED):
            self._undo(undo)
            raise TickerError(self._blocked_reason or self._unusable_reason or "the ticker is not available")
        return outcome

    def _record(self, targets: Sequence[Target], mode: Mode) -> tuple[dict[int, str], dict[int, Mode | None]]:
        """Note the wanted subscriptions. Returns each token's outcome, and what to restore if the call must be undone
        (token -> its previous mode, or None if it was not subscribed)."""
        outcome: dict[int, str] = {}
        undo: dict[int, Mode | None] = {}
        for target in targets:
            previous = self._subs.get(target.token)
            outcome[target.token] = (
                "subscribed" if previous is None else "unchanged" if previous == mode else "mode_changed"
            )
            if previous != mode:
                undo[target.token] = previous
                self._subs[target.token] = mode
            if target.key:
                self._remember(target.token, target.key)
        return outcome, undo

    def _undo(self, undo: dict[int, Mode | None]) -> None:
        for token, previous in undo.items():
            if previous is None:
                self._forget(token)
            else:
                self._subs[token] = previous

    async def unsubscribe(self, tokens: Iterable[int]) -> list[int]:
        """Stop streaming `tokens`. Returns the ones that were subscribed. The connection stays open (order updates
        keep flowing); stop() closes it."""
        removed = [token for token in dict.fromkeys(tokens) if token in self._subs]
        for token in removed:
            self._forget(token)
        if removed and self._link is not None:
            await self._sync()
        return removed

    def status(self) -> dict[str, Any]:
        """The state of the ticker, safe to show anyone: no token, no URL, no account data."""
        modes = Counter(self._subs.values())
        task = self._task
        return {
            "state": self._state,
            "running": task is not None and not task.done(),
            "connected": self._link is not None,
            "connected_since": self._connected_since,
            "last_message_at": self._last_message_at,
            "last_tick_at": self._last_tick_at,
            "reconnects": max(0, self._connections - 1),
            "subscriptions": {"total": len(self._subs), "limit": self.max_subscriptions}
            | {mode: modes.get(mode, 0) for mode in MODES},
            "ticks_cached": len(self._ticks),
            "ticks_received": self._ticks_received,
            "packets_skipped": self._packets_skipped,
            "order_updates_buffered": len(self.events),
            "last_error": self._last_error,
            "last_error_at": self._last_error_at,
            "blocked": self._blocked_reason or self._unusable_reason,
            "endpoint": self._host(),
        }

    async def stop(self) -> None:
        """Close the connection, forget every subscription and cached tick, and end the background task. Safe to call
        at any time, repeatedly, and from a different event loop than the one the task ran on. The order-update
        buffer is kept. A later subscribe() starts afresh (this also clears a refusal, so it is the way to retry)."""
        task, self._task = self._task, None
        if task is not None:
            await _cancel(task)
        self._link = None
        self._subs.clear()
        self._names.clear()
        self._by_key.clear()
        self._ticks.clear()
        self._blocked_token = self._blocked_reason = self._unusable_reason = None
        self._token_in_use = None
        self._reset_counters()
        self._set_state(IDLE)

    async def wait_for_state(self, states: Collection[str], wait_s: float | None = None) -> str:
        """Wait until the state is one of `states` (or `wait_s` seconds pass; default Timings.connect_wait_s). Returns
        the state then."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + (self.timings.connect_wait_s if wait_s is None else wait_s)
        while self._state not in states and (remaining := deadline - loop.time()) > 0:
            waiter: asyncio.Future[None] = loop.create_future()
            self._waiters.append(waiter)
            try:
                async with asyncio.timeout(remaining):
                    await waiter
            except TimeoutError:
                break
            finally:
                if waiter in self._waiters:
                    self._waiters.remove(waiter)
        return self._state

    # --- bookkeeping ---------------------------------------------------------------------------------------------

    def _remember(self, token: int, key: str) -> None:
        old = self._names.get(token)
        if old is not None:
            self._by_key.pop(old.upper(), None)
        self._names[token] = key
        self._by_key[key.upper()] = token

    def _forget(self, token: int) -> None:
        self._subs.pop(token, None)
        self._ticks.pop(token, None)
        key = self._names.pop(token, None)
        if key is not None:
            self._by_key.pop(key.upper(), None)

    def _set_state(self, state: str) -> None:
        if state == self._state:
            return
        self._state = state
        waiters, self._waiters = self._waiters, []
        for waiter in waiters:
            if not waiter.done():
                waiter.set_result(None)

    def _refuse_if_unusable(self, access_token: str) -> None:
        if self._unusable_reason:
            raise TickerError(self._unusable_reason)
        if self._blocked_token is not None and self._blocked_token == access_token:
            raise TickerError(self._blocked_reason or REFUSED_MESSAGE.format(status=403))

    def _host(self) -> str:
        parts = urlsplit(self.settings.ticker_url)
        host = parts.hostname or "?"
        try:
            return f"{host}:{parts.port}" if parts.port else host
        except ValueError:
            return host

    def _redact(self, text: str) -> str:
        s = self.settings
        for secret in (self._token_in_use, s.access_token, s.api_key, s.api_secret):
            if secret:
                text = text.replace(secret, "[redacted]")
        return _QUERY_SECRET.sub(r"\1=[redacted]", text)

    def _note_error(self, message: str) -> None:
        self._last_error = self._redact(message)[:MAX_ERROR_CHARS]
        self._last_error_at = self.clock()

    def _url(self, access_token: str) -> str:
        base = self.settings.ticker_url
        query = urlencode({"api_key": self.settings.require_api_key(), "access_token": access_token})
        return f"{base}{'&' if '?' in base else '?'}{query}"

    # --- the connection task -----------------------------------------------------------------------------------

    def _ensure_started(self) -> None:
        """Start the background task unless one is already running on this event loop. A subscribe() right after a
        block or a missing login also wakes the task, so a fresh token is tried at once instead of at the next poll."""
        loop = asyncio.get_running_loop()
        task = self._task
        if task is not None and not task.done() and task.get_loop() is loop:
            if self._state in (BLOCKED, WAITING_FOR_LOGIN):
                self._set_state(CONNECTING)  # so that a waiter does not mistake the stale state for a verdict
                self._wake_up()
            return
        self._wake = asyncio.Event()
        self._set_state(CONNECTING)
        self._task = loop.create_task(self._run(), name="kite-ticker")

    def _wake_up(self) -> None:
        if self._wake is not None:
            self._wake.set()

    async def _nap(self, seconds: float) -> None:
        """Sleep `seconds`, or until subscribe() has news (no await between the caller's checks and the clear())."""
        wake = self._wake
        if wake is None:
            await asyncio.sleep(seconds)
            return
        wake.clear()
        try:
            async with asyncio.timeout(seconds):
                await wake.wait()
        except TimeoutError:
            pass

    async def _run(self) -> None:
        """Connect, read, reconnect: until cancelled (stop()) or the configuration is found unusable."""
        self._attempt = self._early = 0
        while True:
            token = await self._usable_token()
            end: _End | None = None
            try:
                end = await self._session(token)
            except _Blocked as exc:
                self._block(token, str(exc))
                continue
            except _Unusable as exc:
                self._unusable_reason = str(exc)
                self._note_error(str(exc))
                log.error("ticker: %s", exc)
                self._set_state(FAILED)
                return
            except Exception as exc:  # refused, unreachable, timed out, or a bug: say so, back off, try again
                self._note_failure(exc)
            delay = self._judge(token, end)
            if delay is None:
                continue  # blocked
            self._set_state(RETRYING)
            if end is not None and end.kind == "token":
                log.info("ticker: a new access token appeared; reconnecting")
            else:
                log.warning("ticker: not connected (%s); trying again in %.1f s", self._last_error, delay)
            await asyncio.sleep(delay)

    async def _usable_token(self) -> str:
        """The access token to connect with, waiting (and watching for a login) while there is none or while Kite has
        refused the only one there is."""
        while True:
            record = self.tokens.current()
            if record is None:
                self._set_state(WAITING_FOR_LOGIN)
            elif record.access_token == self._blocked_token:
                self._set_state(BLOCKED)
            else:
                self._blocked_token = self._blocked_reason = None  # a different token: the refusal no longer applies
                return record.access_token
            await self._nap(self.timings.poll_s)

    def _judge(self, token: str, end: _End | None) -> float | None:
        """How long to wait before the next attempt, given how the last one went (`end` is None if no link was made),
        or None if Kite has now hung up on us once too often and the ticker is blocked instead."""
        healthy = False
        if end is not None:
            if end.kind == "token":
                healthy = True  # not a failure: a new login. Still a short pause, in case the token keeps flipping
                self._early = 0
            else:
                self._note_error(end.reason)
                if end.kind == "closed" and end.frames == 0 and end.lived_s < self.timings.early_close_s:
                    self._early += 1  # Kite hung up on us before saying anything
                    if self._early >= EARLY_CLOSE_LIMIT:
                        self._block(token, EARLY_CLOSE_MESSAGE)
                        return None
                else:
                    self._early = 0
                healthy = end.frames > 0 and end.lived_s >= self.timings.stable_after_s
        if healthy:
            self._attempt = 0
        delay = self._backoff(self._attempt)
        if not healthy:
            self._attempt += 1
        return delay

    def _block(self, token: str, reason: str) -> None:
        self._blocked_token = token
        self._blocked_reason = self._redact(reason)
        self._note_error(reason)
        log.warning("ticker: %s", self._blocked_reason)
        self._set_state(BLOCKED)

    def _note_failure(self, exc: Exception) -> None:
        if isinstance(exc, TimeoutError | OSError | WebSocketException):
            self._note_error(_describe(exc))
            return
        self._note_error(f"unexpected {type(exc).__name__}")
        log.error("ticker: unexpected error, will retry:\n%s", self._redact("".join(traceback.format_exception(exc))))

    async def _session(self, token: str) -> _End:
        """One connection, from handshake to hang-up. Raises _Blocked / _Unusable / the connect error if no link was
        made; otherwise returns how the link ended."""
        url = self._url(token)
        self._token_in_use = token
        self._set_state(CONNECTING)
        log.info("ticker: connecting to %s", self._host())
        try:
            async with asyncio.timeout(self.timings.connect_timeout_s):
                ws = await self._connect(url)
        except InvalidStatus as exc:
            status = exc.response.status_code
            if status in (401, 403):
                raise _Blocked(REFUSED_MESSAGE.format(status=status)) from None
            raise
        except InvalidURI:
            raise _Unusable(
                "KITE_TICKER_URL is not a valid WebSocket URL (it must start with wss:// or ws://)"
            ) from None

        link = _Link(ws)
        started = time.monotonic()
        kind: Literal["closed", "idle", "token"] = "closed"
        reason = "closed"
        try:
            self._link = link
            self._connections += 1
            self._connected_since = self.clock()
            self._set_state(CONNECTED)
            log.info("ticker: connected")
            await self._sync()
            kind, reason = await self._read(link, token)
        finally:
            self._link = None  # first, with the state: a subscribe() during the close below must not see "connected"
            self._connected_since = None
            self._set_state(RETRYING)
            await self._close(ws)
        return _End(link.frames, time.monotonic() - started, kind, reason)

    async def _read(self, link: _Link, token: str) -> tuple[Literal["closed", "idle", "token"], str]:
        """Read frames until the link ends. Returns (how it ended, why)."""
        t = self.timings
        wait = min(t.poll_s, t.idle_timeout_s)
        last_frame = last_check = time.monotonic()
        while True:
            try:
                async with asyncio.timeout(wait):
                    message: str | bytes | None = await link.ws.recv()
            except TimeoutError:
                message = None
            except ConnectionClosed as exc:
                return "closed", f"connection closed ({_close_summary(exc)})"
            now = time.monotonic()
            if message is not None:
                last_frame = now
                link.frames += 1
                self._on_message(message)
            elif now - last_frame >= t.idle_timeout_s:
                return "idle", f"no data from Kite for {t.idle_timeout_s:g} s"
            if message is None or now - last_check >= t.poll_s:
                last_check = now
                newer = self.tokens.current()
                if newer is not None and newer.access_token != token:
                    return "token", "a new access token appeared"

    async def _close(self, ws: Connection) -> None:
        try:
            async with asyncio.timeout(self.timings.close_timeout_s + 1):
                await ws.close()
        except Exception:  # a dead link may not close cleanly; there is nothing more to do about it
            log.debug("ticker: the connection did not close cleanly")

    async def _sync(self) -> None:
        """Send whatever the live connection has not been told yet. Serialised, so messages go out in a sane order; a
        send that fails means the link is dying and the reconnect will redo everything, so it is not an error."""
        link = self._link
        if link is None:
            return
        if self._sync_lock is None:
            self._sync_lock = asyncio.Lock()
        async with self._sync_lock:
            if self._link is not link:
                return
            wanted = dict(self._subs)
            try:
                for message in plan_messages(link.applied, wanted):
                    await link.ws.send(json.dumps(message, separators=(",", ":")))
            except (ConnectionClosed, OSError):
                log.debug("ticker: could not send (the connection is closing)")
                return
            link.applied = wanted

    # --- frames ------------------------------------------------------------------------------------------------

    def _on_message(self, message: str | bytes) -> None:
        now = self.clock()
        self._last_message_at = now
        if isinstance(message, bytes):
            self._on_binary(message, now)
        else:
            self._on_text(message)

    def _on_binary(self, frame: bytes, now: dt.datetime) -> None:
        packets = split_packets(frame)
        if not packets and len(frame) > 2:
            self._packets_skipped += 1  # not a heartbeat, yet nothing could be read from it
        for packet in packets:
            tick = parse_packet(packet)
            if tick is None:
                self._packets_skipped += 1
                continue
            token = tick["instrument_token"]
            if token in self._subs:  # a frame already in flight after an unsubscribe must not bring the entry back
                self._ticks[token] = CachedTick(token, tick, now)
                self._ticks_received += 1
                self._last_tick_at = now

    def _on_text(self, message: str) -> None:
        parsed = parse_text(message)
        if parsed is None:
            return
        kind, data = parsed["type"], parsed["data"]
        if kind == "order" and isinstance(data, dict):
            self.record_order_update(data, source="ticker")
        elif kind in ("error", "message"):
            text = (data if isinstance(data, str) else json.dumps(data, default=str))[:MAX_TEXT_CHARS]
            self.events.add(kind, self._redact(text), "ticker")
            if kind == "error":
                log.warning("ticker: Kite reported an error: %s", self._redact(text))

    # --- the default connection ----------------------------------------------------------------------------------

    def _websockets_connect(self, url: str) -> Awaitable[Connection]:
        return websockets.asyncio.client.connect(
            url,
            open_timeout=self.timings.connect_timeout_s,
            close_timeout=self.timings.close_timeout_s,
            ping_interval=None,  # Kite's own heartbeat plus the idle check below do the job of keepalive pings
            max_size=MAX_FRAME_BYTES,
            logger=_WS_LOGGER,
        )


def _close_summary(exc: ConnectionClosed) -> str:
    if exc.rcvd is None:
        return "no close frame from Kite"
    return f"code {exc.rcvd.code}" + (f", {exc.rcvd.reason}" if exc.rcvd.reason else "")


def _describe(exc: BaseException) -> str:
    if isinstance(exc, TimeoutError):
        return "timed out waiting for Kite"
    if isinstance(exc, InvalidStatus):
        return f"Kite answered the WebSocket handshake with HTTP {exc.response.status_code}"
    if isinstance(exc, OSError):
        return f"{type(exc).__name__}: {exc.strerror or exc}"
    return f"{type(exc).__name__}: {exc}"


async def _cancel(task: asyncio.Task[None]) -> None:
    """Cancel the connection task and wait for its clean-up (it closes the socket). A task whose event loop has
    already ended is simply dropped: the loop's shutdown cancelled it."""
    if task.done():
        if not task.cancelled():
            task.exception()  # mark any error as retrieved
        return
    loop = task.get_loop()
    if loop is not asyncio.get_running_loop():
        if not loop.is_closed():
            loop.call_soon_threadsafe(task.cancel)
        return
    task.cancel()
    await asyncio.wait({task})
    if not task.cancelled():
        task.exception()
