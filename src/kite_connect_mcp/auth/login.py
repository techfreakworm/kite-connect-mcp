"""Managed-mode login and logout: the login link, the state-checked callback, the token exchange, the CLI.

    issue_link()            a Kite login URL carrying a one-time `state` (passed through Kite's `redirect_params`)
    handle_callback(q)      the query of Kite's redirect: status=success, one state, one request_token
    complete(token, state)  the same exchange for a request_token a person pasted by hand
    logout()                DELETE /session/token, then delete the token file

The human always logs in at Kite; nothing here types credentials for them (Zerodha's guidance forbids automating it).

The exchange, for a callback or a pasted token (`_finish`), one at a time per process:

    reserve the state (if any)  ->  POST /session/token  ->  account pin  ->  write the token file  ->  commit

A state is reserved for one exchange and committed only once the token is on disk, which also closes every other open
link. Whatever fails before that releases the state, so the same link can be tried again with a fresh Kite login. If
KITE_MCP_ALLOWED_USER_ID is set and Kite says another account logged in, that new session is ended at once, nothing is
stored, and the login is refused.

What is never written anywhere (logs, audit log, results, error messages): the access token, the request token, the
state, names, e-mail addresses. The audit log gets event names, reason codes and the user id of a successful login.

In `external` auth mode every entry point refuses before doing anything; the HTTP client refuses session calls too.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
import secrets
import sys
import time
from collections import deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlencode

import httpx2
from starlette.applications import Starlette
from starlette.routing import Route

from ..client.http import KiteClient, checksum
from ..config import Settings
from ..errors import ConfigError, KiteAPIError, KiteMCPError, OutcomeUnknown, TokenError, ToolInputError
from ..safety.audit import AuditLog
from ..timeutil import next_token_reset, now_ist, to_ist
from .callback import make_callback_handler
from .loginstate import Reservation, StateStore
from .tokenstore import TokenProvider, TokenRecord

log = logging.getLogger(__name__)

# --- constants (module level on purpose: tests change them, there are no settings for them) -------------------------

STATE_FILE = "login_states.json"  # under settings.state_dir
STATE_BYTES = 32  # entropy of a state (token_urlsafe: 43 characters)
LINK_CUTOFF = dt.timedelta(minutes=1)  # links die this long before the 06:00 IST token reset
STATE_RE = re.compile(r"[A-Za-z0-9_\-]{16,128}")
REQUEST_TOKEN_RE = re.compile(r"[A-Za-z0-9_\-]{8,128}")
_ACCESS_TOKEN_RE = re.compile(r"[A-Za-z0-9._\-]{8,512}")  # the shape tokenstore.parse_token_file accepts
_USER_ID_RE = re.compile(r"[A-Za-z0-9]{2,32}")

AI_RISK_WARNING = (
    "WARNING: AI systems are unpredictable and non-deterministic. By continuing, you agree to interact with your "
    "Zerodha account via AI at your own risk."
)
LISTEN_TIMEOUT_S = 600.0  # how long `login --listen` waits for the redirect
NOISE_BURST = 30  # malformed / unknown-state callbacks written to the audit log per NOISE_PERIOD_S
NOISE_PERIOD_S = 60.0

QueryParams = Mapping[str, str | Sequence[str]] | Iterable[tuple[str, str]]


# --- results ------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class LoginResult:
    """How a login attempt ended. `reason` is "ok" or a code: bad_request, bad_state, exchange_failed,
    account_mismatch, token_not_stored, not_configured. `message` is safe to show: it never holds a token or state."""

    ok: bool
    reason: str
    message: str = ""
    user_id: str | None = None
    expires_at: dt.datetime | None = None


@dataclass(frozen=True)
class LogoutResult:
    kite: str  # "ended", "already_invalid" (Kite no longer knew the token) or "no_token"
    file_removed: bool


# --- the link ------------------------------------------------------------------------------------------------------


def link_deadline(now: dt.datetime, ttl_s: int) -> dt.datetime:
    """When a link issued at `now` stops working: `ttl_s` later, but never after 05:59 IST (tokens reset at 06:00, and
    a login finished in the last minute would give a token that is already dead). In that last minute itself the cap
    moves to the next day's 05:59, so a link is never born expired."""
    now = to_ist(now)
    cap = next_token_reset(now) - LINK_CUTOFF
    if cap <= now:
        cap += dt.timedelta(days=1)
    return min(now + dt.timedelta(seconds=ttl_s), cap)


