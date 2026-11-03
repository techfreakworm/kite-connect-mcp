"""The session tools through the MCP client: session_status, login, complete_login, logout, and which of them exist in
each auth mode. Kite is FakeKite; every secret in here is fake."""

from __future__ import annotations

import json
import logging
from urllib.parse import urlsplit

import httpx2
import pytest
from mcp import Client

from conftest import ACCESS_TOKEN, API_KEY, BEARER, FIXED_NOW, tool_json, tool_text
from kite_connect_mcp.auth import loginstate
from kite_connect_mcp.auth.loginstate import digest_of
from kite_connect_mcp.server import build_http_app, build_runtime, build_server
from kite_login import (
    FORBIDDEN_IN_LOGS,
    NEW_TOKEN,
    REQUEST_TOKEN,
    USER_ID,
    KiteSession,
    callback_params,
    state_of,
)

WARNING = (
    "WARNING: AI systems are unpredictable and non-deterministic. By continuing, you agree to interact with your "
    "Zerodha account via AI at your own risk."
)
STATE = "TestState0000000000000000000000000000001"
SESSION_TOOLS = {"session_status", "login", "complete_login", "logout"}


@pytest.fixture
def kite(fake_kite) -> KiteSession:
    return KiteSession(fake_kite)


async def call(server, name, args=None):
    async with Client(server) as client:
        return await client.call_tool(name, args or {})


async def _no_sleep(_: float) -> None:
    return None


def server_at(settings_factory, fake_kite, moment):
    rt = build_runtime(settings_factory(), transport=fake_kite.transport(), clock=lambda: moment, sleep=_no_sleep)
    server, _ = build_server(rt)
    return server, rt


def stored_states(rt) -> dict:
    return json.loads((rt.settings.state_dir / "login_states.json").read_text())["states"]


# --- registration -----------------------------------------------------------------------------------------------------


async def test_managed_mode_registers_all_four_tools(mcp_factory, kite):
    server, reg, _ = mcp_factory()
    kinds = {i.name: i.kind for i in reg.registered if i.toolset == "session"}
    assert kinds == {"session_status": "read", "login": "auth", "complete_login": "auth", "logout": "auth"}
    async with Client(server) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert set(tools) >= SESSION_TOOLS
    assert tools["logout"].annotations.destructive_hint is True
    assert tools["session_status"].annotations.read_only_hint is True
    assert tools["login"].annotations.destructive_hint is False
    assert "request_token" in tools["complete_login"].input_schema["properties"]


async def test_external_mode_has_no_complete_login_or_logout(mcp_factory, kite, fake_kite):
    server, reg, _ = mcp_factory(KITE_MCP_AUTH_MODE="external")
    assert {i.name for i in reg.registered if i.toolset == "session"} == {"session_status", "login"}
    assert reg.skipped["complete_login"] == reg.skipped["logout"] == "external auth mode"
    async with Client(server) as client:
        names = {t.name for t in (await client.list_tools()).tools}
        assert not {"complete_login", "logout"} & names
        refused = await client.call_tool("logout", {"confirmation_token": "confirm_x"})
    assert refused.is_error
    assert fake_kite.requests == []


async def test_the_callback_route_does_not_depend_on_the_session_toolset(mcp_factory, kite):
    _, reg, rt = mcp_factory(KITE_MCP_TOOLSETS="account")  # links from the CLI still need somewhere to land
    assert not [i for i in reg.registered if i.toolset == "session"]
    assert "session" in reg.skipped.get("login", "")
    assert len(rt.extras["http_routes"]) == 1


async def test_session_tools_can_be_excluded_one_by_one(mcp_factory, kite):
    _, reg, _ = mcp_factory(KITE_MCP_EXCLUDED_TOOLS="logout")
    assert "logout" not in {i.name for i in reg.registered}
    assert reg.skipped["logout"] == "listed in KITE_MCP_EXCLUDED_TOOLS"


# --- session_status ---------------------------------------------------------------------------------------------------


