"""Shared plumbing for tool modules: the runtime they act on, registration, results and errors.

A tool module is a file in this package with a `register(reg: Registrar) -> None` function. Inside it, tools are
plain async functions decorated with `@reg.tool(...)`. The decorator decides whether the tool is exposed at all
(toolsets, KITE_MCP_EXCLUDED_TOOLS, read-only mode hides every write tool), attaches MCP annotations, and turns the
function's return value into compact JSON and its exceptions into a short, credential-free error for the caller.

Conventions every tool follows:
  * Arguments are typed with `Annotated[..., Field(description=...)]`: the description is what the model sees.
  * Instruments are named "EXCHANGE:TRADINGSYMBOL" (e.g. "NSE:INFY", "NSE:NIFTY 50", "NFO:NIFTY25NOVFUT").
  * Write tools take `dry_run: bool = True` and `confirmation_token: str | None = None` and hand a WriteRequest to
    `reg.rt.pipeline.run(...)`. They never call the HTTP client's write path themselves.
  * Lists that can be long take `limit` / `offset` and return `paginate(...)`.
"""

from __future__ import annotations

import datetime as dt
import functools
import inspect
import json
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations

from ..auth.tokenstore import TokenProvider
from ..client.http import KiteClient
from ..config import Settings
from ..errors import (
    AuthorisationRequired,
    ConfigError,
    ForbiddenCall,
    KiteAPIError,
    KiteMCPError,
    NotLoggedIn,
    OutcomeUnknown,
    PermissionError_,
    SafetyRefusal,
    TokenError,
    ToolInputError,
)
from ..safety.audit import AuditLog
from ..safety.confirm import ConfirmationStore
from ..safety.pipeline import WritePipeline
from ..timeutil import now_ist

if TYPE_CHECKING:  # the heavier pieces are created lazily by their own modules
    from ..auth.login import LoginManager
    from ..instruments import InstrumentStore
    from ..ticker.manager import TickerManager

log = logging.getLogger(__name__)

Kind = Literal["read", "calc", "write", "auth", "local"]

READ = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=True)
AUTH = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True)
LOCAL = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
ANNOTATIONS: dict[str, ToolAnnotations] = {"read": READ, "calc": READ, "write": WRITE, "auth": AUTH, "local": LOCAL}

MAX_LIMIT = 500


@dataclass
class Runtime:
    """Everything a tool can use. One per server process."""

    settings: Settings
    tokens: TokenProvider
    client: KiteClient
    audit: AuditLog
    confirmations: ConfirmationStore
    pipeline: WritePipeline
    clock: Callable[[], dt.datetime] = now_ist
    instruments: InstrumentStore | None = None
    ticker: TickerManager | None = None
    login: LoginManager | None = None
    extras: dict[str, Any] = field(default_factory=dict)

    async def aclose(self) -> None:
        if self.ticker is not None:
            await self.ticker.stop()
        await self.client.aclose()


@dataclass(frozen=True)
class ToolInfo:
    name: str
    title: str
    toolset: str
    kind: Kind
    description: str


