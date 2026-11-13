"""The doorbell: a verified postback is never stored as it came.

Kite's checksum covers only order_id and order_timestamp, so a postback's other fields could be altered or replayed.
The route therefore only rings: it keeps the order id on disk (`doorbells`), and this worker re-reads that order from
Kite (GET /orders/{order_id}, the last row of its history) and stores Kite's answer, as source "postback". A forged or
replayed postback can at most cause one read.

Without a valid token nothing is read: the doorbells wait, and the first poll that has a token runs them (a new login
starts a poll at once). An order Kite does not know (a replayed old postback) is dropped and counted.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING

from ..errors import KiteAPIError, KiteMCPError, NotLoggedIn, TokenError
from ._wait import wait_any

if TYPE_CHECKING:
    from .service import EventService

log = logging.getLogger(__name__)


def _unknown_order(exc: KiteAPIError) -> bool:
    return exc.status == 404 or exc.error_type == "InputException"


class DoorbellWorker:
    IDLE_S = 60.0

    def __init__(self, service: EventService):
        self.service = service
        self._wake = asyncio.Event()
        self._lock = asyncio.Lock()
        service.on_doorbell(self.wake)

    def wake(self) -> None:
        self._wake.set()

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            self._wake.clear()
            try:
                await self.process()
            except Exception:
                log.exception("order events: the doorbell worker failed; it will try again")
            await wait_any(self.IDLE_S, self._wake, stop)

    async def process(self) -> int:
        """Re-read every waiting doorbell. Returns how many were settled (stored, or dropped as unknown)."""
        svc = self.service
        settled = 0
        async with self._lock:
            for order_id, _tries in svc.store.doorbells():
                record = svc.tokens.current()
                if record is None:
                    break  # no session: the poll after the next login runs the doorbells
                try:
                    history = await svc.client.get(f"/orders/{order_id}")
                except TokenError:
                    svc.note_token_rejected(record.access_token)
                    svc.store.doorbell_tried(order_id)
                    break
                except NotLoggedIn:
                    break
                except KiteAPIError as exc:
                    if _unknown_order(exc):
                        self._drop(order_id)
                        settled += 1
                    else:
                        svc.store.doorbell_tried(order_id)
                        log.warning(
                            "order events: could not re-read a rung order: %s", svc.client.scrub(str(exc))[:200]
                        )
                    continue
                except KiteMCPError as exc:
                    svc.store.doorbell_tried(order_id)
                    log.warning("order events: could not re-read a rung order: %s", svc.client.scrub(str(exc))[:200])
                    continue
                rows = [row for row in history if isinstance(row, Mapping)] if isinstance(history, list) else []
                if not rows:
                    self._drop(order_id)
                    settled += 1
                    continue
                try:
                    svc.ingest_order(rows[-1], source="postback")
                except ValueError:
                    log.warning("order events: Kite's answer for a rung order had no valid order id")
                svc.store.doorbell_done(order_id)
                settled += 1
        return settled

    def _drop(self, order_id: str) -> None:
        self.service.store.doorbell_done(order_id)
        self.service.store.bump("doorbell_unknown_order", self.service.clock())
