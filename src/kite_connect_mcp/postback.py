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

if TYPE_CHECKING:
    from mcp.server import MCPServer

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


class PostbackRoute:
    """The route hook: `rt.extras["http_routes"]` holds callables that take the MCPServer and add their routes."""

    def __init__(self, rt: Runtime):
        self.rt = rt

    def __call__(self, server: MCPServer) -> None:
        path = self.rt.settings.postback_path
        if path:
            server.custom_route(path, methods=["POST"], name="kite_postback", include_in_schema=False)(self.handle)

    async def handle(self, request: Request) -> Response:
        secret = self.rt.settings.api_secret
        if not secret:
            log.warning("postback refused: KITE_API_SECRET is not set, so the checksum cannot be verified")
            return _bare(403)
        try:
            body = await _read_body(request)
        except ClientDisconnect:
            return _bare(400)
        if body is None:
            log.warning("postback refused: the body is over %d bytes", MAX_BODY_BYTES)
            return _bare(413)
        try:
            payload = json.loads(body)
        except (ValueError, RecursionError):
            log.warning("postback refused: the body is not JSON")
            return _bare(400)
        if not isinstance(payload, dict):
            log.warning("postback refused: the body is not a JSON object")
            return _bare(400)
        if not verify_checksum(payload, secret):
            log.warning("postback refused: the checksum is missing or does not match")
            return _bare(403)
        ticker = self.rt.ticker
        if ticker is None:
            return _bare(503)
        ticker.record_order_update(payload, source="postback")
        log.info("postback accepted")
        return JSONResponse({"status": "ok"}, headers={"Cache-Control": "no-store"})
