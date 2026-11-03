"""Building the MCP server: the runtime, the tools, and the HTTP app (Streamable HTTP behind a bearer token)."""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import hmac
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit

import httpx2
from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from . import __version__
from .auth.tokenstore import TokenFile, TokenProvider
from .client.http import KiteClient
from .client.ratelimit import RateLimiter
from .config import Settings
from .safety.audit import AuditLog
from .safety.confirm import ConfirmationStore
from .safety.pipeline import WritePipeline
from .timeutil import now_ist
from .tools import TOOL_MODULES
from .tools._base import Registrar, Runtime

log = logging.getLogger(__name__)


def build_runtime(
    settings: Settings,
    *,
    transport: httpx2.AsyncBaseTransport | None = None,
    clock: Callable[[], dt.datetime] = now_ist,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    limiter: RateLimiter | None = None,
) -> Runtime:
    token_file = TokenFile(settings.token_file, writable=settings.auth_mode == "managed")
    tokens = TokenProvider(token_file=token_file, env_token=settings.access_token, clock=clock)
    client = KiteClient(settings, tokens, transport=transport, limiter=limiter or RateLimiter(sleep=sleep), sleep=sleep)
    audit = AuditLog(settings.audit_dir, clock=clock)
    confirmations = ConfirmationStore(settings.confirmation_ttl_s, wall=clock)
    pipeline = WritePipeline(settings, client, audit, confirmations, clock=clock)
    return Runtime(settings, tokens, client, audit, confirmations, pipeline, clock=clock)


def instructions(settings: Settings) -> str:
    mode = (
        "READ-ONLY: no tool can change the account."
        if settings.read_only
        else (
            "Every write tool (orders, GTTs, alerts, conversions...) is a DRY RUN by default: it validates the "
            "request, runs the safety checks and shows exactly what would be sent, without sending it. "
            + (
                "Live trading is ENABLED: to send a write, call it with dry_run=true, check the answer, then repeat "
                "the identical call with dry_run=false and the confirmation_token it returned."
                if settings.live_trading
                else "Live trading is DISABLED on this server (KITE_MCP_TRADING_MODE=dry_run): writes can only be "
                "previewed."
            )
        )
    )
    return (
        "Zerodha Kite Connect (Indian markets: NSE, BSE, NFO, BFO, CDS, BCD, MCX). Amounts are in INR, times in IST. "
        "Call session_status first: Kite sessions expire every day at 06:00 IST. "
        "Name instruments as EXCHANGE:TRADINGSYMBOL (NSE:INFY, NSE:NIFTY 50, NFO:NIFTY25NOVFUT); use "
        "search_instruments to find symbols, lot sizes and tick sizes. "
        + mode
        + " Never retry a write whose outcome is unknown without checking the order book first."
    )


def build_server(rt: Runtime) -> tuple[MCPServer, Registrar]:
    settings = rt.settings
    server = MCPServer(
        "kite-connect-mcp",
        title="Kite Connect MCP",
        instructions=instructions(settings),
        version=__version__,
        log_level="WARNING" if settings.log_level == "INFO" else settings.log_level,  # type: ignore[arg-type]
        website_url="https://github.com/techfreakworm/kite-connect-mcp",
    )
    reg = Registrar(server, rt)
    for module in TOOL_MODULES:
        module.register(reg)
    log.info("registered %d tools (%d skipped)", len(reg.registered), len(reg.skipped))
    return server, reg


# --- HTTP ---------------------------------------------------------------------------------------------------------

_UNAUTHORIZED = b'{"error":"unauthorized"}'


class BearerAuth:
    """`Authorization: Bearer <token>` on every request under `/mcp`; the other routes (health, the Kite login
    callback, the postback endpoint) are public by nature and check their own inputs. Constant-time comparison; a
    missing header, another scheme and a wrong token all get the same bare 401."""

    def __init__(self, app: ASGIApp, token: str | None, *, protected_prefix: str = "/mcp"):
        self.app = app
        self.token = token.encode() if token else None
        self.prefix = protected_prefix

    def _allowed(self, scope: Scope) -> bool:
        if self.token is None:
            return True
        presented = b""
        for name, value in scope.get("headers", []):
            if name == b"authorization":
                presented = value
                break
        scheme, _, credential = presented.partition(b" ")
        return scheme.lower() == b"bearer" and hmac.compare_digest(credential.strip(), self.token)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not str(scope.get("path", "")).startswith(self.prefix) or self._allowed(scope):
            await self.app(scope, receive, send)
            return
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(_UNAUTHORIZED)).encode()),
                    (b"www-authenticate", b'Bearer realm="kite-connect-mcp"'),
                    (b"cache-control", b"no-store"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": _UNAUTHORIZED})


def transport_security(settings: Settings) -> TransportSecuritySettings:
    hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*", "127.0.0.1", "localhost"]
    origins = ["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"]
    extra = list(settings.allowed_hosts)
    if settings.public_url:
        netloc = urlsplit(settings.public_url).netloc
        if netloc:
            extra.append(netloc)
            origins.append(f"{urlsplit(settings.public_url).scheme}://{netloc}")
    for host in extra:
        hosts.append(host)
        if ":" not in host:
            hosts.append(f"{host}:*")
    return TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=hosts, allowed_origins=origins)


def build_http_app(server: MCPServer, rt: Runtime) -> ASGIApp:
    settings = rt.settings

    @server.custom_route("/health", methods=["GET"], include_in_schema=False)
    async def health(_: Request) -> Response:
        return JSONResponse({"status": "ok", "version": __version__}, headers={"Cache-Control": "no-store"})

    for hook in rt.extras.get("http_routes", []):  # login callback, postbacks: added by their modules
        hook(server)

    app = server.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        host=settings.host,
        transport_security=transport_security(settings),
    )
    return BearerAuth(app, settings.bearer_token)


@contextlib.asynccontextmanager
async def runtime_lifespan(rt: Runtime) -> AsyncIterator[Runtime]:
    try:
        yield rt
    finally:
        await rt.aclose()


def describe(rt: Runtime, reg: Registrar) -> dict[str, Any]:
    """What this server is set up to do: for the status CLI and the safety tool. No credentials."""
    s = rt.settings
    return {
        "version": __version__,
        "auth_mode": s.auth_mode,
        "trading_mode": s.trading_mode,
        "read_only": s.read_only,
        "tools": len(reg.registered),
        "skipped_tools": reg.skipped,
    }
