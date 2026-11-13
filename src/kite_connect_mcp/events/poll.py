"""The order-book poll: the backup for postbacks, and the only source for orders placed outside this API key.

GET /orders every KITE_MCP_ORDER_POLL_INTERVAL_S (60) seconds on KITE_MCP_ORDER_POLL_DAYS (Mon-Fri) inside
KITE_MCP_ORDER_POLL_WINDOW (09:00-16:00 IST). It also runs once when the service starts, at once when a new token
appears (a login), and after the window ends until one poll succeeds, so the day's final statuses are caught (Kite's
order book holds only today's orders). The polls at startup and after a login first run the doorbells that waited for a
token; the other polls leave the doorbells to the doorbell worker's own round, so a Kite outage does not use up a
doorbell's tries twice as fast.

It catches a lost postback, a stretch when the service was down (on the same day), orders placed from Kite's web site or
app, and orders a GTT triggered. The first poll ever records the book as a baseline and tells nobody. Without a valid
token it skips quietly, as it does when the doorbells it ran found the token rejected: a token Kite refused is never
sent again. If Kite refuses the token, the token is marked rejected and `token_rejected` is raised once. An answer that
is not a list of orders is a failed poll (poll_failed), so it never stands in for the day's last poll.

Every failed poll is counted in poll_failed, but logged only when the polls start failing and when one works again, so
an outage logs two lines, not one a minute. Nothing that fails stops run(): it logs, and the next tick tries again.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Literal

from ..errors import KiteMCPError, TokenError
from ..timeutil import to_ist
from ._wait import wait_any
from .model import token_fingerprint
from .store import iso

if TYPE_CHECKING:
    from ..config import Settings
    from .service import EventService

log = logging.getLogger(__name__)

Reason = Literal["startup", "window", "login", "after_close"]  # why a poll ran: the poll_ok note the status shows
DOORBELL_REASONS: tuple[Reason, ...] = ("startup", "login")  # the polls that first run the waiting doorbells
CHECK_LOG_EVERY = dt.timedelta(minutes=1)  # while due() keeps failing, one short log line this often at most


def in_poll_window(settings: Settings, now: dt.datetime) -> bool:
    """A poll day, inside its poll window: the time of the polls every interval."""
    local = to_ist(now)
    start, end = settings.order_poll_window
    return local.weekday() in settings.order_poll_days and start <= local.time() < end


def past_poll_window(settings: Settings, now: dt.datetime) -> bool:
    """A poll day, at or after the end of its poll window: a poll that works now is that day's poll after the close."""
    local = to_ist(now)
    return local.weekday() in settings.order_poll_days and local.time() >= settings.order_poll_window[1]


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


