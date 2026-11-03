"""The write pipeline: the one path by which anything that changes the account can reach Kite.

    validate (tool layer) -> policy (allow-lists, caps, kill switch) -> dry run? -> live gate -> confirmation
    -> audit live_intent (fsynced) -> send once with a WriteTicket -> audit live_result / live_error / live_unknown

Dry run is the default of every write tool, and with KITE_MCP_TRADING_MODE=dry_run (the default) it is the only
outcome: the answer shows the exact request that would be sent and every check, and nothing leaves the process.

A live write needs all of:
  * KITE_MCP_TRADING_MODE=live and not KITE_MCP_READ_ONLY
  * no kill-switch file, every policy check passing with today's usage (read back from the audit log)
  * a valid confirmation token from a dry run of the identical request, unless KITE_MCP_LIVE_CONFIRMATION=allowlist
    and every leg names an instrument on the explicit KITE_MCP_ALLOWED_INSTRUMENTS list

Live writes are serialised (one at a time) so that two concurrent calls can never both slip under a cap.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from ..client.http import _TICKET_ISSUER, KiteClient, Params, WriteTicket, _pairs, payload_digest
from ..config import Settings
from ..errors import KiteAPIError, OutcomeUnknown, SafetyRefusal
from ..timeutil import now_ist
from .audit import AuditLog
from .confirm import ConfirmationStore
from .policy import OrderLeg, added_value, evaluate, preauthorised

log = logging.getLogger(__name__)


@dataclass
class WriteRequest:
    tool: str
    method: str
    path: str
    params: Params = None  # query string
    data: Params = None  # form body
    json_body: Any = None  # JSON body
    summary: str = ""  # one line a person can read: "BUY 10 NSE:INFY LIMIT @ 1500 (CNC, regular)"
    legs: list[OrderLeg] = field(default_factory=list)
    counts_as_order: bool = False  # counts toward the daily order count and value caps
    order_count: int = 1
    daily_value: Decimal | None = None  # what this adds to today's value (default: the sum of the legs' values)
    risk_reducing: bool = False  # cancels and deletes: caps do not apply, confirmation still does
    order_id: str | None = None  # the order a modify/cancel acts on (audit, modification count)
    preview: dict[str, Any] = field(default_factory=dict)  # extra facts for the dry-run answer (margins, ...)
    warnings: list[str] = field(default_factory=list)

    @property
    def digest(self) -> str:
        return payload_digest(self.method, self.path, self.params, self.data, self.json_body)

    def wire(self) -> dict[str, Any]:
        """The request as it would go out (without headers, which hold the credentials)."""
        out: dict[str, Any] = {"method": self.method, "path": self.path}
        if self.params:
            out["query"] = _pairs(self.params)
        if self.data:
            pairs = _pairs(self.data) or []
            keys = [k for k, _ in pairs]
            out["form"] = dict(pairs) if len(set(keys)) == len(keys) else pairs
        if self.json_body is not None:
            out["json"] = self.json_body
        return out


class WritePipeline:
    def __init__(
        self,
        settings: Settings,
        client: KiteClient,
        audit: AuditLog,
        confirmations: ConfirmationStore,
        *,
        clock: Callable[[], dt.datetime] = now_ist,
    ):
        self.settings = settings
        self.client = client
        self.audit = audit
        self.confirmations = confirmations
        self._clock = clock
        self._live_lock = asyncio.Lock()

    def kill_switch_on(self) -> bool:
        return self.settings.kill_switch_file.exists()

    def _verdict(self, req: WriteRequest):
        verdict = evaluate(
            self.settings,
            req.legs,
            usage=self.audit.usage(),
            counts_as_order=req.counts_as_order,
            order_count=req.order_count,
            daily_value=req.daily_value,
            risk_reducing=req.risk_reducing,
            kill_switch_on=self.kill_switch_on(),
        )
        verdict.warnings.extend(req.warnings)
        if req.tool == "modify_order" and req.order_id:
            done = self.audit.modifications(req.order_id)
            if done >= 25:
                verdict.violations.append(
                    f"order {req.order_id} was already modified {done} times today; Kite allows 25 per order "
                    "(cancel it and place a new one)"
                )
        return verdict

    async def run(self, req: WriteRequest, *, dry_run: bool = True, confirmation_token: str | None = None) -> dict:
        call_id = uuid.uuid4().hex[:12]
        base: dict[str, Any] = {"call_id": call_id, "tool": req.tool, "request": req.wire(), "summary": req.summary}
        if self.settings.read_only:  # the tools are not even registered in read-only mode; this is the backstop
            self.audit.write("refused", **base, reasons=["read-only mode (KITE_MCP_READ_ONLY=true)"])
            raise SafetyRefusal("read-only mode: writes are disabled", ["read-only mode (KITE_MCP_READ_ONLY=true)"])

        if dry_run:
            return self._dry_run(req, base)

        async with self._live_lock:
            return await self._live(req, base, confirmation_token)

    # --- dry run ----------------------------------------------------------------------------------------------------

    def _dry_run(self, req: WriteRequest, base: dict[str, Any]) -> dict[str, Any]:
        verdict = self._verdict(req)
        usage = self.audit.usage()
        answer: dict[str, Any] = {
            "dry_run": True,
            "sent": False,
            "summary": req.summary,
            "would_send": req.wire(),
            "checks": {"ok": verdict.ok, "violations": verdict.violations, "warnings": verdict.warnings},
            "legs": [_leg_view(leg) for leg in req.legs],
            "today": usage.as_dict(),
            "limits": self.limits(),
            **req.preview,
        }
        issued: tuple[str, dt.datetime] | None = None
        if not self.settings.live_trading:
            answer["live_trading"] = "disabled: KITE_MCP_TRADING_MODE=dry_run, so this can only ever be a dry run"
        elif not verdict.ok:
            answer["live_trading"] = "enabled, but this request would be refused: fix the violations first"
        elif preauthorised(self.settings, req.legs):
            answer["live_trading"] = "enabled; every leg is allow-listed, so no confirmation token is needed"
            answer["next_step"] = "repeat the identical call with dry_run=false to send it"
        else:
            issued = self.confirmations.issue(req.tool, req.digest)
            answer["live_trading"] = "enabled"
            answer["confirmation_token"] = issued[0]
            answer["confirmation_expires_at"] = issued[1].isoformat(timespec="seconds")
            answer["next_step"] = (
                "to send this for real, repeat the identical call with dry_run=false and this confirmation_token "
                f"(single use, valid {self.settings.confirmation_ttl_s} s)"
            )
        self.audit.write(
            "confirmation_issued" if issued else "dry_run",
            **base,
            ok=verdict.ok,
            violations=verdict.violations,
            warnings=verdict.warnings,
            legs=[_leg_view(leg) for leg in req.legs],
        )
        return answer

    # --- live -------------------------------------------------------------------------------------------------------

    async def _live(self, req: WriteRequest, base: dict[str, Any], token: str | None) -> dict[str, Any]:
        reasons: list[str] = []
        if self.settings.trading_mode != "live":
            reasons.append("live trading is disabled (KITE_MCP_TRADING_MODE=dry_run); only dry runs are possible")
        verdict = self._verdict(req)
        reasons.extend(verdict.violations)
        if not reasons and not preauthorised(self.settings, req.legs):
            if not token:
                reasons.append(
                    "a confirmation_token is required: run the same call with dry_run=true, then repeat it with "
                    "dry_run=false and the token from that answer"
                )
            else:
                try:
                    self.confirmations.consume(token, req.tool, req.digest)
                except SafetyRefusal as exc:
                    reasons.extend(exc.reasons)
        if reasons:
            self.audit.write("refused", **base, reasons=reasons)
            raise SafetyRefusal("refused by the safety policy", reasons)

        value = (
            Decimal(0)
            if req.risk_reducing
            else added_value(req.legs, counts_as_order=req.counts_as_order, daily_value=req.daily_value)
        )
        orders = req.order_count if req.counts_as_order and not req.risk_reducing else 0
        counted = {"counts": bool(orders or value), "order_count": orders, "value_inr": value}
        self.audit.write("live_intent", durable=True, **base, value_inr=value, order_id=req.order_id)
        ticket = WriteTicket(req.method, req.path, req.digest, issuer=_TICKET_ISSUER)
        try:
            response = await self.client.write(
                req.method, req.path, ticket, params=req.params, data=req.data, json_body=req.json_body
            )
        except OutcomeUnknown as exc:  # it may have happened: count it, so the caps err on the safe side
            self.audit.write("live_unknown", durable=True, **base, **counted, error=str(exc))
            raise
        except KiteAPIError as exc:
            self.audit.write(
                "live_error", durable=True, **base, error_type=exc.error_type, status=exc.status, error=exc.message
            )
            raise
        order_id = req.order_id
        if isinstance(response, dict):
            found = response.get("order_id") or response.get("trigger_id") or response.get("sip_id")
            order_id = str(found or order_id or "") or None
        self.audit.write(
            "live_result",
            durable=True,
            **base,
            **counted,
            order_id=order_id if req.tool != "modify_order" else req.order_id,
            response=response,
        )
        log.info("live write sent: %s (%s)", req.tool, req.summary)
        return {
            "dry_run": False,
            "sent": True,
            "summary": req.summary,
            "call_id": base["call_id"],
            "response": response,
        }

    def limits(self) -> dict[str, Any]:
        s = self.settings
        return {
            "trading_mode": s.trading_mode,
            "read_only": s.read_only,
            "live_confirmation": s.live_confirmation,
            "max_order_value_inr": str(s.max_order_value),
            "max_daily_value_inr": str(s.max_daily_value),
            "max_orders_per_day": s.max_orders_per_day,
            "max_quantity": s.max_quantity,
            "kill_switch_on": self.kill_switch_on(),
        }


def _leg_view(leg: OrderLeg) -> dict[str, Any]:
    view = {
        "instrument": leg.instrument,
        "transaction_type": leg.transaction_type,
        "quantity": leg.quantity,
        "order_type": leg.order_type,
        "product": leg.product,
        "variety": leg.variety,
        "price_used": str(leg.price) if leg.price is not None else None,
        "value_inr": str(leg.value) if leg.value is not None else None,
    }
    if leg.value_note:
        view["value_note"] = leg.value_note
    return {k: v for k, v in view.items() if v is not None}