def build_login_url(login_url: str, api_key: str, state: str) -> str:
    """`<login_url>?v=3&api_key=..&redirect_params=state%3D..`: Kite gives redirect_params back as plain query
    parameters on the registered redirect URL."""
    query = urlencode({"v": "3", "api_key": api_key, "redirect_params": f"state={state}"})
    return f"{login_url}{'&' if '?' in login_url else '?'}{query}"


def unpack_redirect(value: str, state: str | None = None) -> tuple[str, str | None]:
    """(request_token, state) from what a person pasted: the bare token, or the URL they were redirected to (or just
    its query string). A state passed separately wins over the one in the URL."""
    text = value.strip()
    if "request_token=" not in text:
        return text, state
    text = text.partition("#")[0]
    found = parse_qs(text.partition("?")[2] if "?" in text else text)
    tokens = found.get("request_token", [])
    if len(tokens) != 1:
        raise ToolInputError("expected exactly one request_token in the pasted URL")
    if found.get("status", ["success"]) != ["success"]:
        raise ToolInputError("the pasted URL says the Kite login did not succeed; open a new login link")
    states = found.get("state", [])
    return tokens[0], state or (states[0] if len(states) == 1 else None)


# --- logs ----------------------------------------------------------------------------------------------------------

_SESSION_URL_TOKEN = re.compile(r"(access_token=)[^&\s\"']+")


def _redacted(value: object) -> object:
    text = str(value)
    return _SESSION_URL_TOKEN.sub(r"\1[redacted]", text) if "access_token=" in text else value


