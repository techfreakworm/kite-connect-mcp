"""The event service: one per serving process with KITE_MCP_EVENTS=true.

It owns the store and the consumers config, and is the only place events are created:
    ingest_order          a row read from Kite (the doorbell's re-read, or the poll) -> an order event, or nothing
    note_token_rejected   Kite refused the stored token -> one session event per token
    record_selftest       a verified selftest postback -> a selftest event
    record_router_event   a delivery that is late or failed -> a router event (never to its own consumer)
    ring / refused        the postback route's doorbell and its refusal counter

Every new event is written in one transaction with its order's state and a pending delivery per matching consumer,
then the listeners (the router) are told. Part 1B adds the doorbell worker, the poller, the router and start/stop.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from ..config import Settings
from ..errors import ConfigError
from ..timeutil import now_ist, to_ist
from .consumers import ConsumersConfig
from .model import (
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


class EventService:
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
        self._event_listeners.append(callback)

    def on_doorbell(self, callback: Callable[[], None]) -> None:
        self._doorbell_listeners.append(callback)

    @staticmethod
    def _tell(listeners: list[Callable[[], None]]) -> None:
        for callback in listeners:
            try:
                callback()
            except Exception:
                log.exception("order events: a listener failed")

    def _insert(
        self, body: dict[str, Any], *, order_id: str | None, consumers: list[str], now: dt.datetime
    ) -> int | None:
        with self.store.write() as con:
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
                return None  # the same change is stored already (a postback and the poll both reported it)
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
        now = self.clock()
        body = selftest_event(order_id, received_at=now)
        self.store.bump("selftest_received", now)
        return self._insert(body, order_id=order_id, consumers=self.config.matching(body), now=now)

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
        """A verified postback for `order_id`: keep the doorbell on disk (StoreError if that fails) and wake the
        worker that re-reads the order from Kite."""
        now = self.clock()
        self.store.ring(order_id, now)
        self.store.bump("postback_accepted", now)
        self._tell(self._doorbell_listeners)

    def refused(self, reason: str) -> None:
        """Count a refused postback (a wrong secret shows up here). Never raises."""
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


def describe_status(
    store: EventStore, settings: Settings, now: dt.datetime, consumer_names: list[str] | None = None
) -> dict[str, Any]:
    """What `events status` and `get_event_status` show. No payloads, no ids of orders."""
    out = store.summary()
    out["store"] = str(store.path)
    if consumer_names is not None:
        out["configured_consumers"] = consumer_names
        for name in consumer_names:
            out["consumers"].setdefault(
                name,
                {
                    "pending": 0,
                    "retrying": 0,
                    "delayed": 0,
                    "failed": 0,
                    "done": 0,
                    "skipped": 0,
                    "last_done_at": None,
                    "last_error": None,
                },
            )
    local = to_ist(now)
    start, end = settings.order_poll_window
    out["poll"] = {
        "interval_s": settings.order_poll_interval_s,
        "window": f"{start:%H:%M}-{end:%H:%M}",
        "days": sorted(settings.order_poll_days),
        "in_window_now": local.weekday() in settings.order_poll_days and start <= local.time() < end,
    }
    if (
        local.weekday() in settings.order_poll_days
        and local.time() >= end
        and out["after_close_ok"] != local.date().isoformat()
    ):
        out["warning"] = "no successful poll after the close today: fills after the last poll may be missing"
    return out
