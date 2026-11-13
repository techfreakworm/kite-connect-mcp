"""The router: runs each consumer's command for the events stored for it, and records how each delivery ended.

The store holds a pending delivery per matching consumer for every event, so a restart loses nothing: a new router
picks up where the last one stopped. Each consumer has a worker of its own, which runs one command at a time; an
order's events go to it one at a time and in order, and an order still retrying never holds up the others.

The consumer contract:
  stdin    one event as JSON, then a newline
  env      PATH, HOME, LANG, KITE_EVENT_CONSUMER=<name>, and the consumer's env_pass names; never KITE_*
  stdout   discarded
  stderr   its end becomes the delivery's error: one line, control characters dropped, links as "[link]", clipped.
           It reaches the service's log, `events status` and the other consumers' delivery_failed events, so never
           print a secret to it (a token in a path is not recognised as a link)
  exit 0   handled (including a refusal the consumer reported itself): done
  exit 75  temporary failure: retried (10 s, 30 s, 1 min, 2 min, 5 min, then every 5 min, for 30 min in all)
  signal   killed by a signal (a service stop, the OOM killer): like 75. Only the command itself counts: a shell
           script whose child is killed exits 128+N, a permanent failure, so it should exit 75 itself then
  timeout  the process group is killed: like 75. A command has ended when it has exited and its stderr is closed, so
           a process it starts in the background must not keep stderr open (redirect it), or the delivery times out
  other    permanent failure: `delivery_failed` at once, no retry
A command that cannot start fails for good, unless the system is short of something for a while (EAGAIN, ENOMEM,
EMFILE, ENFILE, ETXTBSY): then it is retried like 75. A delivery still retrying past its alert point raises
`delivery_delayed` once. Deliveries are at least once (a crash between the command and the bookkeeping repeats it),
so a consumer must be idempotent on event_id.

The program is checked again before each run (events/trust.py): a program that someone else could change since the
consumers file was loaded, or that is gone, is a permanent failure and is not run. The log names a consumer and its
program, never the command's arguments (they may hold what belongs in env_pass) nor the event.

Only the last STDERR_KEEP bytes of stderr are kept while it is read, not after, so a command that floods it cannot
fill the server's memory. A failure is written with its delivery_failed event, and an alert's mark with its
delivery_delayed event, in one transaction (EventService.fail_delivery, alert_delayed).
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import errno
import json
import logging
import os
import re
import signal
from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING, Any

from ._wait import wait_any
from .consumers import EXIT_RETRY, Consumer, consumer_env
from .model import MAX_ERROR_CHARS, clean_error_text, clip_words
from .store import Delivery
from .trust import check_owned_file

if TYPE_CHECKING:
    from .service import EventService

log = logging.getLogger(__name__)

STDERR_KEEP = 4096  # the bytes of a command's stderr kept, from its end: plenty for an error, never more
_READ_CHUNK = 65536
_TEMPORARY_START_ERRNOS = frozenset({errno.EAGAIN, errno.ENOMEM, errno.EMFILE, errno.ENFILE, errno.ETXTBSY})
_FIRST_WORD_RE = re.compile(r"^\S*")

Spawn = Callable[..., Awaitable[asyncio.subprocess.Process]]


async def _read_tail(stream: asyncio.StreamReader) -> tuple[bytes, bool]:
    """Read `stream` to its end, keeping only its last STDERR_KEEP bytes. Returns them, and whether anything before
    them was dropped."""
    tail = bytearray()
    cut = False
    while chunk := await stream.read(_READ_CHUNK):
        tail += chunk
        if len(tail) > STDERR_KEEP:
            del tail[:-STDERR_KEEP]
            cut = True
    return bytes(tail), cut


async def _feed(stdin: asyncio.StreamWriter, data: bytes) -> None:
    """Write the event and close stdin. A command that exits without reading it is not an error here: its exit code
    tells how it went."""
    try:
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            stdin.write(data)
            await stdin.drain()
    finally:
        stdin.close()


async def _converse(proc: asyncio.subprocess.Process, data: bytes) -> tuple[bytes, bool]:
    """Give the command its event, read the end of its stderr (see _read_tail) and wait for it to exit."""
    assert proc.stdin is not None  # spawned with stdin and stderr as pipes
    assert proc.stderr is not None
    reading = asyncio.ensure_future(_read_tail(proc.stderr))
    try:
        await _feed(proc.stdin, data)
        tail = await reading
        await proc.wait()
    finally:
        reading.cancel()
    return tail


async def _reap(proc: asyncio.subprocess.Process) -> None:
    """After a kill: read what is left of stderr, so that the end of the pipe is seen (a pipe left full is no longer
    read), and wait for the process."""
    assert proc.stderr is not None
    await asyncio.gather(_read_tail(proc.stderr), proc.wait())


def _stderr_text(data: bytes, cut: bool) -> str:
    """The kept end of a command's stderr as clean text (model.clean_error_text). When its start was dropped, the
    first word can be the end of a longer one, a link that cleaning would no longer recognise among them: it goes."""
    text = data.decode("utf-8", "replace")
    if cut:
        text = _FIRST_WORD_RE.sub("", text, count=1)
    return clean_error_text(text)


def _error(head: str, stderr: str) -> str:
    """`head` ("exit 3"), then as much of the end of the clean `stderr` as fits with it in MAX_ERROR_CHARS, from the
    start of a word: a cut inside one drops what is left of it, so the error never starts with part of a "[link]"."""
    room = MAX_ERROR_CHARS - len(head) - 2
    if room <= 0 or not stderr:
        return head
    tail = stderr[-room:]
    if len(stderr) > room and not stderr[-room - 1].isspace():
        tail = _FIRST_WORD_RE.sub("", tail, count=1)
    tail = tail.lstrip()
    return f"{head}: {tail}" if tail else head


def _signal_name(number: int) -> str:
    try:
        return signal.Signals(number).name
    except ValueError:
        return f"signal {number}"


def _ending(code: int) -> str:
    """How a command with return code `code` ended: "exited 3", or "was killed by SIGTERM" (a negative code)."""
    return f"was killed by {_signal_name(-code)}" if code < 0 else f"exited {code}"


def _kill(proc: asyncio.subprocess.Process) -> None:
    """Kill the command and whatever it started (it runs in a session, so a process group, of its own)."""
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)


class Router:
    """Delivers stored events to the configured consumers: one command at a time per consumer, each order's events in
    order, and an order still retrying never holds up the others."""

    IDLE_S = 30.0  # the longest a worker sleeps: a change from another process (events resume, retry) wakes no one
    PAUSED_S = 5.0  # how often a paused consumer is looked at again

    def __init__(self, service: EventService, *, environ: Mapping[str, str] | None = None, spawn: Spawn | None = None):
        self.service = service
        self.store = service.store
        self.config = service.config
        self.clock = service.clock
        self._environ = dict(os.environ if environ is None else environ)
        self._spawn: Spawn = spawn or asyncio.create_subprocess_exec
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
                log.exception(
                    "order events: the loop for consumer %s failed; retrying in %d s", consumer.name, self.IDLE_S
                )
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
                "order events: event %d for %s will be retried in %d s (%s)", delivery.seq, consumer.name, delay, error
            )
            return "retry"
        if not self.service.fail_delivery(consumer.name, delivery, error, now):  # with its delivery_failed event
            return self._decided_elsewhere(consumer, delivery)
        log.warning("order events: delivering event %d to %s failed: %s", delivery.seq, consumer.name, error)
        return "failed"

    def _decided_elsewhere(self, consumer: Consumer, delivery: Delivery) -> str:
        """The delivery was skipped (events resume --skip-backlog) or pruned while it ran: leave it as it is."""
        log.info(
            "order events: event %d for %s was changed while it was delivered; left as it is",
            delivery.seq,
            consumer.name,
        )
        return "skipped"

    def _report_delays(self, consumer: Consumer, now: dt.datetime) -> None:
        """Raise `delivery_delayed` once for each delivery of `consumer` still retrying past its alert point: the mark
        and the event are written together, so a store error leaves the alert due, not lost."""
        for delivery in self.store.overdue_alerts(consumer.name, now, consumer.alert_after_s):
            self.service.alert_delayed(consumer.name, delivery, now)

    async def run_command(self, consumer: Consumer, body: Mapping[str, Any]) -> tuple[str, str | None]:
        """("done" | "retry" | "failed", error text or None): see the contract in the module docstring. The event
        goes on stdin; stdout is discarded; the end of stderr, cleaned, ends the error. An error is clean text of at
        most MAX_ERROR_CHARS, the same in the log, the store and the router event."""
        data = (json.dumps(body, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        # again now (a chmod since loading is caught), off the event loop: user and group lookups can be slow
        problem = await asyncio.to_thread(check_owned_file, consumer.command[0], "the program")
        if problem is not None:
            return "failed", clip_words(clean_error_text(problem), MAX_ERROR_CHARS)
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
            outcome = "retry" if exc.errno in _TEMPORARY_START_ERRNOS else "failed"  # short of processes, memory, files
            return outcome, f"cannot start the command: {exc.strerror or type(exc).__name__}"
        try:
            err, cut = await asyncio.wait_for(_converse(proc, data), consumer.timeout_s)
        except TimeoutError:
            exited = proc.returncode  # set when the command itself has exited: then stderr is held by what it started
            _kill(proc)
            with contextlib.suppress(Exception):  # read the rest, so the pipe's end is seen and the process is reaped
                await asyncio.wait_for(_reap(proc), 5)
            error = f"timed out after {consumer.timeout_s:g} s"
            if exited is not None:
                error += f": the command {_ending(exited)}, but a process it started kept stderr open"
            return "retry", error
        except BaseException:  # cancelled as the router stops, or anything else: never leave the command unwatched
            _kill(proc)
            raise
        code = proc.returncode
        if code == 0:
            return "done", None
        stderr = _stderr_text(err, cut)
        if code is not None and code < 0:
            return "retry", _error(f"killed by {_signal_name(-code)}", stderr)
        if code == EXIT_RETRY:
            return "retry", _error(f"exit {EXIT_RETRY}", stderr)
        return "failed", _error(f"exit {code}", stderr)