class _RedactAccessTokens(logging.Filter):
    """httpx2 logs every request URL at INFO, and Kite's logout carries the access token in the query string. The CLI
    keeps that logger at WARNING; this makes the redaction hold for any other host of the package too."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(_redacted(a) for a in record.args)
        if isinstance(record.msg, str):
            record.msg = _SESSION_URL_TOKEN.sub(r"\1[redacted]", record.msg)
        return True


def _protect_http_logs() -> None:
    logger = logging.getLogger("httpx2")
    if not any(isinstance(f, _RedactAccessTokens) for f in logger.filters):
        logger.addFilter(_RedactAccessTokens())


# --- the manager ---------------------------------------------------------------------------------------------------


class LoginManager:
    """Everything about the daily login that this server does itself. Cheap to construct: nothing is read, written or
    sent until a method is called. Methods that act raise KiteMCPError in external auth mode."""

    def __init__(
        self,
        settings: Settings,
        client: KiteClient,
        tokens: TokenProvider,
        audit: AuditLog,
        *,
        clock: Callable[[], dt.datetime] = now_ist,
    ):
        self.settings = settings
        self.client = client
        self.tokens = tokens
        self.audit = audit
        self.clock = clock
        self.states = StateStore(settings.state_dir / STATE_FILE)
        self._flow_lock = asyncio.Lock()  # one exchange or logout at a time, so token writes cannot land out of order
        self._noise: deque[float] = deque()
        self._noise_dropped = 0
        _protect_http_logs()

    # --- facts --------------------------------------------------------------------------------------------------------

    @property
    def managed(self) -> bool:
        return self.settings.auth_mode == "managed"

    def redirect_url(self) -> str:
        """The URL the Kite app's redirect must be set to: <public_url><callback_path>, or the loopback address."""
        s = self.settings
        return f"{s.public_url or f'http://127.0.0.1:{s.port}'}{s.callback_path}"

    def _require_managed(self) -> None:
        if not self.managed:
            raise ConfigError(
                "the Kite login is managed by another program (KITE_MCP_AUTH_MODE=external): this server never logs "
                "in, logs out or changes the token file"
            )

    # --- the link -----------------------------------------------------------------------------------------------------

    async def issue_link(self, *, source: str = "tool") -> tuple[str, dt.datetime]:
        """A new login URL and when it expires. Only the digest of its state is kept."""
        self._require_managed()
        api_key = self.settings.require_api_key()
        self.settings.require_api_secret()  # a link that could never be completed is not worth issuing
        now = to_ist(self.clock())
        expires_at = link_deadline(now, self.settings.login_link_ttl_s)
        state = secrets.token_urlsafe(STATE_BYTES)
        await asyncio.to_thread(self.states.issue, state, now, expires_at)
        self._audit("login_link_issued", source=source, expires_at=expires_at)
        return build_login_url(self.settings.login_url, api_key, state), expires_at

    # --- completing a login -------------------------------------------------------------------------------------------

    async def handle_callback(self, query_params: QueryParams, *, client: str | None = None) -> LoginResult:
        """Kite's redirect. Only status=success with exactly one state and one request_token of a sane shape goes any
        further; the state must be one this server issued and still open. `client` is for the audit log only."""
        self._require_managed()
        found = _group(query_params)
        status, state, token = _single(found, "status"), _single(found, "state"), _single(found, "request_token")
        if (
            status != "success"
            or state is None
            or token is None
            or not STATE_RE.fullmatch(state)
            or not REQUEST_TOKEN_RE.fullmatch(token)
        ):
            return self._refuse("bad_request", "the redirect is not a successful Kite login", "callback", client)
        return await self._finish(token, state, "callback", client)

    async def complete(self, request_token: str, state: str | None = None, *, source: str = "tool") -> LoginResult:
        """Exchange a request_token a person pasted. The state is optional, but if given it must be valid (and is used
        up like any other)."""
        self._require_managed()
        token = request_token.strip()
        if not REQUEST_TOKEN_RE.fullmatch(token):
            return self._refuse(
                "bad_request", "that does not look like a Kite request_token (8 to 128 letters, digits, - or _)", source
            )
        state = (state or "").strip() or None
        if state is not None and not STATE_RE.fullmatch(state):
            return self._refuse("bad_state", "that does not look like a state from a login link", source)
        return await self._finish(token, state, source, None)

    async def _finish(self, request_token: str, state: str | None, source: str, client: str | None) -> LoginResult:
        async with self._flow_lock:
            reservation: Reservation | None = None
            if state is not None:
                reservation = await asyncio.to_thread(self.states.reserve, state, to_ist(self.clock()))
                if reservation is None:
                    return self._refuse(
                        "bad_state",
                        "this login link is unknown, expired, already used or in use; call login for a new one",
                        source,
                        client,
                    )
            try:
                result = await self._exchange_and_store(request_token, state, source)
            except BaseException:
                await self._release(reservation)
                raise
            if result.ok:
                await self._commit(reservation)
            else:
                await self._release(reservation)
            return result

    async def _exchange_and_store(self, request_token: str, state: str | None, source: str) -> LoginResult:
        try:
            api_key, api_secret = self.settings.require_api_key(), self.settings.require_api_secret()
        except ConfigError as exc:
            return self._refuse("not_configured", str(exc), source)
        try:
            answer = await self.client.request(
                "POST",
                "/session/token",
                data={
                    "api_key": api_key,
                    "request_token": request_token,
                    "checksum": checksum(api_key, request_token, api_secret),
                },
            )
        except OutcomeUnknown:
            return self._refuse(
                "exchange_failed",
                "Kite did not answer the token exchange, so the request token may be used up; log in again from a "
                "login link",
                source,
            )
        except KiteAPIError as exc:
            detail = self._scrubbed(exc.message, request_token, state)
            return self._refuse(
                "exchange_failed",
                f"Kite refused the token exchange ({exc.error_type}: {detail}). Request tokens are single use and "
                "last only a few minutes; log in again from a login link",
                source,
            )
        unpacked = _read_exchange(answer)
        if unpacked is None:
            return self._refuse("exchange_failed", "Kite's answer held no usable access token", source)
        access_token, user_id = unpacked

        pin = (self.settings.allowed_user_id or "").upper()
        if pin and user_id != pin:
            ended = await self._invalidate(api_key, access_token)
            return self._refuse(
                "account_mismatch",
                "the Kite account that logged in is not the one this server is pinned to (KITE_MCP_ALLOWED_USER_ID); "
                + (
                    "that new session was ended and nothing was stored"
                    if ended
                    else "nothing was stored, but ending that new session at Kite failed (it lapses at 06:00 IST)"
                ),
                source,
                session_ended=ended,
            )

        record = TokenRecord.new(access_token, to_ist(self.clock()))
        try:
            await asyncio.to_thread(self.tokens.file.write, record)
        except (OSError, ConfigError) as exc:
            ended = await self._invalidate(api_key, access_token)  # nobody holds this token now: do not leave it alive
            reason = exc.strerror if isinstance(exc, OSError) else str(exc)
            return self._refuse(
                "token_not_stored",
                f"the token file could not be written ({reason}); the new Kite session was "
                + ("ended" if ended else "left to lapse at 06:00 IST")
                + ". Fix the file or folder permissions and log in again",
                source,
            )
        self.tokens.accept(access_token)
        self._audit("login_ok", source=source, user_id=user_id, expires_at=record.expires_at)
        log.info("Kite login completed (%s)", source)
        return LoginResult(True, "ok", "logged in", user_id=user_id, expires_at=record.expires_at)

    async def _commit(self, reservation: Reservation | None) -> None:
        try:
            await asyncio.to_thread(self.states.commit, reservation, to_ist(self.clock()))
        except KiteMCPError as exc:  # the token is on disk: the login stands; only the old links stay open
            log.warning("login succeeded but the login link states could not be closed (%s)", type(exc).__name__)

    async def _close_links(self) -> None:
        try:
            await asyncio.to_thread(self.states.close_pending, to_ist(self.clock()))
        except KiteMCPError as exc:  # the session is over either way
            log.warning("logged out, but open login links could not be closed (%s)", type(exc).__name__)

    async def _release(self, reservation: Reservation | None) -> None:
        if reservation is None:
            return
        try:
            await asyncio.to_thread(self.states.release, reservation)
        except KiteMCPError as exc:  # it lapses by itself after the reservation TTL
            log.warning("could not release a login link state (%s)", type(exc).__name__)

    async def _invalidate(self, api_key: str, access_token: str) -> bool:
        """End a session we must not keep. Never raises; says whether Kite confirmed."""
        try:
            await self.client.request(
                "DELETE",
                "/session/token",
                params={"api_key": api_key, "access_token": access_token},
                allow_session_call=True,
                access_token=access_token,
            )
        except KiteMCPError as exc:
            log.warning("could not end a Kite session that must not be kept (%s)", type(exc).__name__)
            return False
        return True

    # --- logout -------------------------------------------------------------------------------------------------------

    async def logout(self, *, source: str = "tool") -> LogoutResult:
        """End the Kite session of the stored token, delete the token file and close every open login link. If Kite
        cannot be reached the file is kept (deleting the only copy of a live token would leave it unrevocable) and an
        error is raised. A token Kite no longer knows, or one past its expiry, counts as ended."""
        self._require_managed()
        async with self._flow_lock:
            record = await asyncio.to_thread(self.tokens.file.read)
            kite = "no_token"  # nothing usable on disk: an unusable file just goes
            if record is not None:
                kite = await self._end_session(record, source)
                self.tokens.reject(record.access_token)  # whatever happens to the file, never serve it again
            try:
                removed = await asyncio.to_thread(self.tokens.file.remove)
            except OSError as exc:
                self._audit("logout_failed", source=source, error=type(exc).__name__, kite=kite)
                raise KiteMCPError(
                    f"the Kite session was ended, but the token file could not be deleted ({exc.strerror}); "
                    "delete it by hand"
                ) from None
            await self._close_links()
            self._audit("logout_ok", source=source, kite=kite, file_removed=removed)
            return LogoutResult(kite, removed)

    async def _end_session(self, record: TokenRecord, source: str) -> str:
        """DELETE /session/token for the stored token: "ended", or "already_invalid" if Kite no longer knows it (or it
        is past its expiry and Kite cannot be reached). Raises if a live token could not be ended."""
        token = record.access_token
        try:
            await self.client.request(
                "DELETE",
                "/session/token",
                params={"api_key": self.settings.require_api_key(), "access_token": token},
                allow_session_call=True,
                access_token=token,
            )
        except TokenError:
            return "already_invalid"
        except KiteMCPError as exc:
            if not record.valid_at(to_ist(self.clock())):
                return "already_invalid"  # past its 06:00 expiry the token is dead whatever Kite answers
            detail = self._scrubbed(str(exc), token)
            self._audit("logout_failed", source=source, error=type(exc).__name__)
            raise KiteMCPError(
                f"could not end the Kite session ({detail}); the token file was kept, try again"
            ) from None
        return "ended"

    # --- audit and messages -------------------------------------------------------------------------------------------

    def _refuse(self, reason: str, message: str, source: str, client: str | None = None, **fields: Any) -> LoginResult:
        self.record_refusal(reason, source=source, client=client, **fields)
        return LoginResult(False, reason, message)

    def record_refusal(self, reason: str, *, source: str, client: str | None = None, **fields: Any) -> None:
        """Audit a refused login. Junk aimed at the public callback (malformed, unknown state, rate limited, errors) is
        throttled so it cannot fill the disk; everything past a valid state is always written."""
        suppressed = 0
        if source == "callback" and reason in ("bad_request", "bad_state", "rate_limited", "error"):
            if not self._noise_allowed():
                return
            suppressed, self._noise_dropped = self._noise_dropped, 0
        if suppressed:
            fields["suppressed_before"] = suppressed
        if client:
            fields["client"] = client
        log.log(logging.DEBUG if source == "callback" else logging.INFO, "login refused: %s (%s)", reason, source)
        self._audit("login_refused", source=source, reason=reason, **fields)

    def _noise_allowed(self) -> bool:
        now = time.monotonic()
        while self._noise and now - self._noise[0] >= NOISE_PERIOD_S:
            self._noise.popleft()
        if len(self._noise) >= NOISE_BURST:
            self._noise_dropped += 1
            return False
        self._noise.append(now)
        return True

    def _audit(self, event: str, **fields: Any) -> None:
        tool = "logout" if event.startswith("logout") else "login"
        try:
            self.audit.write(event, tool=tool, **fields)
        except OSError as exc:  # a full disk must not undo a login the person just did
            log.warning("could not write the audit log (%s)", exc.strerror)

    def _scrubbed(self, text: str, *secrets_: str | None) -> str:
        text = self.client.scrub(text)
        for secret in secrets_:
            if secret:
                text = text.replace(secret, "[redacted]")
        return text