class Registrar:
    def __init__(self, server: MCPServer, rt: Runtime):
        self.server = server
        self.rt = rt
        self.registered: list[ToolInfo] = []
        self.skipped: dict[str, str] = {}

    def enabled(self, name: str, toolset: str, kind: Kind) -> str | None:
        """None if the tool should be exposed, else why not."""
        s = self.rt.settings
        if toolset not in s.toolsets:
            return f"toolset {toolset} is not in KITE_MCP_TOOLSETS"
        if name in s.excluded_tools:
            return "listed in KITE_MCP_EXCLUDED_TOOLS"
        if kind == "write" and s.read_only:
            return "read-only mode"
        return None

    def tool(
        self,
        name: str,
        *,
        title: str,
        toolset: str,
        kind: Kind,
        description: str | None = None,
        annotations: ToolAnnotations | None = None,
    ) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]:
        def register(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
            text = inspect.cleandoc(description or fn.__doc__ or "")
            why_not = self.enabled(name, toolset, kind)
            if why_not:
                self.skipped[name] = why_not
                return fn

            @functools.wraps(fn)
            async def wrapper(*args: Any, **kwargs: Any) -> str:
                try:
                    return to_json(await fn(*args, **kwargs))
                except Exception as exc:  # every failure becomes one short, credential-free ToolError
                    raise ToolError(self.describe_error(name, exc)) from None

            self.server.add_tool(
                wrapper,
                name=name,
                title=title,
                description=text,
                annotations=annotations or ANNOTATIONS[kind],
                structured_output=False,
            )
            self.registered.append(ToolInfo(name, title, toolset, kind, text))
            return fn

        return register

    def describe_error(self, name: str, exc: Exception) -> str:
        scrub = self.rt.client.scrub
        if isinstance(exc, SafetyRefusal):
            reasons = "; ".join(exc.reasons) or str(exc)
            return scrub(f"{name} refused by the safety policy: {reasons}")
        if isinstance(exc, ToolInputError):
            return scrub(f"{name}: {exc}")
        if isinstance(exc, NotLoggedIn):
            return scrub(f"{name}: not logged in to Kite: {exc}. {self.login_hint()}")
        if isinstance(exc, TokenError):
            return scrub(f"{name}: Kite rejected the session ({exc.message}). {self.login_hint()}")
        if isinstance(exc, AuthorisationRequired):
            return scrub(
                f"{name}: {exc.message} (HTTP 428). The sell needs a CDSL authorisation first: call "
                "authorise_holdings and open the link it returns."
            )
        if isinstance(exc, PermissionError_):
            return scrub(
                f"{name}: Kite says {exc.message!r} (PermissionException). Market data (quotes, historical candles, "
                "the WebSocket) needs the paid Kite Connect plan; the free Personal plan does not include it."
            )
        if isinstance(exc, OutcomeUnknown):
            return scrub(f"{name}: {exc.message}")
        if isinstance(exc, KiteAPIError):
            return scrub(f"{name}: Kite error {exc}")
        if isinstance(exc, ForbiddenCall):
            log.error("tool %s attempted a forbidden call: %s", name, scrub(str(exc)))
            return scrub(f"{name}: refused by the client's endpoint guard: {exc}")
        if isinstance(exc, ConfigError | KiteMCPError):
            return scrub(f"{name}: {exc}")
        log.exception("tool %s failed", name)
        return f"{name} failed with an internal error ({type(exc).__name__}); see the server log"

    def login_hint(self) -> str:
        if self.rt.settings.auth_mode == "external":
            return "The token file is maintained by another program; wait for its next login or check session_status."
        return "Call the login tool to get a fresh login link (tokens reset at 06:00 IST every day)."


# --- results ------------------------------------------------------------------------------------------------------


def _default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dt.datetime | dt.date):
        return value.isoformat()
    if isinstance(value, set | frozenset | tuple):
        return list(value)
    if hasattr(value, "model_dump"):
        return value.model_dump(exclude_none=True)
    return str(value)


def to_json(value: Any) -> str:
    """Compact JSON (the text an MCP client receives)."""
    if isinstance(value, str):
        return value
    return json.dumps(value, default=_default, separators=(",", ":"), ensure_ascii=False)


def paginate(items: Sequence[Any], limit: int, offset: int, **extra: Any) -> dict[str, Any]:
    if limit < 1 or limit > MAX_LIMIT:
        raise ToolInputError(f"limit must be between 1 and {MAX_LIMIT}")
    if offset < 0:
        raise ToolInputError("offset must be 0 or more")
    page = list(items[offset : offset + limit])
    out: dict[str, Any] = {"total": len(items), "offset": offset, "limit": limit, "count": len(page)}
    if offset + len(page) < len(items):
        out["next_offset"] = offset + len(page)
    out.update(extra)
    out["items"] = page
    return out


def parse_instrument(value: str) -> tuple[str, str]:
    """'NSE:INFY' -> ('NSE', 'INFY'). Raises ToolInputError for anything else."""
    exchange, sep, symbol = value.strip().partition(":")
    if not sep or not exchange or not symbol.strip():
        raise ToolInputError(f"instrument {value!r} must look like EXCHANGE:TRADINGSYMBOL (e.g. NSE:INFY)")
    return exchange.strip().upper(), symbol.strip().upper()


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ToolInputError(message)
