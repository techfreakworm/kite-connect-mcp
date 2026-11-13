"""The router: runs each consumer's command for the events stored for it, and records how each delivery ended.

The store holds a pending delivery per matching consumer for every event, so a restart loses nothing: a new router
picks up where the last one stopped. Each consumer has a worker of its own, which runs one command at a time; an
order's events go to it one at a time and in order, and an order still retrying never holds up the others.

The consumer contract:
  stdin    one event as JSON, then a newline
  env      PATH, HOME, LANG, KITE_EVENT_CONSUMER=<name>, and the consumer's env_pass names; never KITE_*
  exit 0   handled (including a refusal the consumer reported itself): done
  exit 75  temporary failure: retried (10 s, 30 s, 1 min, 2 min, 5 min, then every 5 min, for 30 min in all)
  timeout  the process group is killed: like 75
  other    permanent failure: `delivery_failed` at once, no retry
A delivery still retrying past its alert point raises `delivery_delayed` once. Deliveries are at least once (a crash
between the command and the bookkeeping repeats it), so a consumer must be idempotent on event_id.

The program is checked again before each run (events/trust.py): a program that someone else could change since the
consumers file was loaded, or that is gone, is a permanent failure and is not run. The log names a consumer and its
program, never the command's arguments (they may hold what belongs in env_pass) nor the event.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import json
import logging
import os
import signal
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from ._wait import wait_any
from .consumers import EXIT_RETRY, Consumer, consumer_env
from .model import MAX_ERROR_CHARS
from .store import Delivery, iso
from .trust import check_owned_file

if TYPE_CHECKING:
    from .service import EventService

log = logging.getLogger(__name__)


def _tail(data: bytes | None) -> str:
    """The end of a command's stderr, on one line."""
    text = " ".join((data or b"").decode("utf-8", "replace").split())
    return text[-MAX_ERROR_CHARS:]


def _kill(proc: asyncio.subprocess.Process) -> None:
    """Kill the command and whatever it started (it runs in a session, so a process group, of its own)."""
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)


