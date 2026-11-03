"""Tools: the safety settings, today's usage, the audit log, and the kill switch (which a tool can only switch ON)."""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Any

from pydantic import Field

from ..auth.tokenstore import write_private_file
from ..errors import ToolInputError
from ._base import Registrar


def register(reg: Registrar) -> None:
    rt = reg.rt

    @reg.tool("get_safety_status", title="Safety settings and today's usage", toolset="safety", kind="read")
    async def get_safety_status() -> Any:
        """How this server guards writes: trading mode (dry_run or live), read-only, how live writes are confirmed,
        the per-order / daily value caps and order count, the allow-lists, whether the kill switch is on, and what has
        been sent live today (counted from the audit log). Call this before planning any order."""
        s = rt.settings
        return {
            **rt.pipeline.limits(),
            "today": rt.audit.usage().as_dict(),
            "allow_lists": {
                "instruments": sorted(s.allowed_instruments) if s.allowed_instruments else "any",
                "exchanges": sorted(s.allowed_exchanges) if s.allowed_exchanges else "any",
                "products": sorted(s.allowed_products) if s.allowed_products else "any",
                "varieties": sorted(s.allowed_varieties) if s.allowed_varieties else "any",
                "order_types": sorted(s.allowed_order_types) if s.allowed_order_types else "any",
                "transaction_types": sorted(s.allowed_transaction_types) if s.allowed_transaction_types else "any",
            },
            "mf_writes": "enabled" if s.allow_mf_writes else "disabled (KITE_MCP_ALLOW_MF_WRITES=false)",
            "confirmation_ttl_s": s.confirmation_ttl_s,
            "how_live_writes_work": (
                "every write tool defaults to dry_run=true; a live write needs KITE_MCP_TRADING_MODE=live, passing "
                "checks, and the confirmation_token from a dry run of the identical call"
                + (" (or an allow-listed instrument)" if s.live_confirmation == "allowlist" else "")
            ),
        }

    @reg.tool("get_audit_log", title="Audit log of write attempts", toolset="safety", kind="read")
    async def get_audit_log(
        date: Annotated[str | None, Field(description="IST date YYYY-MM-DD; omit for today")] = None,
        limit: Annotated[int, Field(ge=1, le=500, description="newest N events")] = 50,
        tool: Annotated[str | None, Field(description="only events of this tool, e.g. place_order")] = None,
    ) -> Any:
        """The audit log: every dry run, refusal, confirmation, live send and its result, oldest first (newest
        `limit`). Each event has a call_id tying a live_intent to its live_result / live_error / live_unknown."""
        try:
            day = dt.date.fromisoformat(date) if date else None
        except ValueError:
            raise ToolInputError("date must be YYYY-MM-DD") from None
        events = rt.audit.read(day, limit=limit, tool=tool)
        return {"date": (day or rt.clock().date()).isoformat(), "count": len(events), "events": events}

    @reg.tool("engage_kill_switch", title="Engage the kill switch", toolset="safety", kind="local")
    async def engage_kill_switch(
        reason: Annotated[str, Field(min_length=3, max_length=300, description="why writes are being stopped")],
    ) -> Any:
        """Stop every write at once: creates the kill-switch file, after which all write tools are refused (dry runs
        still work and say so). Only a person can switch it off again, by deleting the file on the server."""
        path = rt.settings.kill_switch_file
        stamp = rt.clock().isoformat(timespec="seconds")
        write_private_file(path, f"engaged {stamp} via MCP: {reason.strip()}\n")
        rt.audit.write("kill_switch_engaged", tool="engage_kill_switch", reason=reason.strip())
        return {"kill_switch_on": True, "file": str(path), "note": "delete this file on the server to switch it off"}
