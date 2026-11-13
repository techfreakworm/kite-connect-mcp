"""The event service: one per serving process with KITE_MCP_EVENTS=true.

It owns the store and the consumers config, and is the only place events are created:
    ingest_order          a row read from Kite (the doorbell's re-read, or the poll) -> an order event, or nothing
    note_token_rejected   Kite refused the stored token -> one session event per token
    record_selftest       a verified selftest postback -> a selftest event
    record_router_event   a delivery that is late or failed -> a router event (never to its own consumer)
    ring / refused        the postback route's doorbell and its refusal counter

Every new event is written in one transaction with its order's state, the counter it moves (selftest_received) and a
pending delivery per matching consumer, then the listeners (the router) are told; a doorbell is kept in one
transaction with its counter (postback_accepted). So a StoreError means none of it was kept. Part 1B adds the doorbell
worker, the poller, the router and start/stop.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any, Literal, get_args

from ..config import Settings
from ..errors import ConfigError
from ..timeutil import now_ist, to_ist
from .consumers import ConsumersConfig
from .model import (
    ORDER_ID_RE,
    OrderView,
    judge,
    order_event,
    order_key,
    router_event,
    selftest_event,
    session_event,
)
from .store import EventStore, StoreError

if TYPE_CHECKING:
    from ..auth.tokenstore import TokenProvider
    from ..client.http import KiteClient

log = logging.getLogger(__name__)

Refusal = Literal[
    "no API secret",
    "body over the cap",
    "body not JSON",
    "body not a JSON object",
    "bad checksum",
    "bad order id",
    "another account",
]
# Why the postback route refused a postback: the only notes `refused` keeps. The note is shown to MCP clients (status),
# so it is never text from the unverified request.
REFUSALS: tuple[Refusal, ...] = get_args(Refusal)

_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


class EventService:
    """See the module docstring. Listeners (on_event, on_doorbell) are called synchronously, on the thread that
    wrote, right after the commit; one that raises is logged and the others still run. A listener tied to an event
    loop must therefore be safe to call from that thread: asyncio.Event.set is safe only on the loop's own thread, so a
    service written to from another thread needs `lambda: loop.call_soon_threadsafe(event.set)` instead."""

    def __init__(
        self,
        settings: Settings,
        client: KiteClient,
        tokens: TokenProvider,
        *,
        clock: Callable[[], dt.datetime] = now_ist,
        store: EventStore | None = None,
        config: ConsumersConfig | None = None,
    ):
        if settings.events_db is None:
            raise ConfigError("the event service needs a store: set KITE_MCP_EVENTS=true or KITE_MCP_EVENTS_DB")
        self.settings = settings
        self.client = client
        self.tokens = tokens
        self.clock = clock
        self.config = config if config is not None else ConsumersConfig.load(settings.events_consumers_file)
        self.store = store if store is not None else EventStore(settings.events_db)
        self.doorbells: Any = None  # DoorbellWorker, part 1B
        self.poller: Any = None  # Poller, part 1B
        self.router: Any = None  # Router, part 1B
        self._event_listeners: list[Callable[[], None]] = []
        self._doorbell_listeners: list[Callable[[], None]] = []

    # --- listeners ------------------------------------------------------------------------------------------------

    def on_event(self, callback: Callable[[], None]) -> None:
        """Call `callback` after each new event is committed (the router's wake-up), on the writer's thread."""
        self._event_listeners.append(callback)

    def on_doorbell(self, callback: Callable[[], None]) -> None:
        """Call `callback` after each doorbell is kept (the doorbell worker's wake-up), on the writer's thread."""
        self._doorbell_listeners.append(callback)

    @staticmethod
    def _tell(listeners: list[Callable[[], None]]) -> None:
        for callback in listeners:
            try:
                callback()
            except Exception:
                log.exception("order events: a listener failed")

    def _insert(
        self,
        body: dict[str, Any],
        *,
        order_id: str | None,
        consumers: list[str],
        now: dt.datetime,
        counter: str | None = None,
    ) -> int | None:
        """Store `body` with its deliveries, and bump `counter` when given, in one transaction; tell the listeners
        when the event is new."""
        with self.store.write() as con:
            if counter is not None:
                self.store.bump(counter, now, con=con)
            seq = self.store.insert_event(
                con, body, order_id=order_id, consumers=consumers, order_key=order_key(body), now=now
            )
        if seq is not None:
            self._tell(self._event_listeners)
        return seq

    # --- creating events ------------------------------------------------------------------------------------------

    def ingest_order(self, row: Mapping[str, Any], *, source: str, baseline: bool = False) -> int | None:
        """Store what this read of an order reports, as an event for the matching consumers. Returns the new seq, or
        None when it reports nothing new. With `baseline`, only the state is recorded (an order seen for the first
        time), and nobody is told. Raises ValueError for a row without a valid order_id, StoreError if the store
        cannot write."""
        view = OrderView.from_row(row)
        now = self.clock()
        with self.store.write() as con:
            previous = self.store.previous(view.order_id, con)
            if baseline:
                if previous is None:
                    self.store.save_state(con, view, now, event_id="")
                return None
            change = judge(view, previous)
            if change is None:
                return None
            body = order_event(
                view,
                change=change,
                source=source,
                received_at=now,
                tag_labels=self.config.tag_labels,
                previous_event_id=previous.event_id if previous is not None else "",
            )
            seq = self.store.insert_event(
                con,
                body,
                order_id=view.order_id,
                consumers=self.config.matching(body),
                order_key=view.order_id,
                now=now,
            )
            if seq is None:
                # Not a change reported twice: judge() drops that, as the state saved with the first report is read in
                # this same transaction. An event with this id is stored already only when the order's state row is
                # behind its events, and then the state catches up with the stored event.
                self.store.save_state(con, view, now, event_id=body["event_id"])
                return None
            self.store.save_state(con, view, now, event_id=body["event_id"])
        self._tell(self._event_listeners)
        return seq

    def note_token_rejected(self, access_token: str) -> int | None:
        """Kite refused this token: one `token_rejected` event per token (the id comes from a fingerprint of it; the
        token itself is never stored)."""
        fingerprint = hashlib.sha256(access_token.encode("utf-8", "replace")).hexdigest()
        now = self.clock()
        body = session_event("token_rejected", seed=fingerprint, received_at=now)
        return self._insert(body, order_id=None, consumers=self.config.matching(body), now=now)

    def record_selftest(self, order_id: str) -> int | None:
        """A verified selftest postback: counted in selftest_received every time, stored as an event once per id (a
        repeat returns None). ValueError, with nothing counted, for an id that is not a selftest id."""
        now = self.clock()
        body = selftest_event(order_id, received_at=now)
        return self._insert(
            body, order_id=order_id, consumers=self.config.matching(body), now=now, counter="selftest_received"
        )

    def record_router_event(
        self, name: str, *, consumer: str, about: Mapping[str, Any], error: str | None, seed: str
    ) -> int | None:
        """A late or failed delivery, for the other consumers. Nothing is raised about a router event itself, so two
        failing consumers cannot report each other's failures forever."""
        if about.get("kind") == "router":
            log.warning("order events: %s for %s about a router event (not reported further)", name, consumer)
            return None
        now = self.clock()
        body = router_event(name, consumer=consumer, about=about, error=error, seed=seed, received_at=now)
        return self._insert(body, order_id=None, consumers=self.config.matching(body, exclude=consumer), now=now)

    # --- the postback route's side ----------------------------------------------------------------------------------

    def ring(self, order_id: str) -> None:
        """A verified postback for `order_id`: keep its doorbell on disk and count it (postback_accepted) in one
        transaction, then wake the worker that re-reads the order from Kite. ValueError for an id that is not an order
        id; StoreError when the store cannot write, and then nothing was kept."""
        if not isinstance(order_id, str) or not ORDER_ID_RE.fullmatch(order_id):
            raise ValueError("not an order id")
        self.store.ring(order_id, self.clock())
        self._tell(self._doorbell_listeners)

    def refused(self, reason: Refusal) -> None:
        """Count a refused postback (a wrong secret shows up here as "bad checksum"). `reason` is one of REFUSALS,
        never text from the request: it is shown to MCP clients. Any other reason is a bug in the caller and raises
        ValueError; a store that cannot write is only logged."""
        if reason not in REFUSALS:
            raise ValueError("refused() takes one of REFUSALS")
        try:
            self.store.bump("postback_refused", self.clock(), reason)
        except StoreError:
            log.warning("order events: could not count a refused postback")

    def user_allowed(self, user_id: object) -> bool:
        """With KITE_MCP_ALLOWED_USER_ID set, a postback must name that account. (Plan 2 adds the hash pin.)"""
        pinned = self.settings.allowed_user_id
        if pinned is None:
            return True
        return isinstance(user_id, str) and user_id.strip().upper() == pinned

    # --- status ---------------------------------------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        return describe_status(self.store, self.settings, self.clock(), [c.name for c in self.config.consumers])


def last_closed_poll_day(settings: Settings, now: dt.datetime) -> dt.date | None:
    """The most recent poll day whose poll window has closed by `now`: today once its window has ended, else the poll
    day before (Friday, on a weekend or a Monday morning). None when no day is a poll day."""
    local = to_ist(now)
    first = 0 if local.time() >= settings.order_poll_window[1] else 1
    for back in range(first, first + 7):
        day = local.date() - dt.timedelta(days=back)
        if day.weekday() in settings.order_poll_days:
            return day
    return None


def describe_status(
    store: EventStore, settings: Settings, now: dt.datetime, consumer_names: list[str] | None = None
) -> dict[str, Any]:
    """What `events status` and `get_event_status` show. No payloads, no ids of orders.

    `warning` is set while no poll has succeeded after the most recent close of the poll window (meta after_close_ok
    is older than last_closed_poll_day), so a failed Friday close stays visible over the weekend and on Monday until
    Monday's own close is polled. A store the poll has never touched (neither baseline_done nor after_close_ok set) has
    missed nothing, and shows no warning."""
    out = store.summary(consumer_names or ())
    out["store"] = str(store.path)
    if consumer_names is not None:
        out["configured_consumers"] = consumer_names
    local = to_ist(now)
    start, end = settings.order_poll_window
    out["poll"] = {
        "interval_s": settings.order_poll_interval_s,
        "window": f"{start:%H:%M}-{end:%H:%M}",
        "days": sorted(settings.order_poll_days),
        "in_window_now": local.weekday() in settings.order_poll_days and start <= local.time() < end,
    }
    due = last_closed_poll_day(settings, now)
    ok = out["after_close_ok"]
    polled = ok is not None or out["baseline_done"] is not None
    if due is not None and polled and (ok is None or ok < due.isoformat()):
        out["warning"] = (
            f"no successful poll after the close on {_WEEKDAYS[due.weekday()]} {due.isoformat()}: "
            "fills after the last poll may be missing"
        )
    return out
