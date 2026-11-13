"""The `events` toolset: read the order-event store.

In the serving process (KITE_MCP_EVENTS=true) the tools read the service's own store. Any other MCP process (a stdio
client, say) can read the same store read-only by naming it in KITE_MCP_EVENTS_DB; that store is opened once, when the
tools are registered, and closed with the runtime. Without a store the tools are hidden, and say why. They only read a
local file: they never call Kite and never change anything."""

from __future__ import annotations

from typing import Annotated, Any

from mcp_types import ToolAnnotations
from pydantic import Field

from ..events.service import describe_status
from ..events.store import EventStore, StoreError
from ._base import Registrar, Runtime, require

LOCAL_READ = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
TOOLS = ("get_order_events", "get_event_status")
MAX_EVENTS = 200


def _store_of(rt: Runtime) -> tuple[EventStore | None, str]:
    """(the store the tools read, "") or (None, why there is none). A store opened here is closed by rt.aclose()."""
    settings = rt.settings
    if rt.events is not None:
        return rt.events.store, ""
    if settings.events_db is None:
        return None, "no event store: set KITE_MCP_EVENTS=true, or KITE_MCP_EVENTS_DB to read another server's store"
    if not settings.events_db.exists():
        return None, f"the event store {settings.events_db} does not exist yet (has the event service run?)"
    try:
        store = EventStore(settings.events_db, readonly=True)
    except StoreError as exc:
        return None, str(exc)
    rt.closers.append(store.close)
    return store, ""


def register(reg: Registrar) -> None:
    rt = reg.rt
    off = {name: why for name in TOOLS if (why := reg.enabled(name, "events", "read"))}
    if len(off) == len(TOOLS):  # turned off by the settings: open no store for them
        reg.skipped.update(off)
        return
    store, why_not = _store_of(rt)
    if store is None:
        for name in TOOLS:
            reg.skipped[name] = off.get(name, why_not)
        return

    @reg.tool("get_order_events", title="Order events", toolset="events", kind="read", annotations=LOCAL_READ)
    async def get_order_events(
        order_id: Annotated[str | None, Field(description="Only this order's events (optional)")] = None,
        after_seq: Annotated[
            int, Field(description="Only events after this seq, oldest first (for paging); 0 = the newest")
        ] = 0,
        limit: Annotated[int, Field(description=f"How many events, 1-{MAX_EVENTS}")] = 20,
    ) -> Any:
        """Recent order events from the event store: fills, partial fills, cancellations, rejections, new and modified
        orders (from postbacks and the order-book poll), session events such as a rejected token, and delivery alerts.
        Each event has a one-line `text`. Oldest first."""
        require(1 <= limit <= MAX_EVENTS, f"limit must be between 1 and {MAX_EVENTS}")
        require(after_seq >= 0, "after_seq must be 0 or more")
        return {"events": store.events(order_id=order_id, after_seq=after_seq, limit=limit)}

    @reg.tool("get_event_status", title="Order event status", toolset="events", kind="read", annotations=LOCAL_READ)
    async def get_event_status() -> Any:
        """The health of the order events: the last postback and how many were refused, the last poll and its error,
        doorbells waiting, each consumer's pending, delayed and failed deliveries, and paused consumers."""
        names = [c.name for c in rt.events.config.consumers] if rt.events is not None else None
        return describe_status(store, rt.settings, rt.clock(), names)