def _group(query: QueryParams) -> dict[str, list[str]]:
    """Every value per key, so a repeated key can be seen (Starlette's QueryParams map only keeps the last one)."""
    multi = getattr(query, "multi_items", None)
    pairs: Iterable[tuple[str, str]]
    found: dict[str, list[str]] = {}
    if callable(multi):
        pairs = multi()
    elif isinstance(query, Mapping):
        for key, value in query.items():
            found[key] = [value] if isinstance(value, str) else list(value)
        return found
    else:
        pairs = query
    for key, value in pairs:
        found.setdefault(key, []).append(value)
    return found


def _single(found: Mapping[str, list[str]], key: str) -> str | None:
    values = found.get(key, [])
    return values[0] if len(values) == 1 else None


def _read_exchange(answer: Any) -> tuple[str, str | None] | None:
    """(access_token, upper-cased user_id) from Kite's exchange answer; everything else in it is dropped unread."""
    if not isinstance(answer, dict):
        return None
    token = answer.get("access_token")
    if not isinstance(token, str) or not _ACCESS_TOKEN_RE.fullmatch(token):
        return None
    user = answer.get("user_id")
    user_id = user.strip().upper() if isinstance(user, str) and _USER_ID_RE.fullmatch(user.strip()) else None
    return token, user_id