class Poller:
    """Polls Kite's order book: poll() once, tick() when due() says so, run() at start and every TICK_S until
    stopped."""

    TICK_S = 5.0  # how often run() asks due() whether a poll is due

    def __init__(self, service: EventService):
        self.service = service
        self.settings = service.settings
        self.interval = dt.timedelta(seconds=service.settings.order_poll_interval_s)
        self._token_seen: str | None = None  # the fingerprint of the last token due() saw
        self._next_at: dt.datetime | None = None
        self._failing = False  # the polls are failing, and that has been logged
        self._check_logged_at: dt.datetime | None = None  # due() is failing: when that was last logged

    # --- when ------------------------------------------------------------------------------------------------------

    def in_window(self, now: dt.datetime) -> bool:
        return in_poll_window(self.settings, now)

    def _after_close(self, now: dt.datetime) -> bool:
        return (
            past_poll_window(self.settings, now)
            and self.service.store.meta("after_close_ok") != to_ist(now).date().isoformat()
        )

    def _current_token(self) -> str | None:
        record = self.service.tokens.current()
        return token_fingerprint(record.access_token) if record is not None else None

    def prime(self) -> None:
        """Remember the token in use now, so that only a new one counts as a login."""
        self._token_seen = self._current_token()

    def mark_polled(self, now: dt.datetime) -> None:
        self._next_at = now + self.interval

    def due(self, now: dt.datetime) -> Reason | None:
        """Why a poll is due now ("login", "window" or "after_close"), or None. Not a pure question: it remembers the
        token it saw, so a new token is "login" only once, and a caller that gets "login" must poll."""
        token = self._current_token()
        if token is None:
            return None
        if token != self._token_seen:
            self._token_seen = token
            return "login"
        if self._next_at is not None and now < self._next_at:
            return None
        if self.in_window(now):
            return "window"
        if self._after_close(now):
            return "after_close"
        return None

    # --- what --------------------------------------------------------------------------------------------------------

    async def poll(self, reason: Reason) -> bool:
        """One poll. True when Kite's order book was read and stored."""
        svc = self.service
        now = svc.clock()
        self.mark_polled(now)
        record = svc.tokens.current()
        if record is not None and reason in DOORBELL_REASONS:
            await svc.doorbells.process()
            # read the token again: the doorbells may have found it rejected (it is never sent again), and another
            # process may have replaced the file
            record = svc.tokens.current()
        if record is None:
            return False
        try:
            # the token checked last is the one sent, rejected and reported
            rows = await svc.client.request("GET", "/orders", access_token=record.access_token)
        except TokenError:
            log.warning("order events: Kite rejected the access token; the poll waits for a new login")
            svc.note_token_rejected(record.access_token)
            svc.store.bump("poll_failed", now, "Kite rejected the access token")
            return False
        except KiteMCPError as exc:
            return self._failed(now, svc.client.scrub(str(exc))[:200])
        if not isinstance(rows, list):
            return self._failed(now, "Kite's answer is not an order list")
        baseline = svc.store.meta("baseline_done") is None
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            try:
                svc.ingest_order(row, source="poll", baseline=baseline)
            except ValueError:
                continue
        if baseline:
            svc.store.set_meta("baseline_done", iso(now))
        svc.store.bump("poll_ok", now, reason)
        if past_poll_window(self.settings, now):
            svc.store.set_meta("after_close_ok", to_ist(now).date().isoformat())
        if self._failing:
            self._failing = False
            log.info("order events: the order-book poll works again")
        return True

    def _failed(self, now: dt.datetime, note: str) -> bool:
        """Count a failed poll, with `note` (shown in the status); log it only when the polls start failing."""
        self.service.store.bump("poll_failed", now, note)
        if not self._failing:
            self._failing = True
            log.warning("order events: the order-book poll failed: %s (not logged again until a poll works)", note)
        return False

    async def run(self, stop: asyncio.Event) -> None:
        """Poll now, then tick() every TICK_S, until `stop` is set."""
        try:
            self.prime()
        except Exception:
            # no token is remembered, so the first one a tick can read counts as a login and is polled at once
            log.exception("order events: could not read the token at start; the poll tries again")
        await self._safe_poll("startup")
        while not stop.is_set():
            await wait_any(self.TICK_S, stop)
            if stop.is_set():
                break
            await self.tick()

    async def tick(self) -> None:
        """One step of run(): poll if due() says so. A due() that raises (a store that cannot be read) is logged with
        its traceback when it starts failing, then in one short line every CHECK_LOG_EVERY at most, until it works."""
        now = self.service.clock()
        try:
            reason = self.due(now)
        except Exception as exc:
            if self._check_logged_at is None:
                log.exception("order events: could not tell whether a poll is due; the next tick tries again")
            elif now - self._check_logged_at >= CHECK_LOG_EVERY:
                log.warning("order events: still cannot tell whether a poll is due (%s)", type(exc).__name__)
            else:
                return
            self._check_logged_at = now
            return
        if self._check_logged_at is not None:
            self._check_logged_at = None
            log.info("order events: telling whether a poll is due works again")
        if reason is not None:
            await self._safe_poll(reason)

    async def _safe_poll(self, reason: Reason) -> bool:
        """poll(), with an error logged instead of raised: its traceback when the polls start failing."""
        try:
            return await self.poll(reason)
        except Exception:
            if not self._failing:
                self._failing = True
                log.exception("order events: the order-book poll failed (not logged again until a poll works)")
            return False
