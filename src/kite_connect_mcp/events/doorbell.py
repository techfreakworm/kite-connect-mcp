"""The doorbell: a verified postback is never stored as it came.

Kite's checksum covers only order_id and order_timestamp, so a postback's other fields could be altered or replayed.
The route therefore only rings: it keeps the order id on disk (`doorbells`), and this worker re-reads that order from
Kite (GET /orders/{order_id}, the last row of its history) and stores Kite's answer, as source "postback". A forged or
replayed postback can cause one read, plus retries while Kite fails (at most MAX_TRIES failed reads).

Without a valid token nothing is read: the doorbells wait, and the poll that a new login starts at once runs them first
(so does the poll at startup; the other polls leave them to this worker's own round). The token the worker checks is
the one it sends, so a rejection is reported for the token Kite refused.

Each doorbell is settled on its own. A failed read (an error from Kite, an answer that is not an order history, a bug)
keeps the doorbell for a later round, counts a try, and the round moves on to the next one; only a rejected token,
which every read would meet, or a store that cannot write ends the round.

A doorbell is dropped when Kite does not know the order (a replayed old postback: Kite answers HTTP 404,
GeneralException, "Couldn't find that `order_id`.", or an empty history), counted in doorbell_unknown_order; when
Kite's answer is about another order, with no event; and after MAX_TRIES failed reads, counted in doorbell_gave_up (the
poll still reports the order). Any other 404 is a failed read, not an unknown order: a wrong base URL ("Route not
found") or a proxy's error page would otherwise drop every doorbell.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Literal

from ..errors import KiteAPIError, KiteMCPError, TokenError
from ._wait import wait_any
from .store import StoreError

if TYPE_CHECKING:
    from .service import EventService

log = logging.getLogger(__name__)

MAX_TRIES = 20  # failed reads of one doorbell before it is given up (the poll is the backstop)

Outcome = Literal["settled", "kept", "stop"]


def _unknown_order(exc: KiteAPIError) -> bool:
    """Kite's answer for an order id it does not know: HTTP 404, GeneralException, "Couldn't find that `order_id`."."""
    return exc.status == 404 and exc.error_type == "GeneralException" and "order_id" in exc.message


class DoorbellWorker:
    """Re-reads the rung orders from Kite: process() once, run() on every ring and every IDLE_S until stopped."""

    # A ring wakes the worker at once; this tick retries the doorbells that a failed read or a missing token left.
    IDLE_S = 60.0

    def __init__(self, service: EventService):
        self.service = service
        self._wake = asyncio.Event()
        self._lock = asyncio.Lock()
        service.on_doorbell(self.wake)

    def wake(self) -> None:
        """Make run() start a round now. Call it on the event loop's thread (asyncio.Event is not thread-safe)."""
        self._wake.set()

    async def run(self, stop: asyncio.Event) -> None:
        """Run process() on every wake and at least every IDLE_S, until `stop` is set. A round that fails (a store that
        cannot be read or written) is logged, and the next round tries again."""
        while not stop.is_set():
            self._wake.clear()
            try:
                await self.process()
            except Exception:
                log.exception("order events: the doorbell worker failed; it will try again")
            await wait_any(self.IDLE_S, self._wake, stop)

    async def process(self) -> int:
        """Re-read every waiting doorbell, oldest first. Returns how many were settled: stored, or dropped (unknown to
        Kite, answered for another order, given up). StoreError ends the round, with the doorbells left as they are."""
        svc = self.service
        settled = 0
        async with self._lock:
            for order_id, tries in svc.store.doorbells():
                record = svc.tokens.current()
                if record is None:
                    break  # no session: the poll after the next login runs the doorbells
                try:
                    outcome = await self._settle(order_id, tries, record.access_token)
                except StoreError:
                    raise
                except Exception:
                    log.exception("order events: could not settle a rung order; it stays for a later round")
                    outcome = self._failed(order_id, tries)
                if outcome == "stop":
                    break
                if outcome == "settled":
                    settled += 1
        return settled

    async def _settle(self, order_id: str, tries: int, access_token: str) -> Outcome:
        """Read one rung order with `access_token` and store Kite's answer, or keep or drop its doorbell."""
        svc = self.service
        try:
            history = await svc.client.request("GET", f"/orders/{order_id}", access_token=access_token)
        except TokenError:
            svc.note_token_rejected(access_token)
            self._failed(order_id, tries)
            return "stop"
        except KiteAPIError as exc:
            if _unknown_order(exc):
                return self._drop(order_id, "doorbell_unknown_order")
            log.warning("order events: could not re-read a rung order: %s", svc.client.scrub(str(exc))[:200])
            return self._failed(order_id, tries)
        except KiteMCPError as exc:
            log.warning("order events: could not re-read a rung order: %s", svc.client.scrub(str(exc))[:200])
            return self._failed(order_id, tries)
        if isinstance(history, list) and not history:
            return self._drop(order_id, "doorbell_unknown_order")
        if not isinstance(history, list) or not isinstance(history[-1], Mapping):
            log.warning("order events: Kite's answer for a rung order is not an order history")
            return self._failed(order_id, tries)
        row = history[-1]
        if row.get("order_id") != order_id:
            log.warning("order events: Kite's answer for a rung order is about another order; dropped")
            svc.store.doorbell_done(order_id)
            return "settled"
        svc.ingest_order(row, source="postback")
        svc.store.doorbell_done(order_id)  # after the order is stored: a crash in between costs one more read
        return "settled"

    def _failed(self, order_id: str, tries: int) -> Outcome:
        """Count a failed read of a doorbell that had failed `tries` times; at MAX_TRIES it is given up."""
        if tries + 1 >= MAX_TRIES:
            log.warning("order events: gave up a rung order after %d failed reads", MAX_TRIES)
            return self._drop(order_id, "doorbell_gave_up")
        self.service.store.doorbell_tried(order_id)
        return "kept"

    def _drop(self, order_id: str, counter: Literal["doorbell_unknown_order", "doorbell_gave_up"]) -> Outcome:
        self.service.store.drop_doorbell(order_id, self.service.clock(), counter)
        return "settled"