# --- the command line ----------------------------------------------------------------------------------------------


def _note(text: str) -> None:
    print(text, file=sys.stderr, flush=True)


async def cli_login(
    settings: Settings,
    *,
    request_token: str | None,
    listen: bool,
    transport: httpx2.AsyncBaseTransport | None = None,
    clock: Callable[[], dt.datetime] | None = None,
) -> int:
    """`kite-connect-mcp login`. Without options: print a login link (stdout; everything else goes to stderr). With
    `request_token`: exchange it. With `listen`: print the link, then serve the callback on KITE_MCP_HOST:KITE_MCP_PORT
    until one login completes or LISTEN_TIMEOUT_S passes. `transport` and `clock` exist for tests. Exit codes: 0 done,
    1 the login did not happen, 2 not possible in this configuration."""
    if settings.auth_mode != "managed":
        _note(
            "login is managed by another program (KITE_MCP_AUTH_MODE=external); this server does not log in. "
            "`kite-connect-mcp status` shows the token it is using."
        )
        return 2
    from ..server import build_runtime  # imported here: server imports the tool modules, which import this one

    rt = build_runtime(settings, transport=transport, clock=clock or now_ist)
    manager = rt.login
    try:
        if manager is None:  # build_runtime always sets it
            raise ConfigError("the login manager is not available")
        if request_token:
            return await _login_with_token(manager, request_token)
        return await _login_with_link(manager, listen=listen)
    finally:
        await rt.aclose()


async def _login_with_token(manager: LoginManager, pasted: str) -> int:
    token, state = unpack_redirect(pasted)
    result = await manager.complete(token, state, source="cli")
    if not result.ok:
        _note(f"login failed: {result.message}")
        return 1
    until = to_ist(result.expires_at) if result.expires_at else None
    who = f" as {result.user_id}" if result.user_id else ""
    print(f"Logged in{who}. The token works until {until:%Y-%m-%d %H:%M} IST." if until else f"Logged in{who}.")
    return 0