async def test_status_of_a_valid_login(mcp_factory, kite, fake_kite):
    server, _, rt = mcp_factory()
    result = await call(server, "session_status")
    out = tool_json(result)
    assert out["logged_in"] is True and out["source"] == "file" and out["expired"] is False
    assert out["expires_at"] == "2026-11-05T06:00:00+05:30" and out["minutes_left"] == 1200
    assert out["auth_mode"] == "managed" and out["login_managed_here"] is True
    assert out["token_file"] == str(rt.settings.token_file)
    assert out["next_token_reset"] == "2026-11-05T06:00+05:30" and out["minutes_to_token_reset"] == 1200
    assert out["safety"] == "trading_mode=dry_run, read_only=false: writes can only be previewed"
    assert "verify" not in out and "next_step" not in out
    assert ACCESS_TOKEN not in tool_text(result)
    assert fake_kite.requests == []  # no live call unless asked


async def test_status_safety_line_follows_the_settings(mcp_factory, kite):
    server, _, _ = mcp_factory(KITE_MCP_TRADING_MODE="live")
    assert "orders can be sent live" in tool_json(await call(server, "session_status"))["safety"]
    server, _, _ = mcp_factory(KITE_MCP_READ_ONLY="true", KITE_MCP_TRADING_MODE="live")
    assert tool_json(await call(server, "session_status"))["safety"] == (
        "trading_mode=live, read_only=true: no tool can change the account"
    )


async def test_status_when_logged_out_says_what_to_do(mcp_factory, kite):
    server, _, rt = mcp_factory()
    rt.settings.token_file.unlink()
    out = tool_json(await call(server, "session_status"))
    assert out["logged_in"] is False and "problem" in out
    assert "call login" in out["next_step"]


async def test_status_after_the_reset_reports_the_expiry(settings_factory, fake_kite, kite):
    server, _ = server_at(settings_factory, fake_kite, FIXED_NOW.replace(day=5, hour=7))  # the stored token expired
    out = tool_json(await call(server, "session_status"))
    assert out["logged_in"] is False and out["expired"] is True and "06:00 IST" in out["problem"]
    assert out["minutes_to_token_reset"] == 1380  # 07:00 to the next 06:00


async def test_verify_asks_kite_once_and_reports_the_user_id(mcp_factory, kite, fake_kite):
    fake_kite.on("GET", "/user/profile", {"user_id": USER_ID, "user_name": "Test Person", "email": "a@example.invalid"})
    server, _, _ = mcp_factory()
    result = await call(server, "session_status", {"verify": True})
    out = tool_json(result)
    assert out["verify"] == {"ok": True, "user_id": USER_ID}
    assert len(fake_kite.requests) == 1 and fake_kite.requests[0].url.path == "/user/profile"
    assert "Test Person" not in tool_text(result) and "example.invalid" not in tool_text(result)


async def test_verify_reports_kites_error_and_the_rejection(mcp_factory, kite, fake_kite):
    fake_kite.on("GET", "/user/profile", status=403, error_type="TokenException", message="Incorrect token")
    server, _, _ = mcp_factory()
    out = tool_json(await call(server, "session_status", {"verify": True}))
    assert out["verify"]["ok"] is False and "Incorrect token" in out["verify"]["error"]
    assert out["logged_in"] is False and "rejected" in out["problem"]  # the rejection is in the same answer
    assert ACCESS_TOKEN not in json.dumps(out)


async def test_verify_without_a_token_does_not_call_kite(mcp_factory, kite, fake_kite):
    server, _, rt = mcp_factory()
    rt.settings.token_file.unlink()
    out = tool_json(await call(server, "session_status", {"verify": True}))
    assert out["verify"]["ok"] is False and out["logged_in"] is False
    assert fake_kite.requests == []


async def test_status_in_external_mode(mcp_factory, kite):
    server, _, rt = mcp_factory(KITE_MCP_AUTH_MODE="external")
    out = tool_json(await call(server, "session_status"))
    assert out["auth_mode"] == "external" and out["login_managed_here"] is False and out["logged_in"] is True
    rt.settings.token_file.unlink()
    out = tool_json(await call(server, "session_status"))
    assert out["logged_in"] is False and "another program" in out["next_step"]


async def test_status_flags_a_static_token_that_overrides_the_file(mcp_factory, kite):
    server, _, _ = mcp_factory(KITE_ACCESS_TOKEN="statictoken0000000000000000000009")
    out = tool_json(await call(server, "session_status"))
    assert out["source"] == "env" and "KITE_ACCESS_TOKEN" in out["note"]