class Router:
    """Delivers stored events to the configured consumers: one command at a time per consumer, each order's events in
    order, and an order still retrying never holds up the others."""

    IDLE_S = 30.0  # the longest a worker sleeps: a change from another process (events resume, retry) wakes no one
    PAUSED_S = 5.0  # how often a paused consumer is looked at again

    def __init__(self, service: EventService, *, environ: Mapping[str, str] | None = None, spawn: Any = None):
        self.service = service
        self.store = service.store
        self.config = service.config
        self.clock = service.clock
        self._environ = dict(os.environ if environ is None else environ)
        self._spawn = spawn or asyncio.create_subprocess_exec
        self._wake = {c.name: asyncio.Event() for c in self.config.consumers}
        service.on_event(self.wake)

    def wake(self) -> None:
        """Make every worker look for work now. Call it on the event loop's thread (asyncio.Event is not
        thread-safe)."""
        for event in self._wake.values():
            event.set()

    async def run(self, stop: asyncio.Event) -> None:
        """Run a worker per consumer until `stop` is set; a command still running then is killed, and its delivery
        stays pending for the next start."""
        workers = [
            asyncio.create_task(self._worker(c, stop), name=f"kite-events-consumer-{c.name}")
            for c in self.config.consumers
        ]
        try:
            await stop.wait()
        finally:
            for worker in workers:
                worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)

    async def _worker(self, consumer: Consumer, stop: asyncio.Event) -> None:
        wake = self._wake[consumer.name]
        while not stop.is_set():
            wake.clear()
            try:
                wait = await self.step(consumer)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("router: the loop for consumer %s failed; retrying in %d s", consumer.name, self.IDLE_S)
                wait = self.IDLE_S
            if wait is not None:
                await wait_any(wait, wake, stop)

    async def step(self, consumer: Consumer) -> float | None:
        """Do one thing for `consumer`: report overdue retries, then run one due delivery. Returns None when it ran a
        delivery (look again at once), else how many seconds to wait before looking again."""
        now = self.clock()
        self._report_delays(consumer, now)
        if self.store.is_paused(consumer.name):
            return self.PAUSED_S
        delivery, later = self.store.next_delivery(consumer.name, now)
        if delivery is not None:
            await self.deliver(consumer, delivery)
            return None
        waits = [self.IDLE_S]
        if later is not None:
            waits.append((later - now).total_seconds())
        alert = self.store.next_alert_at(consumer.name, consumer.alert_after_s)
        if alert is not None:
            waits.append((alert - now).total_seconds())
        return max(0.05, min(waits))

    async def deliver(self, consumer: Consumer, delivery: Delivery) -> str:
        """Run the consumer for one delivery and record the outcome: done, retry, failed or skipped."""
        started = self.clock()
        body = self.store.event(delivery.seq)
        if body is None:
            self.store.finish_delivery(consumer.name, delivery.seq, "skipped", started, "the event is no longer stored")
            return "skipped"
        outcome, error = await self.run_command(consumer, body)
        now = self.clock()
        if outcome == "done":
            if not self.store.finish_delivery(consumer.name, delivery.seq, "done", now):
                return self._decided_elsewhere(consumer, delivery)
            return "done"
        first = delivery.first_try_at or started
        if outcome == "retry" and (now - first).total_seconds() < consumer.give_up_after_s:
            delay = consumer.retry_delay(delivery.attempts + 1)
            if not self.store.retry_delivery(
                consumer.name,
                delivery.seq,
                first_try_at=first,
                next_try_at=now + dt.timedelta(seconds=delay),
                now=now,
                error=error,
            ):
                return self._decided_elsewhere(consumer, delivery)
            log.warning(
                "router: event %d for %s will be retried in %d s (%s)", delivery.seq, consumer.name, delay, error
            )
            return "retry"
        if not self.store.finish_delivery(consumer.name, delivery.seq, "failed", now, error):
            return self._decided_elsewhere(consumer, delivery)
        log.warning("router: delivering event %d to %s failed: %s", delivery.seq, consumer.name, error)
        self.service.record_router_event(
            "delivery_failed", consumer=consumer.name, about=body, error=error, seed=iso(now)
        )
        return "failed"

    def _decided_elsewhere(self, consumer: Consumer, delivery: Delivery) -> str:
        """The delivery was skipped (events resume --skip-backlog) or pruned while it ran: leave it as it is."""
        log.info(
            "router: event %d for %s was changed while it was delivered; left as it is", delivery.seq, consumer.name
        )
        return "skipped"

    def _report_delays(self, consumer: Consumer, now: dt.datetime) -> None:
        """Raise `delivery_delayed` once for each delivery of `consumer` still retrying past its alert point."""
        for delivery in self.store.overdue_alerts(consumer.name, now, consumer.alert_after_s):
            if not self.store.mark_alerted(consumer.name, delivery.seq):
                continue
            body = self.store.event(delivery.seq)
            if body is not None and delivery.first_try_at is not None:
                self.service.record_router_event(
                    "delivery_delayed",
                    consumer=consumer.name,
                    about=body,
                    error=delivery.last_error,
                    seed=iso(delivery.first_try_at),
                )

    async def run_command(self, consumer: Consumer, body: Mapping[str, Any]) -> tuple[str, str | None]:
        """("done" | "retry" | "failed", error text or None). The event goes on stdin; stdout is discarded; the end of
        stderr becomes the error."""
        data = (json.dumps(body, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        # again now (a chmod since loading is caught), off the event loop: user and group lookups can be slow
        problem = await asyncio.to_thread(check_owned_file, consumer.command[0], "the program")
        if problem is not None:
            return "failed", problem
        try:
            proc = await self._spawn(
                *consumer.command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                env=consumer_env(consumer, self._environ),
                start_new_session=True,
            )
        except OSError as exc:
            return "failed", f"cannot start the command: {exc.strerror or type(exc).__name__}"
        try:
            _, err = await asyncio.wait_for(proc.communicate(data), consumer.timeout_s)
        except TimeoutError:
            _kill(proc)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), 5)
            return "retry", f"timed out after {consumer.timeout_s:g} s"
        except asyncio.CancelledError:
            _kill(proc)
            raise
        tail = _tail(err)
        if proc.returncode == 0:
            return "done", None
        if proc.returncode == EXIT_RETRY:
            return "retry", "exit 75" + (f": {tail}" if tail else "")
        return "failed", f"exit {proc.returncode}" + (f": {tail}" if tail else "")
