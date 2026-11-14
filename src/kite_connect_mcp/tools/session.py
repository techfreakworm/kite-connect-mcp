"""Tools: the Kite session (is the server logged in, log in, finish a login by hand, log out).

Kite sessions end every day at 06:00 IST. In `managed` auth mode this server owns the daily login: `login` hands out a
link, the person logs in at Kite, and Kite's redirect lands on this server's callback route (added by
`_callback_hook`) or the person pastes the request_token into `complete_login`. In `external` auth mode another program
keeps the token file: only `session_status` and an explanatory `login` exist, and `complete_login` / `logout` are not
registered at all. `logout` is not offered with KITE_MCP_ALLOW_LOGOUT=false, and with KITE_MCP_LOGIN_ALLOW_FORCE=false
`login` issues no link while a valid token exists, even with force=true. With a login link command
(KITE_MCP_LOGIN_LINK_COMMAND), `login(deliver=true)` hands the link to that command instead of returning it, and
`session_status` shows the login links' status (auth/reminders.py).

These tools are kind "auth": they reach /session/token through the login manager (auth/login.py) and never touch the
write pipeline.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from typing import TYPE_CHECKING, Annotated, Any

from mcp.server import MCPServer
from mcp_types import ToolAnnotations
from pydantic import Field

from ..auth.account import AccountPin
from ..auth.callback import CallbackRateLimiter, make_callback_handler
from ..auth.login import AI_RISK_WARNING, LoginManager, unpack_redirect
from ..config import Settings
from ..errors import ConfigError, KiteMCPError, SafetyRefusal, ToolInputError
from ..timeutil import next_token_reset, to_ist
from ._base import Registrar, Runtime

if TYPE_CHECKING:
    from ..auth.reminders import Attempt

LOGOUT_DIGEST = hashlib.sha256(b"logout").hexdigest()  # logout takes no arguments: one constant to confirm
LOGOUT = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=True)


def safety_line(s: Settings) -> str:
    """One line on how writes are guarded, for the status answer."""
    if s.read_only:
        effect = "no tool can change the account"
    elif s.trading_mode == "live":
        effect = "orders can be sent live, each after a dry run and a confirmation token"
    else:
        effect = "writes can only be previewed"
    return f"trading_mode={s.trading_mode}, read_only={str(s.read_only).lower()}: {effect}"


def _login_notes(settings: Settings, pin: AccountPin) -> list[str]:
    """The notes of every login answer, a link returned or delivered: what would make the login change nothing, and
    that any account would be accepted."""
    notes = []
    if settings.access_token:
        notes.append("KITE_ACCESS_TOKEN is set and takes precedence over the token file; unset it for this login")
    if not pin.pinned:
        notes.append(
            "no account pin is set (KITE_MCP_ALLOWED_USER_ID or KITE_MCP_ALLOWED_USER_ID_SHA256): any Kite "
            "account that completes this login is accepted"
        )
    return notes


def _delivery_answer(attempt: Attempt, notes: list[str]) -> dict[str, Any]:
    """The answer of login(deliver=true): the AI-risk warning and the notes of every login, whether the link command
    took the link, and when the link expires. Never the link: it went to the person, not to the model (an Attempt
    holds none)."""
    out: dict[str, Any] = {
        "warning": AI_RISK_WARNING,
        "delivered": attempt.outcome == "sent",
        "expires_at": attempt.as_dict()["link_expires_at"],
    }
    if attempt.outcome == "sent":
        out["message"] = (
            "Show the user the warning; the login link went to them through the link command. They log in at Kite; "
            "this server completes the login when Kite redirects to it. Then call session_status."
        )
    else:
        out["error"] = attempt.error
        out["message"] = (
            "The link could not be sent (see error). Call login with deliver=true to retry, or call login without "
            "deliver to get the link here."
        )
    if notes:
        out["notes"] = notes
    return out


def _manager(rt: Runtime) -> LoginManager:
    if rt.login is None:  # build_runtime always sets it
        raise ConfigError("the login manager is not available")
    return rt.login


def _callback_hook(rt: Runtime, manager: LoginManager) -> Callable[[MCPServer], None]:
    """What build_http_app calls with the MCPServer: adds GET <callback_path>. It is public (the bearer token protects
    /mcp only); the one-time state protects it."""
    handler = make_callback_handler(manager, limiter=CallbackRateLimiter())
    path = rt.settings.callback_path

    def hook(server: MCPServer) -> None:
        server.custom_route(path, methods=["GET"], include_in_schema=False)(handler)

    return hook


def register(reg: Registrar) -> None:
    _register_status_and_login(reg)
    if reg.rt.settings.auth_mode == "managed":
        _register_managed(reg)
    else:
        reg.skipped["complete_login"] = "external auth mode"
        reg.skipped["logout"] = "external auth mode"


def _register_status_and_login(reg: Registrar) -> None:
    rt = reg.rt
    settings = rt.settings
    managed = settings.auth_mode == "managed"
    pin = AccountPin.from_settings(settings)

    async def verify_with_kite() -> dict[str, Any]:
        try:
            profile = await rt.client.get("/user/profile")
        except KiteMCPError as exc:  # no token, expired, rejected, network...
            return {"ok": False, "error": rt.client.scrub(str(exc))}
        return {"ok": True, **pin.label(profile.get("user_id") if isinstance(profile, dict) else None)}

    @reg.tool("session_status", title="Session status", toolset="session", kind="read")
    async def session_status(
        verify: Annotated[
            bool,
            Field(
                description="also ask Kite once (GET /user/profile) whether it still accepts the token, and report "
                "the account (its user id, or with the hash pin KITE_MCP_ALLOWED_USER_ID_SHA256 its "
                "user_id_sha256_prefix) or Kite's error"
            ),
        ] = False,
    ) -> Any:
        """Whether this server is logged in to Kite, and what to do if it is not. Call this first: Kite sessions end
        every day at 06:00 IST.

        Returns `logged_in`, the token's `expires_at` and `minutes_left`, `auth_mode` (managed: this server does the
        daily login; external: another program keeps the token file and this server only reads it),
        `login_managed_here`, the token file path, `minutes_to_token_reset` (to the next 06:00 IST), a one-line
        `safety` summary (trading mode, read-only) and, when the server has a login link command, `login_links`: the
        scheduled times, the next one and the last attempt. The token itself is never shown, nor is a login link.

        verify=true also makes one live call to Kite and adds `verify` with the account (its user id, or with the hash
        pin its user_id_sha256_prefix) or Kite's error. Use it when a token that looks valid might have been ended
        elsewhere (logging in again with the same API key, or a master logout on the Kite web, ends the earlier
        token)."""
        check = await verify_with_kite() if verify else None
        now = rt.clock()
        reset = next_token_reset(now)
        out: dict[str, Any] = {
            **rt.tokens.status(),  # after the live check: a rejection shows up in it
            "auth_mode": settings.auth_mode,
            "login_managed_here": managed,
            "token_file": str(settings.token_file),
            "next_token_reset": to_ist(reset).isoformat(timespec="minutes"),
            "minutes_to_token_reset": int((reset - now).total_seconds() // 60),
            "safety": safety_line(settings),
        }
        if check is not None:
            out["verify"] = check
        if not out.get("logged_in"):
            if not managed:
                next_step = (
                    "another program maintains the token file; wait for its next login (this server cannot log in)"
                )
            elif rt.links is not None:
                next_step = (
                    "call login to get a Kite login link, or login(deliver=true) to have the server's link command "
                    "send it straight to the person"
                )
            else:
                next_step = "call login to get a Kite login link"
            out["next_step"] = next_step
        if managed and settings.access_token:
            out["note"] = "KITE_ACCESS_TOKEN is set and takes precedence over the token file; unset it to use logins"
        if rt.links is not None:
            out["login_links"] = rt.links.status()
        return out

    @reg.tool("login", title="Log in to Kite", toolset="session", kind="auth")
    async def login(
        force: Annotated[
            bool,
            Field(
                description="issue a new link even though a valid token exists (the new login ends the old token); "
                "refused when the server turns forcing off (KITE_MCP_LOGIN_ALLOW_FORCE=false)"
            ),
        ] = False,
        deliver: Annotated[
            bool,
            Field(
                description="send the link to the person through the server's login link command "
                "(KITE_MCP_LOGIN_LINK_COMMAND) instead of returning it: the answer then holds the warning, whether it "
                "was sent and when the link expires, never the link"
            ),
        ] = False,
    ) -> Any:
        """Start the daily Kite login (managed auth mode): returns a one-time login link for the person to open.
        Show them the `warning` and the link. They log in at Kite themselves (user id, password, 2FA); never ask for
        their credentials and never try to log in for them. With deliver=true the link goes straight to the person
        through the server's link command (KITE_MCP_LOGIN_LINK_COMMAND) and you never see it; show them the `warning`
        all the same.

        Kite then redirects to the app's registered redirect URL (`redirect_url`). This server completes the login
        when that redirect reaches it; if the redirect page cannot be reached, the person can copy the request_token
        from the browser's address bar (or the whole URL) and you call complete_login with it.

        If a valid token already exists nothing is issued, unless force=true (and never while forcing is off on the
        server); a new login ends the old token at Kite. In external auth mode another program owns the login: this
        only explains and shows the status."""
        status = rt.tokens.status()
        if not managed:
            return {
                "login_managed_here": False,
                "message": "The Kite token is managed by another program (KITE_MCP_AUTH_MODE=external). This server "
                "cannot log in or out; it reads the token file and picks up each new login by itself. If the "
                "session below is not valid, the login program has to run again.",
                "session": status,
                "token_file": str(settings.token_file),
            }
        if deliver and rt.links is None:
            raise ToolInputError(
                "deliver=true needs a login link command (KITE_MCP_LOGIN_LINK_COMMAND); without one, call login "
                "without deliver to get the link"
            )
        if status.get("logged_in") and not force:
            if settings.login_allow_force:
                advice = (
                    "Call login with force=true only if a fresh login is really wanted (it ends the current token at "
                    "Kite once completed)."
                )
            else:  # never advise a call the server refuses
                advice = (
                    "Forcing a new login is off on this server (KITE_MCP_LOGIN_ALLOW_FORCE=false): no link is issued "
                    "until the token expires or Kite rejects it."
                )
            return {
                "already_logged_in": True,
                "session": status,
                "message": f"Already logged in with a valid token. {advice}",
            }
        if deliver:  # forcing off with a valid token: issue_link refuses (audited) and nothing is sent
            assert rt.links is not None  # checked above
            return _delivery_answer(await rt.links.send("requested"), _login_notes(settings, pin))
        manager = _manager(rt)
        url, expires_at = await manager.issue_link()  # refuses (audited) while forcing is off and the token is valid
        redirect = manager.redirect_url()
        if settings.transport == "http":
            handling = (
                "This server handles that redirect itself and shows 'Kite login done, you can close this tab'. "
                "Then call session_status."
            )
        else:
            handling = (
                "This server runs over stdio and serves no web page. Either run `kite-connect-mcp login --listen` in "
                f"a terminal first (the app's redirect URL must then be http://127.0.0.1:{settings.port}"
                f"{settings.callback_path}), or let the redirect page fail to load and use the request_token from the "
                "address bar as described below."
            )
        out: dict[str, Any] = {
            "warning": AI_RISK_WARNING,
            "login_url": url,
            "expires_at": to_ist(expires_at).isoformat(timespec="seconds"),
            "redirect_url": redirect,
            "instructions": [
                "Show the user the warning, then the login_url to open in a browser.",
                "They log in at Kite. The link works for one login only.",
                f"Kite then redirects to the registered redirect URL, {redirect} (the Kite app's redirect URL must be "
                "exactly this).",
                handling,
                "If the redirect page cannot be reached, the user can copy the request_token from the browser's "
                "address bar (pasting the whole URL works too) and you call complete_login with it.",
            ],
        }
        notes = _login_notes(settings, pin)
        if notes:
            out["notes"] = notes
        return out


def _register_managed(reg: Registrar) -> None:
    """complete_login, logout (unless KITE_MCP_ALLOW_LOGOUT=false) and the HTTP callback route: only where this server
    owns the login."""
    rt = reg.rt
    settings = rt.settings
    pin = AccountPin.from_settings(settings)

    @reg.tool("complete_login", title="Complete the Kite login by hand", toolset="session", kind="auth")
    async def complete_login(
        request_token: Annotated[
            str,
            Field(
                min_length=8,
                max_length=2048,
                description="the request_token from the address bar after logging in at Kite; the whole redirected "
                "URL (which also carries the state) is accepted too",
            ),
        ],
        state: Annotated[
            str | None,
            Field(max_length=256, description="the state from the same URL (optional; if given it must be valid)"),
        ] = None,
    ) -> Any:
        """Finish a Kite login with a request_token the person copied from the browser's address bar after logging in
        (use it when the redirect page could not be reached). Exchanges the token with Kite and stores the access
        token for every client of this server. Request tokens are single use and last only a few minutes. With an
        account pin set (KITE_MCP_ALLOWED_USER_ID or KITE_MCP_ALLOWED_USER_ID_SHA256), a login by any other Kite
        account is ended and refused. With forcing off on the server (KITE_MCP_LOGIN_ALLOW_FORCE=false), a request
        token without the state of a login link is refused while a valid token exists: pass the whole URL."""
        token, linked_state = unpack_redirect(request_token, state)
        result = await _manager(rt).complete(token, linked_state, source="tool")
        if not result.ok:
            raise KiteMCPError(result.message)
        expires = to_ist(result.expires_at) if result.expires_at else None
        return {
            "logged_in": True,
            **pin.label(result.user_id),
            "expires_at": expires.isoformat(timespec="minutes") if expires else None,
            "message": "Logged in. The token is shared by every client of this server until the 06:00 IST reset.",
        }

    if settings.allow_logout:
        _register_logout(reg)
    else:  # the registrar's own reason first (toolset off, excluded), else the setting
        reg.skipped["logout"] = reg.enabled("logout", "session", "auth") or "KITE_MCP_ALLOW_LOGOUT=false"

    if rt.login is not None:
        rt.extras.setdefault("http_routes", []).append(_callback_hook(rt, rt.login))


def _register_logout(reg: Registrar) -> None:
    """logout, a two-step tool: managed auth mode only, and not with KITE_MCP_ALLOW_LOGOUT=false."""
    rt = reg.rt
    settings = rt.settings

    @reg.tool("logout", title="Log out of Kite", toolset="session", kind="auth", annotations=LOGOUT)
    async def logout(
        confirmation_token: Annotated[
            str | None,
            Field(description="from the first call; omit it to see what would happen and get one"),
        ] = None,
    ) -> Any:
        """End the Kite API session, delete the stored token and close any open login link (managed auth mode). Two
        steps: the first call (no confirmation_token) only describes what would happen and returns a
        confirmation_token; call again with it to do it. Every client using this token is signed out until the next
        login. It does not log the person out of the Kite web or mobile apps."""
        if confirmation_token is None:
            if rt.tokens.file.read() is None:
                return {"logged_out": False, "message": "There is no stored token; nothing to log out of."}
            token, expires_at = rt.confirmations.issue("logout", LOGOUT_DIGEST)
            return {
                "logged_out": False,
                "would_do": f"end the Kite API session (DELETE /session/token) and delete {settings.token_file}",
                "confirmation_token": token,
                "confirmation_expires_at": to_ist(expires_at).isoformat(timespec="seconds"),
                "next_step": "call logout again with this confirmation_token to go ahead",
            }
        try:
            rt.confirmations.consume(confirmation_token, "logout", LOGOUT_DIGEST)
        except SafetyRefusal:
            raise ToolInputError(
                "the confirmation_token is unknown, already used or expired: call logout without one to get a new one"
            ) from None
        result = await _manager(rt).logout(source="tool")
        return {
            "logged_out": True,
            "kite_session": result.kite,
            "token_file_removed": result.file_removed,
            "next_step": "call login for a new link when a session is needed again",
        }
