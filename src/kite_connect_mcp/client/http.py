"""The only module that talks to api.kite.trade.

    headers     X-Kite-Version: 3, `Authorization: token api_key:access_token`, a User-Agent naming this package
    answers     {"status": "success", "data": ...} or {"status": "error", "message", "error_type", "data"}; the
                instrument dumps are (possibly gzipped) CSV
    errors      mapped to the classes in errors.py; every credential the client holds is scrubbed from messages

The door. Every call must match an entry of ENDPOINTS, or it is refused before any I/O (ForbiddenCall). Each entry says
what kind of call it is:

    read        GET of account or market data
    calc        a POST that only computes (order margins, basket margins, charges): allowed in read-only mode
    auth        the session token exchange and logout: only in `managed` auth mode, and logout only from the logout flow
    write       anything that changes the account (orders, GTTs, alerts, conversions, authorisations, MF orders)

A write is sent only with a WriteTicket that the safety pipeline (safety/pipeline.py) issued for exactly this method,
path and payload; a ticket is good for one request. In read-only mode writes are refused even with a ticket.

Retries. Reads and calcs are retried on 429 (honouring Retry-After), on 502/503/504 and on transport errors, a few
times with backoff. Writes are NEVER retried: an error that may have happened after the request left the process
becomes OutcomeUnknown ("check the order book before trying again").
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import logging
import re
import zlib
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urlencode

import httpx2

from .. import __version__
from ..auth.tokenstore import TokenProvider
from ..config import Settings
from ..errors import (
    ConfigError,
    ForbiddenCall,
    KiteAPIError,
    NetworkError,
    NotLoggedIn,
    OutcomeUnknown,
    RateLimited,
    ServerError,
    TokenError,
    error_for,
)
from .ratelimit import RateLimiter

log = logging.getLogger(__name__)

Kind = Literal["read", "calc", "auth", "write"]
Params = Mapping[str, Any] | Sequence[tuple[str, Any]] | None

_VARIETY = r"(?:regular|amo|co|iceberg|auction)"
_ID = r"[A-Za-z0-9_\-]{1,64}"


@dataclass(frozen=True)
class Endpoint:
    method: str
    pattern: re.Pattern[str]
    kind: Kind
    group: str = "other"
    auth: Literal["required", "optional", "none"] = "required"


def _ep(method: str, path: str, kind: Kind, group: str = "other", auth: str = "required") -> Endpoint:
    return Endpoint(method, re.compile(path + r"\Z"), kind, group, auth)  # type: ignore[arg-type]


ENDPOINTS: tuple[Endpoint, ...] = (
    # session (https://kite.trade/docs/connect/v3/user/)
    _ep("POST", r"/session/token", "auth", auth="none"),
    _ep("DELETE", r"/session/token", "auth", auth="none"),
    # user
    _ep("GET", r"/user/profile", "read"),
    _ep("GET", r"/user/margins", "read"),
    _ep("GET", r"/user/margins/(?:equity|commodity)", "read"),
    # orders (https://kite.trade/docs/connect/v3/orders/)
    _ep("POST", rf"/orders/{_VARIETY}", "write", "order"),
    _ep("PUT", rf"/orders/{_VARIETY}/{_ID}", "write", "order"),
    _ep("DELETE", rf"/orders/{_VARIETY}/{_ID}", "write", "order"),
    _ep("GET", r"/orders", "read"),
    _ep("GET", rf"/orders/{_ID}", "read"),
    _ep("GET", rf"/orders/{_ID}/trades", "read"),
    _ep("GET", r"/trades", "read"),
    # portfolio (https://kite.trade/docs/connect/v3/portfolio/)
    _ep("GET", r"/portfolio/holdings", "read"),
    _ep("GET", r"/portfolio/holdings/auctions", "read"),
    _ep("POST", r"/portfolio/holdings/authorise", "write"),
    _ep("GET", r"/portfolio/positions", "read"),
    _ep("PUT", r"/portfolio/positions", "write"),
    # market quotes and instruments (https://kite.trade/docs/connect/v3/market-quotes/)
    _ep("GET", r"/quote", "read", "quote"),
    _ep("GET", r"/quote/ohlc", "read", "quote"),
    _ep("GET", r"/quote/ltp", "read", "quote"),
    _ep("GET", r"/instruments", "read", auth="optional"),
    _ep("GET", r"/instruments/[A-Z]{2,6}", "read", auth="optional"),
    # historical (https://kite.trade/docs/connect/v3/historical/)
    _ep("GET", r"/instruments/historical/\d{1,12}/[a-z0-9]{3,10}", "read", "historical"),
    # margins and charges (https://kite.trade/docs/connect/v3/margins/)
    _ep("POST", r"/margins/orders", "calc"),
    _ep("POST", r"/margins/basket", "calc"),
    _ep("POST", r"/charges/orders", "calc"),
    # GTT (https://kite.trade/docs/connect/v3/gtt/)
    _ep("GET", r"/gtt/triggers", "read"),
    _ep("GET", r"/gtt/triggers/\d{1,20}", "read"),
    _ep("POST", r"/gtt/triggers", "write"),
    _ep("PUT", r"/gtt/triggers/\d{1,20}", "write"),
    _ep("DELETE", r"/gtt/triggers/\d{1,20}", "write"),
    # alerts (https://kite.trade/docs/connect/v3/alerts/)
    _ep("GET", r"/alerts", "read"),
    _ep("GET", rf"/alerts/{_ID}", "read"),
    _ep("GET", rf"/alerts/{_ID}/history", "read"),
    _ep("POST", r"/alerts", "write"),
    _ep("PUT", rf"/alerts/{_ID}", "write"),
    _ep("DELETE", r"/alerts", "write"),
    # mutual funds (https://kite.trade/docs/connect/v3/mutual-funds/; the writes are legacy routes, opt-in)
    _ep("GET", r"/mf/orders", "read"),
    _ep("GET", rf"/mf/orders/{_ID}", "read"),
    _ep("GET", r"/mf/sips", "read"),
    _ep("GET", rf"/mf/sips/{_ID}", "read"),
    _ep("GET", r"/mf/holdings", "read"),
    _ep("GET", r"/mf/instruments", "read", auth="optional"),
    _ep("POST", r"/mf/orders", "write"),
    _ep("DELETE", rf"/mf/orders/{_ID}", "write"),
    _ep("POST", r"/mf/sips", "write"),
    _ep("PUT", rf"/mf/sips/{_ID}", "write"),
    _ep("DELETE", rf"/mf/sips/{_ID}", "write"),
)


def find_endpoint(method: str, path: str) -> Endpoint:
    for endpoint in ENDPOINTS:
        if endpoint.method == method and endpoint.pattern.match(path):
            return endpoint
    raise ForbiddenCall(f"{method} {path} is not an allowed Kite endpoint")


def payload_digest(method: str, path: str, params: Params, data: Params, json_body: Any) -> str:
    """A stable digest of a request, binding a WriteTicket to exactly what will be sent."""

    def norm(value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, Mapping):
            return sorted((str(k), str(v)) for k, v in value.items() if v is not None)
        if isinstance(value, list | tuple):
            return [(str(k), str(v)) for k, v in value if v is not None]
        return str(value)

    blob = json.dumps(
        [method, path, norm(params), norm(data), json.dumps(json_body, sort_keys=True, default=str)],
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(blob.encode()).hexdigest()


_TICKET_ISSUER = object()  # only safety/pipeline.py passes this; tests pin that no tool module references it


@dataclass
class WriteTicket:
    """Permission, from the safety pipeline, to send one specific write request once."""

    method: str
    path: str
    digest: str
    issuer: object = field(repr=False)
    used: bool = False


@dataclass(frozen=True)
class RawResponse:
    status: int
    content_type: str
    body: bytes = field(repr=False)


class KiteClient:
    def __init__(
        self,
        settings: Settings,
        tokens: TokenProvider,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
        limiter: RateLimiter | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        read_retries: int = 3,
    ):
        self.settings = settings
        self.tokens = tokens
        self._limiter = limiter or RateLimiter()
        self._sleep = sleep
        self._read_retries = read_retries
        self._http = httpx2.AsyncClient(
            base_url=settings.api_base_url,
            transport=transport,
            timeout=httpx2.Timeout(settings.http_timeout_s, connect=10.0),
            headers={"X-Kite-Version": "3", "User-Agent": f"kite-connect-mcp/{__version__}"},
            follow_redirects=False,
        )

    def __repr__(self) -> str:
        return f"KiteClient(base_url={self.settings.api_base_url!r})"

    async def aclose(self) -> None:
        await self._http.aclose()

    # --- the public surface ------------------------------------------------------------------------------------

    async def get(self, path: str, params: Params = None) -> Any:
        return await self.request("GET", path, params=params)

    async def calc(self, path: str, json_body: Any, params: Params = None) -> Any:
        """A computing POST (margins, charges): read-only in effect, allowed in read-only mode."""
        return await self.request("POST", path, params=params, json_body=json_body)

    async def get_csv(self, path: str) -> str:
        raw = await self.request("GET", path, expect="raw")
        return _decode_csv(raw.body)

    async def write(
        self,
        method: str,
        path: str,
        ticket: WriteTicket,
        *,
        params: Params = None,
        data: Params = None,
        json_body: Any = None,
    ) -> Any:
        """Send one write that the safety pipeline approved. Never retried."""
        return await self.request(method, path, params=params, data=data, json_body=json_body, ticket=ticket)

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Params = None,
        data: Params = None,
        json_body: Any = None,
        ticket: WriteTicket | None = None,
        allow_session_call: bool = False,
        access_token: str | None = None,
        expect: Literal["json", "raw"] = "json",
    ) -> Any:
        endpoint = find_endpoint(method, path)
        self._check_door(endpoint, method, path, params, data, json_body, ticket, allow_session_call)

        headers: dict[str, str] = {}
        token_used: str | None = None
        if endpoint.auth != "none":
            token_used = access_token or self._token_or_none(required=endpoint.auth == "required")
            if token_used is not None:
                headers["Authorization"] = f"token {self.settings.require_api_key()}:{token_used}"

        idempotent = endpoint.kind in ("read", "calc")
        attempts = 1 + (self._read_retries if idempotent else 0)
        last_error: Exception | None = None
        for attempt in range(attempts):
            await self._limiter.acquire(endpoint.group)
            try:
                if data is not None:  # form bodies are encoded here: repeated keys (isin=..&isin=..) must survive
                    response = await self._http.request(
                        method,
                        path,
                        params=_query(params),
                        content=urlencode(_pairs(data) or []).encode(),
                        headers={**headers, "Content-Type": "application/x-www-form-urlencoded"},
                    )
                else:
                    response = await self._http.request(
                        method, path, params=_query(params), json=json_body, headers=headers
                    )
            except (httpx2.ConnectError, httpx2.ConnectTimeout) as exc:
                last_error = NetworkError(f"could not connect to Kite: {type(exc).__name__}", error_type="NetworkError")
                if not idempotent:
                    raise last_error from None  # the request never left: safe to say it was not sent
            except httpx2.TransportError as exc:
                if not idempotent:
                    raise OutcomeUnknown(
                        f"no answer from Kite ({type(exc).__name__}); the request may have been executed. Check the "
                        "matching book (get_orders, get_gtts, get_alerts, get_mf_orders or get_mf_sips) before "
                        "trying again",
                        error_type="OutcomeUnknown",
                    ) from None
                last_error = NetworkError(
                    f"network error talking to Kite: {type(exc).__name__}", error_type="NetworkError"
                )
            else:
                try:
                    return self._finish(response, expect)
                except RateLimited as exc:
                    last_error = exc
                    if not idempotent:
                        raise
                    await self._sleep(_retry_after(response, attempt))
                    continue
                except ServerError as exc:
                    last_error = exc
                    if not idempotent or exc.status not in (502, 503, 504):
                        raise
                except TokenError:
                    if token_used:
                        self.tokens.reject(token_used)
                    raise
            if attempt + 1 < attempts:
                await self._sleep(min(8.0, 0.5 * (2**attempt)))
        assert last_error is not None
        raise last_error

    # --- internals --------------------------------------------------------------------------------------------------

    def _token_or_none(self, *, required: bool) -> str | None:
        record = self.tokens.current()
        if record is None:
            if required:
                status = self.tokens.status()
                raise NotLoggedIn(str(status.get("problem") or "not logged in to Kite"))
            return None
        return record.access_token

    def _check_door(
        self,
        endpoint: Endpoint,
        method: str,
        path: str,
        params: Params,
        data: Params,
        json_body: Any,
        ticket: WriteTicket | None,
        allow_session_call: bool,
    ) -> None:
        if endpoint.kind == "auth":
            if self.settings.auth_mode != "managed":
                raise ForbiddenCall(
                    "session calls are disabled in external auth mode (the token belongs to another program)"
                )
            if method == "DELETE" and not allow_session_call:
                raise ForbiddenCall("logout is only sent by the logout flow")
            return
        if endpoint.kind != "write":
            if ticket is not None:
                raise ForbiddenCall("a write ticket was passed to a read call")
            return
        if self.settings.read_only:
            raise ForbiddenCall(f"{method} {path}: read-only mode (KITE_MCP_READ_ONLY=true)")
        if ticket is None or ticket.issuer is not _TICKET_ISSUER:
            raise ForbiddenCall(f"{method} {path}: a write needs a ticket from the safety pipeline")
        if ticket.used:
            raise ForbiddenCall("this write ticket was already used")
        if (ticket.method, ticket.path) != (method, path) or ticket.digest != payload_digest(
            method, path, params, data, json_body
        ):
            raise ForbiddenCall("the write ticket does not match this request")
        ticket.used = True

    def _finish(self, response: httpx2.Response, expect: str) -> Any:
        content_type = response.headers.get("content-type", "")
        body = response.content
        if expect == "raw" and response.status_code < 400:
            return RawResponse(response.status_code, content_type, body)
        payload: Any = None
        if "json" in content_type or body[:1] in (b"{", b"["):
            try:
                payload = json.loads(body)
            except (ValueError, UnicodeDecodeError):
                payload = None
        if response.status_code < 400 and isinstance(payload, dict) and payload.get("status") != "error":
            return payload.get("data")
        if isinstance(payload, dict):
            message = self.scrub(str(payload.get("message") or "no message"))
            raise error_for(response.status_code, payload.get("error_type"), message, payload.get("data"))
        text = self.scrub(body[:200].decode("utf-8", "replace").strip()) or response.reason_phrase
        if response.status_code >= 400:
            raise error_for(response.status_code, None, f"HTTP {response.status_code}: {text}")
        raise KiteAPIError(
            f"unexpected answer from Kite (HTTP {response.status_code}, {content_type or 'no type'})",
            status=response.status_code,
            error_type="DataException",
        )

    def scrub(self, text: str) -> str:
        """`text` with every credential this client knows replaced."""
        secrets = list(self.settings.secrets())
        record = self.tokens.current()
        if record is not None:
            secrets.append(record.access_token)
        for secret in sorted(set(secrets), key=len, reverse=True):
            if secret:
                text = text.replace(secret, "[redacted]")
        return text


def _pairs(params: Params) -> list[tuple[str, str]] | None:
    """Query/form pairs with None dropped and booleans as Kite expects them ("true"/"false")."""
    if params is None:
        return None
    items: Iterable[tuple[str, Any]] = params.items() if isinstance(params, Mapping) else params
    out: list[tuple[str, str]] = []
    for key, value in items:
        if value is None:
            continue
        if isinstance(value, bool):
            out.append((key, "true" if value else "false"))
        elif isinstance(value, list | tuple):
            out.extend((key, str(v)) for v in value if v is not None)
        else:
            out.append((key, str(value)))
    return out


def _query(params: Params) -> httpx2.QueryParams | None:
    pairs = _pairs(params)
    return httpx2.QueryParams(tuple(pairs)) if pairs else None


def _retry_after(response: httpx2.Response, attempt: int) -> float:
    raw = response.headers.get("retry-after", "")
    try:
        return max(0.5, min(30.0, float(raw)))
    except ValueError:
        return min(8.0, 1.0 * (2**attempt))


def _decode_csv(body: bytes) -> str:
    if body[:2] == b"\x1f\x8b":
        try:
            body = gzip.decompress(body)
        except (OSError, zlib.error, EOFError) as exc:
            raise KiteAPIError(
                f"the instrument dump could not be unzipped: {exc}", error_type="DataException"
            ) from None
    return body.decode("utf-8-sig", "replace")


def checksum(api_key: str, request_token: str, api_secret: str) -> str:
    """SHA-256 hex of api_key + request_token + api_secret, as the token exchange requires."""
    if not (api_key and request_token and api_secret):
        raise ConfigError("the token exchange needs the API key, the request token and the API secret")
    return hashlib.sha256(f"{api_key}{request_token}{api_secret}".encode()).hexdigest()
