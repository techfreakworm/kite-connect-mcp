"""The event service: one per serving process with KITE_MCP_EVENTS=true.

It owns the store and the consumers config, and is the only place events are created:
    ingest_order          a row read from Kite (the doorbell's re-read, or the poll) -> an order event, or nothing
    note_token_rejected   Kite refused the stored token -> one session event per token
    record_selftest       a verified selftest postback -> a selftest event
    fail_delivery         a delivery that failed for good -> its failure and a delivery_failed router event
    alert_delayed         a delivery still retrying past its alert point -> a delivery_delayed router event, once
    ring / refused        the postback route's doorbell and its refusal counter

A router event never goes to the consumer it is about, and nothing is raised about a router event itself.

Every new event is written in one transaction with its order's state, the counter it moves (selftest_received), the
delivery change it reports (a failure, an alert sent) and a pending delivery per matching consumer, then the listeners
(the router) are told; a doorbell is kept in one transaction with its counter (postback_accepted). So a StoreError means
none of it was kept.

It builds its parts (the doorbell worker, the poller, the router), and start()/stop() run them, with the housekeeping
that prunes old events, as tasks on the event loop; the HTTP server starts the service as it comes up and stops it as
it shuts down. One process serves a store: start() takes a lock file next to it (`<store>.lock`), so a second server
on the same store fails to start instead of running the same consumers twice.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import fcntl
import logging
import os
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any, Literal, get_args

from ..config import Settings
from ..errors import ConfigError
from ..timeutil import now_ist, to_ist
from ._wait import wait_any
from .consumers import ConsumersConfig
from .doorbell import DoorbellWorker
from .model import (
    ORDER_ID_RE,
    OrderView,
    judge,
    order_event,
    order_key,
    router_event,
    selftest_event,
    session_event,
    token_fingerprint,
)
from .poll import Poller, in_poll_window, last_closed_poll_day
from .router import Router
from .store import Delivery, EventStore, StoreError, iso

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
        environ: Mapping[str, str] | None = None,
    ):
        if settings.events_db is None:
            raise ConfigError("the event service needs a store: set KITE_MCP_EVENTS=true or KITE_MCP_EVENTS_DB")
        self.settings = settings
        self.client = client
        self.tokens = tokens
        self.clock = clock
        self.config = config if config is not None else ConsumersConfig.load(settings.events_consumers_file)
        self.store = store if store is not None else EventStore(settings.events_db)
        self._event_listeners: list[Callable[[], None]] = []
        self._doorbell_listeners: list[Callable[[], None]] = []
        # The workers' wake-ups (on_doorbell, on_event) set asyncio.Event objects, which is safe only on the event
        # loop's thread. Every writer runs on the loop today (the postback route rings, the poller and the doorbell
        # worker ingest, the router records its events); one on another thread needs loop.call_soon_threadsafe.
        self.doorbells = DoorbellWorker(self)
        self.poller = Poller(self)
        self.router = Router(self, environ=environ)
        self._stop: asyncio.Event | None = None
        self._tasks: list[asyncio.Task[None]] = []
        self._lock_fd: int | None = None

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
        now = self.clock()
        body = session_event("token_rejected", seed=token_fingerprint(access_token), received_at=now)
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
        """A late or failed delivery, for the other consumers, on its own (the router uses fail_delivery and
        alert_delayed, which write it with the delivery change it reports). Nothing is raised about a router event
        itself, so two failing consumers cannot report each other's failures forever."""
        now = self.clock()
        body = self._router_body(name, consumer=consumer, about=about, error=error, seed=seed, now=now)
        if body is None:
            return None
        return self._insert(body, order_id=None, consumers=self.config.matching(body, exclude=consumer), now=now)

    def fail_delivery(self, consumer: str, delivery: Delivery, error: str | None, now: dt.datetime) -> bool:
        """Mark a pending delivery failed with `error`, and record its delivery_failed event (seeded with the failure
        time) for the other consumers, in one transaction. False, with nothing written, when the delivery is no longer
        pending (skipped or pruned while its command ran): then no event is raised either."""
        return self._router_change("delivery_failed", consumer, delivery, error, seed=iso(now), now=now)

    def alert_delayed(self, consumer: str, delivery: Delivery, now: dt.datetime) -> bool:
        """Mark the delivery_delayed alert of a pending delivery sent, and record that event (seeded with the first
        try, so it is stored once per round of tries) for the other consumers, in one transaction. False, with nothing
        written, when the delivery is no longer pending or was never tried."""
        if delivery.first_try_at is None:
            return False
        return self._router_change(
            "delivery_delayed", consumer, delivery, delivery.last_error, seed=iso(delivery.first_try_at), now=now
        )

    def _router_change(
        self, name: str, consumer: str, delivery: Delivery, error: str | None, *, seed: str, now: dt.datetime
    ) -> bool:
        """The delivery change behind router event `name` and the event itself, in one write; listeners are told
        after the commit, when the event is new."""
        with self.store.write() as con:
            if name == "delivery_failed":
                changed = self.store.finish_delivery(consumer, delivery.seq, "failed", now, error, con=con)
            else:
                changed = self.store.mark_alerted(consumer, delivery.seq, con=con)
            if not changed:
                return False
            about = self.store.event(delivery.seq, con=con)
            body = None
            if about is not None:
                body = self._router_body(name, consumer=consumer, about=about, error=error, seed=seed, now=now)
            seq = None
            if body is not None:
                seq = self.store.insert_event(
                    con,
                    body,
                    order_id=None,
                    consumers=self.config.matching(body, exclude=consumer),
                    order_key=order_key(body),
                    now=now,
                )
        if seq is not None:
            self._tell(self._event_listeners)
        return True

    @staticmethod
    def _router_body(
        name: str, *, consumer: str, about: Mapping[str, Any], error: str | None, seed: str, now: dt.datetime
    ) -> dict[str, Any] | None:
        """The router event `name` about `about`, None when `about` is a router event itself (not reported further)."""
        if about.get("kind") == "router":
            log.warning("order events: %s for %s about a router event (not reported further)", name, consumer)
            return None
        return router_event(name, consumer=consumer, about=about, error=error, seed=seed, received_at=now)

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

    # --- running --------------------------------------------------------------------------------------------------

    HOUSEKEEPING_S = 6 * 3600

    @property
    def running(self) -> bool:
        return bool(self._tasks)

    async def start(self) -> None:
        """Run the poll, the doorbell worker, the router and the housekeeping as tasks. Raises ConfigError when another
        process already serves this store."""
        if self._tasks:
            return
        self._take_lock()
        self._stop = asyncio.Event()
        jobs = {
            "poller": self.poller.run,
            "doorbells": self.doorbells.run,
            "router": self.router.run,
            "housekeeping": self._housekeeping,
        }
        self._tasks = [asyncio.create_task(job(self._stop), name=f"kite-events-{name}") for name, job in jobs.items()]
        log.info("order events: started, %d consumer(s)", len(self.config.consumers))

    async def stop(self) -> None:
        """Stop the tasks (a part still busy after 10 s is cancelled; a consumer command still running is killed, and
        its delivery stays pending for the next start), then release the lock and close the store. Calling it again,
        or without start(), is harmless."""
        if self._tasks and self._stop is not None:
            self._stop.set()
            _, pending = await asyncio.wait(self._tasks, timeout=10)
            for task in pending:
                task.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)
            self._tasks = []
            log.info("order events: stopped")
        self._release_lock()
        self.store.close()

    def housekeep(self) -> int:
        """Delete what is older than KITE_MCP_EVENTS_KEEP_DAYS (doorbells after two days). Returns the events
        deleted."""
        now = self.clock()
        return self.store.prune(
            now - dt.timedelta(days=self.settings.events_keep_days), doorbells_before=now - dt.timedelta(days=2)
        )

    async def _housekeeping(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                removed = self.housekeep()
                if removed:
                    log.info("order events: removed %d old event(s)", removed)
            except Exception:
                log.exception("order events: housekeeping failed")
            await wait_any(self.HOUSEKEEPING_S, stop)

    def _take_lock(self) -> None:
        """Hold `<store>.lock` (flock) while running: the kernel drops the lock when the process ends, however it ends.
        The file is not passed to consumer commands (Python opens it non-inheritable)."""
        path = f"{self.store.path}.lock"
        try:
            fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as exc:
            raise ConfigError(f"cannot open the lock file {path}: {exc.strerror}") from None
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            if isinstance(exc, BlockingIOError):
                raise ConfigError(f"another process is already serving order events from {self.store.path}") from None
            raise ConfigError(f"cannot lock {path}: {exc.strerror}") from None
        self._lock_fd = fd

    def _release_lock(self) -> None:
        fd, self._lock_fd = self._lock_fd, None
        if fd is not None:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
            with contextlib.suppress(OSError):
                os.close(fd)  # closing the file releases the lock too


def _day_of(stamp: str | None) -> dt.date | None:
    """The IST date of a time kept in meta (baseline_done holds an ISO time; a bare date reads as that day), None when
    there is none or it cannot be read."""
    if not stamp:
        return None
    try:
        return to_ist(dt.datetime.fromisoformat(stamp)).date()
    except ValueError:
        return None


def describe_status(
    store: EventStore, settings: Settings, now: dt.datetime, consumer_names: list[str] | None = None
) -> dict[str, Any]:
    """What `events status` and `get_event_status` show. No payloads, no ids of orders.

    `warning` is set while no poll has succeeded after the most recent close of the poll window (meta after_close_ok
    is older than last_closed_poll_day), so a failed Friday close stays visible over the weekend and on Monday until
    Monday's own close is polled. A store the poll has never touched (neither baseline_done nor after_close_ok set) has
    missed nothing, and shows no warning; nor does a close before the day of the first poll (the date of baseline_done),
    which the service was not running to see. A baseline_done that cannot be read hides no warning."""
    out = store.summary(consumer_names or ())
    out["store"] = str(store.path)
    if consumer_names is not None:
        out["configured_consumers"] = consumer_names
    start, end = settings.order_poll_window
    out["poll"] = {
        "interval_s": settings.order_poll_interval_s,
        "window": f"{start:%H:%M}-{end:%H:%M}",
        "days": sorted(settings.order_poll_days),
        "in_window_now": in_poll_window(settings, now),
    }
    due = last_closed_poll_day(settings, now)
    ok = out["after_close_ok"]
    polled = ok is not None or out["baseline_done"] is not None
    since = _day_of(out["baseline_done"])  # the day the poll first read the book
    if due is not None and polled and (ok is None or ok < due.isoformat()) and (since is None or due >= since):
        out["warning"] = (
            f"no successful poll after the close on {_WEEKDAYS[due.weekday()]} {due.isoformat()}: "
            "fills after the last poll may be missing"
        )
    return out
