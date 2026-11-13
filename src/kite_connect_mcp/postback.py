"""Kite postbacks: an optional HTTP endpoint Kite calls when an order changes (https://kite.trade/docs/connect/v3/postbacks/).

Kite POSTs the order as a raw JSON body to the URL registered for the app. The body carries a `checksum`, the SHA-256 of
order_id + order_timestamp + the app's API secret; only Kite (and we) can compute it, so a matching checksum is what
tells a real postback from a forged one. Accepted updates go to the same bounded buffer as the WebSocket's order
updates (source "postback"), where `ticker_order_updates` reads them.

The route is enabled by KITE_MCP_POSTBACK_PATH, added through the `rt.extras["http_routes"]` hook that
`server.build_http_app` calls, and is public like /health and the login callback: the bearer token protects /mcp only,
and Kite cannot send one. So it must defend itself:

* every failure is a bare response with no body, and checks that fail on the checksum all give the same 403, so a prober
  learns nothing about which part was wrong;
* the body is capped (MAX_BODY_BYTES) while it is read, not after;
* nothing from the payload (no ids, no checksum, no user) is logged, and the buffer keeps only the documented order
  fields (see manager.clean_order);
* with no API secret configured the endpoint refuses everything instead of accepting unverified data.

What the checksum does not cover: it binds the order_id and order_timestamp only, so a captured postback could be
replayed or have its other fields altered and still verify. Treat what arrives as a notification and confirm anything
that matters with the order book.

With KITE_MCP_EVENTS=true the route is registered whenever KITE_MCP_POSTBACK_PATH is set, and a verified postback only
rings the event service's doorbell (or records a selftest): the order is then re-read from Kite and nothing from the
payload is stored (see events/doorbell.py). Every refusal is counted, under a fixed reason (service.REFUSALS).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from starlette.requests import ClientDisconnect, Request
from starlette.responses import JSONResponse, Response

from .events.model import ORDER_ID_RE, SELFTEST_PREFIX
from .events.store import StoreError

if TYPE_CHECKING:
    from mcp.server import MCPServer

    from .events.service import Refusal
    from .tools._base import Runtime

log = logging.getLogger(__name__)

MAX_BODY_BYTES = 64 * 1024


def compute_checksum(order_id: str, order_timestamp: str, api_secret: str) -> str:
    """The checksum Kite attaches to a postback: SHA-256 hex of order_id + order_timestamp + api_secret."""
    return hashlib.sha256((order_id + order_timestamp + api_secret).encode("utf-8", "replace")).hexdigest()


def verify_checksum(payload: Mapping[str, Any], api_secret: str) -> bool:
    """True if the payload's checksum matches. A missing or non-string field is a mismatch. Constant-time compare."""
    order_id = payload.get("order_id")
    order_timestamp = payload.get("order_timestamp")
    presented = payload.get("checksum")
    if not (isinstance(order_id, str) and isinstance(order_timestamp, str) and isinstance(presented, str)):
        return False
    expected = compute_checksum(order_id, order_timestamp, api_secret)
    return hmac.compare_digest(expected.encode(), presented.strip().lower().encode("utf-8", "replace"))


def _bare(status: int) -> Response:
    return Response(status_code=status, headers={"Cache-Control": "no-store"})


def _ok() -> Response:
    return JSONResponse({"status": "ok"}, headers={"Cache-Control": "no-store"})


async def _read_body(request: Request) -> bytes | None:
    """The request body, or None if it is over MAX_BODY_BYTES. Counts the bytes as they arrive, so an oversized or
    endless body is cut off instead of being buffered first."""
    declared = request.headers.get("content-length", "")
    if declared.isdecimal() and int(declared) > MAX_BODY_BYTES:
        return None
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_BODY_BYTES:
            return None
    return bytes(body)


def ensure_postback_route(rt: Runtime) -> None:
    """Add the route hook once (`server.build_http_app` calls the hooks)."""
    hooks = rt.extras.setdefault("http_routes", [])
    if not any(isinstance(hook, PostbackRoute) for hook in hooks):
        hooks.append(PostbackRoute(rt))


class PostbackRoute:
    """The route hook: `rt.extras["http_routes"]` holds callables that take the MCPServer and add their routes."""

    def __init__(self, rt: Runtime):
        self.rt = rt

    def __call__(self, server: MCPServer) -> None:
        path = self.rt.settings.postback_path
        if path:
            server.custom_route(path, methods=["POST"], name="kite_postback", include_in_schema=False)(self.handle)

    async def handle(self, request: Request) -> Response:
        events = self.rt.events
        secret = self.rt.settings.api_secret

        def refuse(status: int, reason: Refusal, detail: str) -> Response:
            """`reason` is one of service.REFUSALS (it is counted and shown in the status); `detail` is for the log."""
            log.warning("postback refused: %s", detail)
            if events is not None:
                events.refused(reason)
            return _bare(status)

        if not secret:
            return refuse(403, "no API secret", "KITE_API_SECRET is not set, so the checksum cannot be verified")
        try:
            body = await _read_body(request)
        except ClientDisconnect:
            return _bare(400)
        if body is None:
            return refuse(413, "body over the cap", f"the body is over {MAX_BODY_BYTES} bytes")
        try:
            payload = json.loads(body)
        except (ValueError, RecursionError):
            return refuse(400, "body not JSON", "the body is not JSON")
        if not isinstance(payload, dict):
            return refuse(400, "body not a JSON object", "the body is not a JSON object")
        if not verify_checksum(payload, secret):
            return refuse(403, "bad checksum", "the checksum is missing or does not match")
        if events is None:
            ticker = self.rt.ticker
            if ticker is None:
                return _bare(503)
            ticker.record_order_update(payload, source="postback")
            log.info("postback accepted")
            return _ok()
        order_id = payload["order_id"]  # a str: verify_checksum checked it
        if not ORDER_ID_RE.fullmatch(order_id):
            return refuse(400, "bad order id", "the order id has an unexpected form")
        try:
            if order_id.startswith(SELFTEST_PREFIX):
                events.record_selftest(order_id)
                log.info("postback selftest received")
                return _ok()
            if not events.user_allowed(payload.get("user_id")):
                return refuse(
                    403, "another account", "the postback is for another account than KITE_MCP_ALLOWED_USER_ID"
                )
            events.ring(order_id)
        except ValueError:  # an id the service does not take: "selftest-" with nothing after it
            return refuse(400, "bad order id", "the order id has an unexpected form")
        except StoreError:
            log.error("postback: the event store cannot write; answering 503 (the poll will catch up)")
            return _bare(503)
        if self.rt.extras.get("postback_to_ticker") and self.rt.ticker is not None:
            self.rt.ticker.record_order_update(payload, source="postback")
        log.info("postback accepted")
        return _ok()
