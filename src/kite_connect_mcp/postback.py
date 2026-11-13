"""Kite postbacks: an optional HTTP endpoint Kite calls when an order changes (https://kite.trade/docs/connect/v3/postbacks/).

Kite POSTs the order as a raw JSON body to the URL registered for the app. The body carries a `checksum`, the SHA-256 of
order_id + order_timestamp + the app's API secret; only Kite (and we) can compute it, so a matching checksum is what
tells a real postback from a forged one. Accepted updates go to the same bounded buffer as the WebSocket's order
updates (source "postback"), where `ticker_order_updates` reads them.

The route is enabled by KITE_MCP_POSTBACK_PATH, added once (ensure_postback_route) through the
`rt.extras["http_routes"]` hook that `server.build_http_app` calls, and is public like /health and the login callback:
the bearer token protects /mcp only, and Kite cannot send one. So it must defend itself:

* every failure is a bare response with no body, and checks that fail on the checksum all give the same 403, so a prober
  learns nothing about which part was wrong;
* the body is capped (MAX_BODY_BYTES) while it is read, not after;
* nothing from the payload (no ids, no checksum, no user) is logged, and the buffer keeps only the documented order
  fields (see manager.clean_order);
* a refusal is logged at most once a minute for each reason (the next line for it says how many were not), so junk
  requests cannot flood the log; so is the 503 answered while the event store cannot write;
* with no API secret configured the endpoint refuses everything instead of accepting unverified data.

What the checksum does not cover: it binds the order_id and order_timestamp only, so a captured postback could be
replayed or have its other fields altered and still verify. Treat what arrives as a notification and confirm anything
that matters with the order book.

With KITE_MCP_EVENTS=true the route is registered whenever KITE_MCP_POSTBACK_PATH is set, the ticker on or off, and a
verified postback rings the event service's doorbell (or records a selftest): the order is then re-read from Kite and
nothing from the payload is stored (see events/doorbell.py). With KITE_MCP_ALLOWED_USER_ID set, a postback for another
account is refused. When the ticker's order updates are on (`ticker_order_updates`), an accepted postback also goes to
their buffer, as without the events. Every refusal is counted under a fixed reason (service.REFUSALS), and written at
most once a second (EventService.refused). An exact repeat of a postback accepted in the last ten seconds (Kite sending
it twice, or a replay) is answered 200 without ringing again (RepeatGuard). Only the whole body makes a repeat: the
checksum covers only order_id and order_timestamp, so an order's later updates can carry the same one.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import logging
from collections import OrderedDict
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from starlette.requests import ClientDisconnect, Request
from starlette.responses import JSONResponse, Response

from .events.model import ORDER_ID_RE, SELFTEST_PREFIX, is_selftest_id
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


def ensure_postback_route(rt: Runtime, *, to_ticker: bool = False) -> PostbackRoute:
    """Add the route hook once (`server.build_http_app` calls the hooks) and return it. `to_ticker` (the ticker tools
    ask for it when `ticker_order_updates` is on) makes it copy accepted postbacks to the ticker's buffer when the event
    service is on too; without the service the buffer is where they go anyway."""
    hooks = rt.extras.setdefault("http_routes", [])
    route = next((hook for hook in hooks if isinstance(hook, PostbackRoute)), None)
    if route is None:
        route = PostbackRoute(rt)
        hooks.append(route)
    route.to_ticker = route.to_ticker or to_ticker
    return route


class RepeatGuard:
    """The postbacks accepted in the last WINDOW_S seconds, by the SHA-256 of their body (nothing else is kept), at most
    MAX_ENTRIES of them. A repeat is answered without ringing again, so a replayed postback costs no disk write. A new
    update of the same order has another body, and is rung at once."""

    WINDOW_S = 10.0
    MAX_ENTRIES = 1000

    def __init__(self) -> None:
        self._seen: OrderedDict[bytes, dt.datetime] = OrderedDict()

    def __len__(self) -> int:
        return len(self._seen)

    def _fresh(self, at: dt.datetime, now: dt.datetime) -> bool:
        return dt.timedelta(0) <= now - at < dt.timedelta(seconds=self.WINDOW_S)  # a clock that went back holds nothing

    def is_repeat(self, key: bytes, now: dt.datetime) -> bool:
        at = self._seen.get(key)
        return at is not None and self._fresh(at, now)

    def remember(self, key: bytes, now: dt.datetime) -> None:
        self._seen.pop(key, None)
        self._seen[key] = now
        while self._seen and (
            len(self._seen) > self.MAX_ENTRIES or not self._fresh(next(iter(self._seen.values())), now)
        ):
            self._seen.popitem(last=False)  # the oldest first


class PostbackRoute:
    """The route hook: `rt.extras["http_routes"]` holds callables that take the MCPServer and add their routes."""

    REFUSAL_LOG_S = 60.0  # the refusals for one reason, and the 503s, are each logged at most once in this many seconds

    def __init__(self, rt: Runtime):
        self.rt = rt
        self.to_ticker = False  # see ensure_postback_route
        self.recent = RepeatGuard()
        self._log_times: dict[str, tuple[dt.datetime, int]] = {}  # what -> (last logged at, lines not logged since)

    def __call__(self, server: MCPServer) -> None:
        path = self.rt.settings.postback_path
        if path:
            server.custom_route(path, methods=["POST"], name="kite_postback", include_in_schema=False)(self.handle)

    async def handle(self, request: Request) -> Response:
        events = self.rt.events
        secret = self.rt.settings.api_secret

        def refuse(status: int, reason: Refusal, detail: str) -> Response:
            """`reason` is one of service.REFUSALS (it is counted and shown in the status); `detail` is for the log."""
            self._log_limited(reason, logging.WARNING, f"postback refused: {detail}")
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
        selftest = is_selftest_id(order_id)
        # a selftest id, or an order id that does not take the selftest prefix ("selftest-" alone is neither)
        if not selftest and (order_id.startswith(SELFTEST_PREFIX) or not ORDER_ID_RE.fullmatch(order_id)):
            return refuse(400, "bad order id", "the order id has an unexpected form")
        if not selftest and not events.user_allowed(payload.get("user_id")):
            return refuse(403, "another account", "the postback is for another account than KITE_MCP_ALLOWED_USER_ID")
        now = self.rt.clock()
        key = hashlib.sha256(body).digest()
        if self.recent.is_repeat(key, now):
            return _ok()  # accepted moments ago (Kite sent it twice, or a replay): rung already, nothing to write
        if not selftest and self.to_ticker and self.rt.ticker is not None:
            self.rt.ticker.record_order_update(payload, source="postback")  # first: the buffer does not need the store
        try:
            if selftest:
                events.record_selftest(order_id)
            else:
                events.ring(order_id)
        except StoreError as exc:  # its text is SQLite's message (a write names no path), nothing from the payload
            self._log_limited("503", logging.ERROR, f"postback: answering 503 (the poll will catch up): {exc}")
            return _bare(503)
        self.recent.remember(key, now)
        log.info("postback selftest received" if selftest else "postback accepted")
        return _ok()

    def _log_limited(self, what: str, level: int, line: str) -> None:
        """Log `line`, at most once every REFUSAL_LOG_S for each `what` (a refusal's reason, or "503"): the lines in
        between are only counted, and the next one logged says how many there were. `line` never holds text from the
        request."""
        now = self.rt.clock()
        since, unlogged = self._log_times.get(what, (None, 0))
        if since is not None and dt.timedelta(0) <= now - since < dt.timedelta(seconds=self.REFUSAL_LOG_S):
            self._log_times[what] = (since, unlogged + 1)
            return
        if unlogged:
            log.log(level, "%s (%d more like it since the last such line)", line, unlogged)
        else:
            log.log(level, "%s", line)
        self._log_times[what] = (now, 0)