# --- login ------------------------------------------------------------------------------------------------------------


async def test_login_does_not_issue_a_link_while_a_valid_token_exists(mcp_factory, kite, fake_kite):
    server, _, rt = mcp_factory()
    result = await call(server, "login")
    out = tool_json(result)
    assert out["already_logged_in"] is True and out["session"]["logged_in"] is True
    assert "login_url" not in out and "force" in out["message"]
    assert not (rt.settings.state_dir / "login_states.json").exists()
    assert fake_kite.requests == [] and ACCESS_TOKEN not in tool_text(result)


async def test_login_issues_a_link_with_the_warning_and_instructions(mcp_factory, kite):
    server, _, rt = mcp_factory()
    rt.settings.token_file.unlink()  # logged out: no force needed
    out = tool_json(await call(server, "login"))
    assert out["warning"] == WARNING
    parts = urlsplit(out["login_url"])
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == "https://kite.zerodha.com/connect/login"
    assert parts.query.startswith(f"v=3&api_key={API_KEY}&redirect_params=state%3D")
    assert out["expires_at"] == "2026-11-04T11:00:00+05:30"
    assert out["redirect_url"] == "http://127.0.0.1:8765/callback"
    text = " ".join(out["instructions"])
    assert "http://127.0.0.1:8765/callback" in text
    assert "request_token" in text and "complete_login" in text and "address bar" in text
    assert list(stored_states(rt)) == [digest_of(state_of(out["login_url"]))]


async def test_login_force_issues_a_link_even_when_logged_in(mcp_factory, kite):
    server, _, _ = mcp_factory()
    out = tool_json(await call(server, "login", {"force": True}))
    assert out["warning"] == WARNING and "login_url" in out and "already_logged_in" not in out


async def test_login_instructions_follow_the_transport_and_public_url(mcp_factory, kite):
    stdio, _, _ = mcp_factory()
    out = tool_json(await call(stdio, "login", {"force": True}))
    assert "stdio" in " ".join(out["instructions"]) and "login --listen" in " ".join(out["instructions"])

    http, _, _ = mcp_factory(
        KITE_MCP_TRANSPORT="http", KITE_MCP_BEARER_TOKEN=BEARER, KITE_MCP_PUBLIC_URL="https://kite.example.invalid"
    )
    out = tool_json(await call(http, "login", {"force": True}))
    assert out["redirect_url"] == "https://kite.example.invalid/callback"
    assert "handles that redirect itself" in " ".join(out["instructions"])


async def test_login_notes_a_missing_account_pin(mcp_factory, kite):
    server, _, _ = mcp_factory()
    assert any(
        "KITE_MCP_ALLOWED_USER_ID" in n for n in tool_json(await call(server, "login", {"force": True}))["notes"]
    )
    pinned, _, _ = mcp_factory(KITE_MCP_ALLOWED_USER_ID=USER_ID)
    assert "notes" not in tool_json(await call(pinned, "login", {"force": True}))


async def test_login_in_external_mode_only_explains(mcp_factory, kite, fake_kite):
    server, _, rt = mcp_factory(KITE_MCP_AUTH_MODE="external")
    out = tool_json(await call(server, "login", {"force": True}))
    assert out["login_managed_here"] is False and "another program" in out["message"]
    assert out["session"]["logged_in"] is True and "login_url" not in out and "warning" not in out
    assert fake_kite.requests == [] and not (rt.settings.state_dir / "login_states.json").exists()


async def test_login_says_so_when_the_state_folder_cannot_be_written(mcp_factory, kite, monkeypatch):
    def refuse(path, text) -> None:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(loginstate, "write_private_file", refuse)
    server, _, _ = mcp_factory()
    result = await call(server, "login", {"force": True})
    assert result.is_error and "cannot write the login state file" in tool_text(result)


async def test_login_without_the_api_secret_says_so(mcp_factory, kite):
    server, _, _ = mcp_factory(KITE_API_SECRET=None)
    result = await call(server, "login", {"force": True})
    assert result.is_error and "KITE_API_SECRET" in tool_text(result)