async def _login_with_link(manager: LoginManager, *, listen: bool) -> int:
    url, expires_at = await manager.issue_link(source="cli")
    print(url, flush=True)
    redirect = manager.redirect_url()
    lines = [
        AI_RISK_WARNING,
        f"Open the link above, log in at Kite, and Kite redirects to {redirect}",
        f"The link works until {to_ist(expires_at):%Y-%m-%d %H:%M} IST and for one login only.",
    ]
    if listen:
        settings = manager.settings
        local = f"http://127.0.0.1:{settings.port}{settings.callback_path}"
        lines += [
            f"Listening on {settings.host}:{settings.port} for up to {int(LISTEN_TIMEOUT_S // 60)} minutes.",
            f"This only works if the redirect URL of your Kite app is {local}",
            "(a server already running with the HTTP transport handles the redirect itself: no --listen needed).",
        ]
    else:
        lines += [
            "If the redirect page does not load, copy the request_token from the address bar and run:",
            "  kite-connect-mcp login --request-token <request_token>",
        ]
    _note("\n".join(lines))
    if not listen:
        return 0
    return 0 if await _listen_for_login(manager) else 1


async def _listen_for_login(manager: LoginManager) -> bool:
    """Serve only the callback path until one login completes (True), the timeout passes, or the port cannot be bound
    (False)."""
    import uvicorn  # imported here: only this command needs a server of its own

    settings = manager.settings
    done, failed = asyncio.Event(), asyncio.Event()

    async def finished() -> None:  # run by Starlette once the success page is out, on the event loop
        done.set()

    handler = make_callback_handler(manager, on_success=finished)
    app = Starlette(routes=[Route(settings.callback_path, handler, methods=["GET"])])
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=settings.host,
            port=settings.port,
            log_level="warning",
            access_log=False,  # the query string holds one-time secrets
            lifespan="off",
            server_header=False,
            date_header=False,
            timeout_graceful_shutdown=5,
        )
    )

    async def serve() -> None:
        try:
            await server.serve()
        except SystemExit:  # uvicorn exits the process when it cannot bind the port
            failed.set()

    serving = asyncio.create_task(serve())
    logged_in = asyncio.create_task(done.wait())
    timed_out = False
    try:
        try:
            async with asyncio.timeout(LISTEN_TIMEOUT_S):
                await asyncio.wait({logged_in, serving}, return_when=asyncio.FIRST_COMPLETED)
        except TimeoutError:
            timed_out = True
    finally:
        logged_in.cancel()
        server.should_exit = True
        await serving
    if failed.is_set():
        _note(
            f"could not listen on {settings.host}:{settings.port}: is something else using it? If kite-connect-mcp "
            "already serves HTTP there, its own callback handles the redirect: just open the link."
        )
    elif timed_out:
        _note(f"no login arrived within {LISTEN_TIMEOUT_S / 60:g} minutes; run the command again for a new link")
    elif not done.is_set():
        _note("stopped before a login arrived; run the command again for a new link")
    return done.is_set()


async def cli_logout(
    settings: Settings,
    *,
    yes: bool,
    transport: httpx2.AsyncBaseTransport | None = None,
    clock: Callable[[], dt.datetime] | None = None,
) -> int:
    """`kite-connect-mcp logout --yes`: end the Kite session of the stored token and delete the token file. Exit
    codes: 0 done, 1 Kite could not be told, 2 not possible in this configuration or not confirmed."""
    if settings.auth_mode != "managed":
        _note("the token is managed by another program (KITE_MCP_AUTH_MODE=external); this server does not log out.")
        return 2
    if not yes:
        _note(
            "this ends the Kite API session (every client using the token is signed out) and deletes "
            f"{settings.token_file}; run again with --yes to do it"
        )
        return 2
    from ..server import build_runtime

    rt = build_runtime(settings, transport=transport, clock=clock or now_ist)
    manager = rt.login
    try:
        if manager is None:
            raise ConfigError("the login manager is not available")
        result = await manager.logout(source="cli")
    finally:
        await rt.aclose()
    if result.kite == "no_token":
        print("There was no stored token." + (" The unusable token file was deleted." if result.file_removed else ""))
    else:
        how = "ended" if result.kite == "ended" else "had already ended"
        print(f"Logged out: the Kite session {how} and the token file was deleted.")
    return 0
