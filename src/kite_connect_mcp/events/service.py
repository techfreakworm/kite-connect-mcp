"""The event service: one per serving process with KITE_MCP_EVENTS=true.

It owns the store and the consumers config, and is the only place events are created:
    ingest_order          a row read from Kite (the doorbell's re-read, or the poll) -> an order event, or nothing
    note_token_rejected   Kite refused the stored token -> one session event per token
    note_login            a new token was stored -> one `login` session event per token
    note_login_refused    another account's login was refused (the account pin) -> one `login_refused` per session
    note_login_failed     a real login on a link of ours did not complete -> one `login_failed` per link
    record_selftest       a verified selftest postback -> a selftest event
    fail_delivery         a delivery that failed for good -> its failure and a delivery_failed router event
    alert_delayed         a delivery still retrying past its alert point -> a delivery_delayed router event, once
    accept_postback       the postback route's write for a verified postback: a ring (or a selftest), at most one
                          write per order every RING_EVERY_S, the rings in between merged into one more
    ring / refused        the doorbell's write, and the route's refusal counter (written at most once a second)

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
from collections import OrderedDict
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any, Literal, get_args

from ..auth.account import AccountPin
from ..config import Settings
from ..errors import ConfigError
from ..timeutil import now_ist, to_ist
from ._wait import wait_any
from .consumers import ConsumersConfig
from .doorbell import DoorbellWorker
from .model import (
    ORDER_ID_RE,
    OrderView,
    is_selftest_id,
    judge,
    login_event,
    login_failed_event,
    login_refused_event,
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
        self.pin = AccountPin.from_settings(settings)
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
        self._stopped = False  # stop() closed the store: start() refuses from then on
        # refused postbacks not yet written: how many, the last one's reason and time (see refused())
        self._refused_count = 0
        self._refused_note: Refusal | None = None
        self._refused_at: dt.datetime | None = None
        self._refused_written_at: dt.datetime | None = None  # the last write of them, or try
        self._refused_timer: asyncio.TimerHandle | None = None
        # the postback route's writes (see accept_postback): when each order id was last written, for RING_EVERY_S at
        # most; the rings held for one more write, and the timers that write them
        self._written: OrderedDict[str, dt.datetime] = OrderedDict()
        self._held: dict[str, int] = {}
        self._held_timers: dict[str, asyncio.TimerHandle] = {}

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

    def note_login(self, access_token: str, *, logged_in_at: dt.datetime, expires_at: dt.datetime) -> int | None:
        """A new token was stored: one `login` event per token (seeded with its fingerprint), whoever reports it (the
        login manager, or the poll for a token stored beside this process)."""
        now = self.clock()
        body = login_event(
            seed=token_fingerprint(access_token), logged_in_at=logged_in_at, expires_at=expires_at, received_at=now
        )
        return self._insert(body, order_id=None, consumers=self.config.matching(body), now=now)

    def note_login_refused(self, access_token: str, *, session_ended: bool) -> int | None:
        """Another account's login was refused: one `login_refused` event per refused session (seeded with a
        fingerprint of that session's token, which is never stored)."""
        now = self.clock()
        body = login_refused_event(seed=token_fingerprint(access_token), session_ended=session_ended, received_at=now)
        return self._insert(body, order_id=None, consumers=self.config.matching(body), now=now)

    def note_login_failed(self, seed: str, *, reason: str) -> int | None:
        """A real login on a link this server issued did not complete: one `login_failed` event per link. `seed` is
        the digest of the link's state (never the state itself); `reason` one of LOGIN_FAILURES (events/model.py),
        ValueError for any other."""
        now = self.clock()
        body = login_failed_event(seed=seed, reason=reason, received_at=now)
        return self._insert(body, order_id=None, consumers=self.config.matching(body), now=now)

    def record_selftest(self, order_id: str) -> int | None:
        """A verified selftest postback: counted in selftest_received every time, stored as an event once per id (a
        repeat returns None). ValueError, with nothing counted, for an id that is not a selftest id."""
        now = self.clock()
        body = selftest_event(order_id, received_at=now)
        return self._insert(
            body, order_id=order_id, consumers=self.config.matching(body), now=now, counter="selftest_received"
        )

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
        """The router event `name` about `about`, None when `about` is a router event itself: nothing is raised about a
        router event, so two failing consumers cannot report each other's failures forever."""
        if about.get("kind") == "router":
            log.warning("order events: %s for %s about a router event (not reported further)", name, consumer)
            return None
        return router_event(name, consumer=consumer, about=about, error=error, seed=seed, received_at=now)

    # --- the postback route's side ----------------------------------------------------------------------------------

    def ring(self, order_id: str, *, rings: int = 1) -> None:
        """Keep the doorbell of `order_id` on disk and count its `rings` (postback_accepted) in one transaction, then
        wake the worker that re-reads the order from Kite. At once: the postback route goes through accept_postback.
        ValueError for an id that is not an order id; StoreError when the store cannot write, and then nothing was
        kept."""
        if not isinstance(order_id, str) or not ORDER_ID_RE.fullmatch(order_id):
            raise ValueError("not an order id")
        self.store.ring(order_id, self.clock(), rings=rings)
        self._tell(self._doorbell_listeners)

    RING_EVERY_S = 2.0  # the postback route writes for one order id at most this often (see accept_postback)
    WRITTEN_MAX = 1000  # the most order ids remembered for that, each for RING_EVERY_S

    def accept_postback(self, order_id: str) -> bool:
        """The postback route's write for a verified postback: record a selftest (record_selftest), or ring the doorbell
        of an order (ring). True when it was written now, False when it was not needed or is held.

        A captured postback with a field changed that the checksum does not cover is a new postback each time, and each
        write waits for the disk on the event loop. So one order id is written at most once every RING_EVERY_S. A ring
        sooner is held: the rings held for an order are written as one when its interval ends (by a timer, or by stop()
        through flush_rings), so a burst of real updates still gets one more read from Kite after its last ring, and a
        replayed burst costs one write and one read every RING_EVERY_S. A selftest sooner is not written at all: its
        event is stored once per id anyway. Without a running event loop (no timer) the ring is written at once.

        ValueError for an id that is neither an order id nor a selftest id; StoreError when the store cannot write,
        and then nothing was kept now (rings held before stay held)."""
        selftest = is_selftest_id(order_id)
        if not selftest and (not isinstance(order_id, str) or not ORDER_ID_RE.fullmatch(order_id)):
            raise ValueError("not an order id")
        now = self.clock()
        wait = self._wait_left(order_id, now)
        if wait is not None and not self._stopped:
            if selftest:
                return False
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None  # nothing could write it later: written now, below
            if loop is not None:
                self._held[order_id] = self._held.get(order_id, 0) + 1
                if order_id not in self._held_timers:
                    self._held_timers[order_id] = loop.call_later(wait, self._write_held, order_id)
                return False
        if selftest:
            self.record_selftest(order_id)
        else:
            self.ring(order_id, rings=1 + self._held.get(order_id, 0))  # with any held rings: one write for all
            self._held.pop(order_id, None)
            timer = self._held_timers.pop(order_id, None)
            if timer is not None:
                timer.cancel()
        self._remember_write(order_id, now)
        return True

    def flush_rings(self) -> None:
        """Write every ring held by accept_postback now (stop() does, before it closes the store)."""
        for order_id in list(self._held):
            self._write_held(order_id)

    def _write_held(self, order_id: str) -> None:
        """Write the rings held for `order_id` as one (its timer, or flush_rings). A store that cannot write is logged,
        and those rings are dropped: the poll catches the order's change."""
        timer = self._held_timers.pop(order_id, None)
        if timer is not None:
            timer.cancel()  # harmless from the timer's own callback
        rings = self._held.pop(order_id, 0)
        if not rings:
            return
        try:
            self.ring(order_id, rings=rings)
        except StoreError as exc:
            log.warning(
                "order events: could not keep %d held postback ring(s) (the poll will catch up): %s", rings, exc
            )
            return
        self._remember_write(order_id, self.clock())

    def _wait_left(self, order_id: str, now: dt.datetime) -> float | None:
        """The seconds left until `order_id` may be written again, None when it may be now."""
        at = self._written.get(order_id)
        if at is None or not self._fresh(at, now):
            return None
        return (at + dt.timedelta(seconds=self.RING_EVERY_S) - now).total_seconds()

    def _fresh(self, at: dt.datetime, now: dt.datetime) -> bool:
        """A write at `at` is less than RING_EVERY_S old; a clock that went back holds nothing."""
        return dt.timedelta(0) <= now - at < dt.timedelta(seconds=self.RING_EVERY_S)

    def _remember_write(self, order_id: str, now: dt.datetime) -> None:
        """Note the write of `order_id`, and forget the writes too old to matter (at most WRITTEN_MAX are kept)."""
        self._written.pop(order_id, None)
        self._written[order_id] = now
        while self._written and (
            len(self._written) > self.WRITTEN_MAX or not self._fresh(next(iter(self._written.values())), now)
        ):
            self._written.popitem(last=False)  # the oldest first

    REFUSAL_WRITE_S = 1.0  # refused postbacks are written to the store at most this often

    def refused(self, reason: Refusal) -> None:
        """Count a refused postback (a wrong secret shows up here as "bad checksum"). `reason` is one of REFUSALS,
        never text from the request: it is shown to MCP clients. Any other reason is a bug in the caller and raises
        ValueError; a store that cannot write is only logged.

        Anyone can send junk to the route, and each write of the store waits for the disk (synchronous=FULL) on the
        event loop, so refusals are counted in memory and written at most once every REFUSAL_WRITE_S, in one write
        (postback_refused moves by their number; its note is the last one's reason): the first after a quiet spell
        is written at once, the ones after it by a timer when the interval is over, by the next refusal after it, or
        by stop(). Once stop() has begun they are left to it, and after it nothing is written (the store is closed)."""
        if reason not in REFUSALS:
            raise ValueError("refused() takes one of REFUSALS")
        now = self.clock()
        self._refused_count += 1
        self._refused_note = reason
        self._refused_at = now
        if self._stopped:
            return  # no write and no timer: stop() writes what it finds, then closes the store
        interval = dt.timedelta(seconds=self.REFUSAL_WRITE_S)
        last = self._refused_written_at
        if last is None or not dt.timedelta(0) <= now - last < interval:  # a clock that went back holds nothing
            self.flush_refusals()
        elif self._refused_timer is None:
            with contextlib.suppress(RuntimeError):  # no running event loop: the next refusal or stop() writes them
                delay = (last + interval - now).total_seconds()
                self._refused_timer = asyncio.get_running_loop().call_later(delay, self.flush_refusals)

    def flush_refusals(self) -> None:
        """Write the refusals counted in memory, in one write. A store that cannot write is logged, and they stay
        counted for the next try."""
        timer, self._refused_timer = self._refused_timer, None
        if timer is not None:
            timer.cancel()
        if not self._refused_count or self._refused_at is None:
            return
        count = self._refused_count
        self._refused_written_at = self.clock()
        try:
            self.store.bump("postback_refused", self._refused_at, self._refused_note, by=count)
        except StoreError as exc:
            log.warning("order events: could not count %d refused postback(s): %s", count, exc)
            return
        self._refused_count = 0

    def user_allowed(self, user_id: object) -> bool:
        """With an account pin (KITE_MCP_ALLOWED_USER_ID or KITE_MCP_ALLOWED_USER_ID_SHA256), a postback must name that
        account."""
        return self.pin.allows(user_id)

    # --- status ---------------------------------------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        return describe_status(self.store, self.settings, self.clock(), [c.name for c in self.config.consumers])

    # --- running --------------------------------------------------------------------------------------------------

    HOUSEKEEPING_S = 6 * 3600
    STOP_TIMEOUT_S = 10  # how long stop() waits for the parts before it cancels them

    @property
    def running(self) -> bool:
        """True between start() and stop()."""
        return bool(self._tasks)

    async def start(self) -> None:
        """Run the poll, the doorbell worker, the router and the housekeeping as tasks. Raises ConfigError when another
        process already serves this store, or when this service was stopped (stop() closed its store)."""
        if self._tasks:
            return
        if self._stopped:
            raise ConfigError("a stopped event service cannot be restarted: build a new one")
        self._take_lock()
        self._stop = asyncio.Event()
        jobs = {
            "poller": self.poller.run,
            "doorbells": self.doorbells.run,
            "router": self.router.run,
            "housekeeping": self._housekeeping,
        }
        self._tasks = [asyncio.create_task(job(self._stop), name=f"kite-events-{name}") for name, job in jobs.items()]
        for task in self._tasks:
            task.add_done_callback(self._part_ended)
        log.info("order events: started, %d consumer(s)", len(self.config.consumers))

    async def stop(self) -> None:
        """Stop the tasks (a part still busy after STOP_TIMEOUT_S is cancelled; a consumer command still running is
        killed, and its delivery stays pending for the next server), then close the store and release the lock. A
        stop() that is itself cancelled still cancels the parts, closes the store and releases the lock. Calling it
        again, or without start(), is harmless: only the first call writes and closes (a refusal counted after it stays
        unwritten); start() refuses afterwards."""
        first = not self._stopped
        self._stopped = True
        tasks = self._tasks
        try:
            if tasks and self._stop is not None:
                self._stop.set()
                await asyncio.wait(tasks, timeout=self.STOP_TIMEOUT_S)
        finally:
            try:
                for task in tasks:
                    task.cancel()  # does nothing to a part that has ended
                await asyncio.gather(*tasks, return_exceptions=True)
            finally:
                self._tasks = []
                if first:
                    try:
                        self.flush_rings()  # both log a store that cannot write, rather than raise
                        self.flush_refusals()
                    finally:
                        # the store first, so that the lock covers this process's last use of the file (SQLite
                        # tidies its WAL on close); both even when the flush fails
                        self.store.close()
                        self._release_lock()
                        if tasks:
                            log.info("order events: stopped")

    def _part_ended(self, task: asyncio.Task[None]) -> None:
        """Each part's done-callback: a part that ends while the service runs (its loop crashed, or something cancelled
        it) is an error, as nothing runs it again until the server restarts. Ending after stop() is quiet."""
        if self._stop is None or self._stop.is_set():
            return
        if task.cancelled():
            log.error("order events: %s was cancelled while the service was running; it stays down", task.get_name())
        elif (exc := task.exception()) is not None:
            log.error(
                "order events: %s failed while the service was running; it stays down", task.get_name(), exc_info=exc
            )
        else:
            log.error("order events: %s ended while the service was running; it stays down", task.get_name())

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
            raise ConfigError(f"cannot open the lock file {path}: {exc.strerror or type(exc).__name__}") from None
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            if isinstance(exc, BlockingIOError):
                raise ConfigError(f"another process is already serving order events from {self.store.path}") from None
            raise ConfigError(f"cannot lock {path}: {exc.strerror or type(exc).__name__}") from None
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
    store: EventStore,
    settings: Settings,
    now: dt.datetime,
    consumer_names: list[str] | None = None,
    *,
    with_poll: bool = True,
) -> dict[str, Any]:
    """What `events status` and `get_event_status` show. No payloads, no ids of orders.

    `poll` and `warning` come from the poll settings in `settings`. A process that only reads another's store
    (with_poll=False) may not share them, so it leaves both out and points to the serving process instead.

    `warning` is set while no poll has succeeded after the most recent close of the poll window (meta after_close_ok
    is older than last_closed_poll_day), so a failed Friday close stays visible over the weekend and on Monday until
    Monday's own close is polled. A store the poll has never touched (neither baseline_done nor after_close_ok set) has
    missed nothing, and shows no warning; nor does a close before the day of the first poll (the date of baseline_done),
    which the service was not running to see. A baseline_done that cannot be read hides no warning."""
    out = store.summary(consumer_names or ())
    out["store"] = str(store.path)
    if consumer_names is not None:
        out["configured_consumers"] = consumer_names
    if not with_poll:
        out["poll"] = "see the serving process's status"
        return out
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