async def test_the_whole_login_through_the_tool_and_the_callback_route(mcp_factory, kite):
    server, _, rt = mcp_factory(KITE_MCP_TRANSPORT="http", KITE_MCP_BEARER_TOKEN=BEARER)
    app = build_http_app(server, rt)
    link = tool_json(await call(server, "login", {"force": True}))["login_url"]
    transport = httpx2.ASGITransport(app=app, client=("203.0.113.7", 40000))
    async with httpx2.AsyncClient(transport=transport, base_url="http://127.0.0.1:8765") as http:
        response = await http.get("/callback", params=callback_params(state_of(link)))
    assert response.status_code == 200
    status = tool_json(await call(server, "session_status"))
    assert status["logged_in"] is True
    assert rt.tokens.current().access_token == NEW_TOKEN
    assert tool_json(await call(server, "login"))["already_logged_in"] is True


# --- complete_login ---------------------------------------------------------------------------------------------------


async def test_complete_login_stores_the_token(mcp_factory, kite):
    server, _, rt = mcp_factory()
    result = await call(server, "complete_login", {"request_token": REQUEST_TOKEN})
    out = tool_json(result)
    assert out["logged_in"] is True and out["user_id"] == USER_ID and out["expires_at"] == "2026-11-05T06:00+05:30"
    for secret in (NEW_TOKEN, REQUEST_TOKEN):
        assert secret not in tool_text(result)
    assert rt.tokens.current().access_token == NEW_TOKEN
    assert json.loads(rt.settings.token_file.read_text())["access_token"] == NEW_TOKEN


async def test_complete_login_takes_the_whole_redirected_url(mcp_factory, kite):
    server, _, rt = mcp_factory()
    state = state_of(tool_json(await call(server, "login", {"force": True}))["login_url"])
    query = f"action=login&type=login&status=success&request_token={REQUEST_TOKEN}&state={state}"
    url = f"http://127.0.0.1:8765/callback?{query}"
    out = tool_json(await call(server, "complete_login", {"request_token": url}))
    assert out["logged_in"] is True
    assert stored_states(rt)[digest_of(state)]["status"] == "used"  # the state in the URL was used up


async def test_complete_login_with_an_invalid_state_is_refused(mcp_factory, kite):
    server, _, rt = mcp_factory()
    result = await call(server, "complete_login", {"request_token": REQUEST_TOKEN, "state": STATE})
    assert result.is_error and "unknown, expired, already used or in use" in tool_text(result)
    assert kite.exchanges == [] and rt.tokens.current().access_token == ACCESS_TOKEN
    assert STATE not in tool_text(result) and REQUEST_TOKEN not in tool_text(result)


@pytest.mark.parametrize(
    "pasted",
    ["has space!", "x" * 150, "http://127.0.0.1:8765/callback?status=error&request_token=aaaaaaaaaaaa"],
)
async def test_complete_login_refuses_what_is_not_a_request_token(mcp_factory, kite, pasted):
    server, _, _ = mcp_factory()
    result = await call(server, "complete_login", {"request_token": pasted})
    assert result.is_error
    assert kite.exchanges == []
    assert pasted not in tool_text(result)  # never echoed


async def test_complete_login_reports_kites_refusal(mcp_factory, kite):
    kite.fail_next = True
    server, _, rt = mcp_factory()
    result = await call(server, "complete_login", {"request_token": REQUEST_TOKEN})
    assert result.is_error and "Token is invalid or has expired" in tool_text(result)
    assert REQUEST_TOKEN not in tool_text(result)
    assert rt.tokens.current().access_token == ACCESS_TOKEN
    retry = await call(server, "complete_login", {"request_token": REQUEST_TOKEN})  # a failed exchange costs nothing
    assert not retry.is_error


async def test_complete_login_by_another_account_is_ended_and_refused(mcp_factory, fake_kite):
    kite = KiteSession(fake_kite, user_id="ZZ9999")
    server, _, rt = mcp_factory(KITE_MCP_ALLOWED_USER_ID=USER_ID)
    result = await call(server, "complete_login", {"request_token": REQUEST_TOKEN})
    assert result.is_error and "KITE_MCP_ALLOWED_USER_ID" in tool_text(result) and "ZZ9999" not in tool_text(result)
    assert kite.logouts == [{"api_key": API_KEY, "access_token": NEW_TOKEN}]
    assert rt.tokens.current().access_token == ACCESS_TOKEN


