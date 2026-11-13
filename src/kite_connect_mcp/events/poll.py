"""The order-book poll: the backup for postbacks, and the only source for orders placed outside this API key.

GET /orders every KITE_MCP_ORDER_POLL_INTERVAL_S (60) seconds on KITE_MCP_ORDER_POLL_DAYS (Mon-Fri) inside
KITE_MCP_ORDER_POLL_WINDOW (09:00-16:00 IST). It also runs once when the service starts, at once when a new token
appears (a login: the doorbells that waited for it go first), and after the window ends until one poll succeeds, so the
day's final statuses are caught (Kite's order book holds only today's orders).

It catches a lost postback, a stretch when the service was down (on the same day), orders placed from Kite's web site or
app, and orders a GTT triggered. The first poll ever records the book as a baseline and tells nobody. Without a valid
token it skips quietly; if Kite refuses the token, the token is marked rejected and `token_rejected` is raised once.
An answer that is not a list of orders is a failed poll (poll_failed), so it never stands in for the day's last poll.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING

from ..errors import KiteMCPError, TokenError
from ..timeutil import to_ist
from ._wait import wait_any
from .store import iso

if TYPE_CHECKING:
    from .service import EventService

log = logging.getLogger(__name__)


def _fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8", "replace")).hexdigest()


class Poller:
    """Polls Kite's order book: poll() once, run() at start and whenever due() says so, until stopped."""

    TICK_S = 5.0  # how often run() asks due() whether a poll is due

    def __init__(self, service: EventService):
        self.service = service
        settings = service.settings
        self.interval = dt.timedelta(seconds=settings.order_poll_interval_s)
        self.start, self.end = settings.order_poll_window
        self.days = settings.order_poll_days
        self._token_seen: str | None = None
        self._next_at: dt.datetime | None = None

    # --- when ------------------------------------------------------------------------------------------------------

    def in_window(self, now: dt.datetime) -> bool:
        local = to_ist(now)
        return local.weekday() in self.days and self.start <= local.time() < self.end

    def _after_close(self, now: dt.datetime) -> bool:
        local = to_ist(now)
        return (
            local.weekday() in self.days
            and local.time() >= self.end
            and self.service.store.meta("after_close_ok") != local.date().isoformat()
        )

    def _current_token(self) -> str | None:
        record = self.service.tokens.current()
        return _fingerprint(record.access_token) if record is not None else None

    def prime(self) -> None:
        """Remember the token in use now, so that only a new one counts as a login."""
        self._token_seen = self._current_token()

    def mark_polled(self, now: dt.datetime) -> None:
        self._next_at = now + self.interval

    def due(self, now: dt.datetime) -> str | None:
        """Why a poll is due now ("login", "window" or "after_close"), or None."""
        token = self._current_token()
        if token is not None and token != self._token_seen:
            self._token_seen = token
            return "login"
        if token is None:
            return None
        if self._next_at is not None and now < self._next_at:
            return None
        if self.in_window(now):
            return "window"
        if self._after_close(now):
            return "after_close"
        return None

    # --- what --------------------------------------------------------------------------------------------------------

    async def poll(self, reason: str) -> bool:
        """One poll. True when Kite's order book was read and stored."""
        svc = self.service
        now = svc.clock()
        self.mark_polled(now)
        record = svc.tokens.current()
        if record is None:
            return False
        if svc.doorbells is not None:
            await svc.doorbells.process()
        try:
            # the token checked above is the one sent, rejected and reported (another process may replace the file)
            rows = await svc.client.request("GET", "/orders", access_token=record.access_token)
        except TokenError:
            log.warning("order events: Kite rejected the access token; the poll waits for a new login")
            svc.note_token_rejected(record.access_token)
            svc.store.bump("poll_failed", now, "Kite rejected the access token")
            return False
        except KiteMCPError as exc:
            svc.store.bump("poll_failed", now, svc.client.scrub(str(exc))[:200])
            log.warning("order events: the order-book poll failed: %s", svc.client.scrub(str(exc))[:200])
            return False
        if not isinstance(rows, list):
            log.warning("order events: Kite's answer to the order-book poll is not an order list")
            svc.store.bump("poll_failed", now, "Kite's answer is not an order list")
            return False
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
        local = to_ist(now)
        if local.weekday() in self.days and local.time() >= self.end:
            svc.store.set_meta("after_close_ok", local.date().isoformat())
        return True

    async def run(self, stop: asyncio.Event) -> None:
        """Poll now, then whenever due() says so (asked every TICK_S), until `stop` is set. A poll or a check that
        fails (a store that cannot be read) is logged, and the next tick tries again."""
        self.prime()
        await self._safe_poll("startup")
        while not stop.is_set():
            await wait_any(self.TICK_S, stop)
            if stop.is_set():
                break
            try:
                reason = self.due(self.service.clock())
            except Exception:
                log.exception("order events: could not tell whether a poll is due; the next tick tries again")
                continue
            if reason is not None:
                await self._safe_poll(reason)

    async def _safe_poll(self, reason: str) -> bool:
        try:
            return await self.poll(reason)
        except Exception:
            log.exception("order events: the order-book poll failed")
            return False
