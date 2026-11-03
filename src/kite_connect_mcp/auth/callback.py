"""The public HTTP route Kite redirects to after the login (the server mounts it; so does `login --listen`).

The route is public by nature (the bearer token protects `/mcp` only), so it is protected by the one-time state (see
auth/loginstate.py), a per-client rate limit, and by saying nothing: every refusal is the same bare 400
"Login not completed." with the same headers, so no check can be probed apart from the others. The reason goes to the
audit log only. The query string holds the request token and the state and is never logged.
"""

from __future__ import annotations

import ipaddress
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse, Response

if TYPE_CHECKING:
    from .login import LoginManager

log = logging.getLogger(__name__)

CALLBACK_RATE_LIMIT = 10  # requests per client per period
CALLBACK_RATE_PERIOD_S = 60.0
CALLBACK_MAX_QUERY = 2048  # bytes of query string; Kite's is about 150
CALLBACK_MAX_CLIENTS = 2048  # distinct clients remembered by the rate limiter

REFUSAL_TEXT = "Login not completed."
SUCCESS_HTML = (
    '<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Kite login</title></head>'
    "<body><p>Kite login done, you can close this tab.</p></body></html>"
)
SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'",
}


class CallbackRateLimiter:
    """At most `limit` requests per client in any `period_s`, in memory. Every request counts, valid or not. With more
    than `max_clients` clients in one period it refuses new ones rather than grow."""

    def __init__(
        self,
        limit: int = CALLBACK_RATE_LIMIT,
        period_s: float = CALLBACK_RATE_PERIOD_S,
        *,
        clock: Callable[[], float] = time.monotonic,
        max_clients: int = CALLBACK_MAX_CLIENTS,
    ):
        self._limit = limit
        self._period_s = period_s
        self._clock = clock
        self._max_clients = max_clients
        self._hits: dict[str, deque[float]] = {}

    def allow(self, client: str) -> bool:
        now = self._clock()
        hits = self._hits.get(client)
        if hits is None:
            if len(self._hits) >= self._max_clients:
                self._forget_idle(now)
                if len(self._hits) >= self._max_clients:
                    return False
            hits = self._hits[client] = deque()
        while hits and now - hits[0] >= self._period_s:
            hits.popleft()
        if len(hits) >= self._limit:
            return False
        hits.append(now)
        return True

    def _forget_idle(self, now: float) -> None:
        for key in [k for k, h in self._hits.items() if not h or now - h[-1] >= self._period_s]:
            del self._hits[key]


def _ip(text: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        address = ipaddress.ip_address(text.strip())
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def client_address(request: Request) -> str:
    """Who is calling, for rate limiting: the peer address, or, when the peer is on loopback (a reverse proxy on the
    same machine), the last X-Forwarded-For entry, which is the address that proxy saw. A header from any other peer
    is not believed. Always a valid IP (an IPv6 one reduced to its /64) or "unknown", so it is safe to log."""
    peer = _ip(request.client.host) if request.client else None
    if peer is not None and peer.is_loopback:
        forwarded = request.headers.getlist("x-forwarded-for")
        via = _ip(forwarded[-1].split(",")[-1]) if forwarded else None
        peer = via or peer
    if peer is None:
        return "unknown"
    if isinstance(peer, ipaddress.IPv6Address):
        return str(ipaddress.IPv6Network((peer, 64), strict=False))
    return str(peer)


def make_callback_handler(
    manager: LoginManager,
    *,
    limiter: CallbackRateLimiter | None = None,
    on_success: Callable[[], Awaitable[None]] | None = None,
) -> Callable[[Request], Awaitable[Response]]:
    """The GET handler for Kite's redirect, public by nature: it is protected by the one-time state, a per-client rate
    limit, and by saying nothing. Every refusal (bad query, unknown or used state, Kite's refusal, wrong account,
    rate limit, any error) is the same bare 400 "Login not completed.", so no check can be probed apart from the
    others. The reason goes to the audit log only. The query string (it holds the request token and the state) is
    never logged. `on_success` (a coroutine function) runs after the success page has been sent."""
    limiter = limiter or CallbackRateLimiter()

    def refusal() -> Response:
        return PlainTextResponse(REFUSAL_TEXT, status_code=400, headers=SECURITY_HEADERS)

    async def callback(request: Request) -> Response:
        client = client_address(request)
        if request.method != "GET":  # Starlette also routes HEAD here; it must not run an exchange
            return refusal()
        if not limiter.allow(client):
            manager.record_refusal("rate_limited", source="callback", client=client)
            return refusal()
        if len(request.url.query) > CALLBACK_MAX_QUERY:
            manager.record_refusal("bad_request", source="callback", client=client)
            return refusal()
        try:
            result = await manager.handle_callback(request.query_params.multi_items(), client=client)
        except Exception as exc:
            log.warning("login callback failed (%s)", type(exc).__name__)
            manager.record_refusal("error", source="callback", client=client, error=type(exc).__name__)
            return refusal()
        if not result.ok:
            return refusal()
        background = BackgroundTask(on_success) if on_success else None
        return HTMLResponse(SUCCESS_HTML, headers=SECURITY_HEADERS, background=background)

    return callback