# --- logout -----------------------------------------------------------------------------------------------------------


async def test_logout_is_two_steps_and_removes_the_token(mcp_factory, kite, fake_kite):
    server, _, rt = mcp_factory()
    first = tool_json(await call(server, "logout"))
    assert first["logged_out"] is False and first["confirmation_token"].startswith("confirm_")
    assert str(rt.settings.token_file) in first["would_do"]
    assert fake_kite.requests == [] and rt.settings.token_file.exists()  # nothing happened yet

    second = tool_json(await call(server, "logout", {"confirmation_token": first["confirmation_token"]}))
    assert second == {
        "logged_out": True,
        "kite_session": "ended",
        "token_file_removed": True,
        "next_step": "call login for a new link when a session is needed again",
    }
    assert kite.logouts == [{"api_key": API_KEY, "access_token": ACCESS_TOKEN}]
    assert not rt.settings.token_file.exists()
    assert tool_json(await call(server, "session_status"))["logged_in"] is False

    replay = await call(server, "logout", {"confirmation_token": first["confirmation_token"]})
    assert replay.is_error and "unknown, already used or expired" in tool_text(replay)
    assert len(kite.logouts) == 1


async def test_logout_refuses_a_made_up_or_foreign_confirmation_token(mcp_factory, kite):
    server, _, rt = mcp_factory()
    forged = await call(server, "logout", {"confirmation_token": "confirm_madeup"})
    assert forged.is_error and "call logout without one" in tool_text(forged)
    other_tool, _ = rt.confirmations.issue("place_order", digest_of("anything"))  # valid, but for another call
    foreign = await call(server, "logout", {"confirmation_token": other_tool})
    assert foreign.is_error
    assert kite.logouts == [] and rt.settings.token_file.exists()


async def test_logout_with_nothing_stored_issues_no_confirmation(mcp_factory, kite):
    server, _, rt = mcp_factory()
    rt.settings.token_file.unlink()
    out = tool_json(await call(server, "logout"))
    assert out["logged_out"] is False and "confirmation_token" not in out and "nothing to log out" in out["message"]


async def test_logout_when_kite_cannot_be_told_keeps_the_token(mcp_factory, kite):
    kite.logout_error = (500, "GeneralException")
    server, _, rt = mcp_factory()
    token = tool_json(await call(server, "logout"))["confirmation_token"]
    result = await call(server, "logout", {"confirmation_token": token})
    assert result.is_error and "token file was kept" in tool_text(result)
    assert rt.settings.token_file.exists() and rt.tokens.current().access_token == ACCESS_TOKEN
    assert ACCESS_TOKEN not in tool_text(result)


# --- logs -------------------------------------------------------------------------------------------------------------


async def test_the_session_tools_log_no_secrets(mcp_factory, kite, caplog):
    server, _, rt = mcp_factory()
    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.DEBUG, logger="httpx2")  # in case a CLI run elsewhere raised it
    link = tool_json(await call(server, "login", {"force": True}))["login_url"]
    state = state_of(link)
    await call(server, "complete_login", {"request_token": "wrong-request-token-000000", "state": state})
    await call(server, "complete_login", {"request_token": REQUEST_TOKEN, "state": state})
    token = tool_json(await call(server, "logout"))["confirmation_token"]
    await call(server, "logout", {"confirmation_token": token})
    await call(server, "session_status", {"verify": True})

    secrets = (NEW_TOKEN, ACCESS_TOKEN, REQUEST_TOKEN, "wrong-request-token-000000", state, *FORBIDDEN_IN_LOGS)
    assert any(r.name == "httpx2" and "DELETE" in r.getMessage() for r in caplog.records)
    for record in caplog.records:
        text = f"{record.getMessage()} {record.args!r} {record.exc_text or ''}"
        for secret in secrets:
            assert secret not in text, (record.name, record.levelname)
    events = json.dumps(rt.audit.read())
    for secret in (*secrets, link):
        assert secret not in events
