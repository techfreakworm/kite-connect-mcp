"""A small asyncio helper shared by the event service's loops."""

from __future__ import annotations

import asyncio


async def wait_any(timeout: float, *events: asyncio.Event) -> None:  # noqa: ASYNC109 (asyncio.timeout would raise)
    """Return when one of `events` is set, or after `timeout` seconds, whichever comes first. With no events, wait the
    timeout (asyncio.wait refuses an empty set)."""
    if not events:
        await asyncio.sleep(max(0.0, timeout))
        return
    waiters = [asyncio.ensure_future(event.wait()) for event in events]
    try:
        await asyncio.wait(waiters, timeout=max(0.0, timeout), return_when=asyncio.FIRST_COMPLETED)
    finally:
        for waiter in waiters:
            waiter.cancel()
        await asyncio.gather(*waiters, return_exceptions=True)
