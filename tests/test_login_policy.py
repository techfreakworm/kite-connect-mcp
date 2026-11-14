"""The login policy switches: logout off (KITE_MCP_ALLOW_LOGOUT) and forcing a new login off
(KITE_MCP_LOGIN_ALLOW_FORCE). Both default to true, which changes nothing."""

from __future__ import annotations

import json
from pathlib import Path

import httpx2
import pytest
from mcp import Client

from conftest import ACCESS_TOKEN, API_KEY, FIXED_NOW, tool_json, tool_text
from kite_connect_mcp import server as server_module
from kite_connect_mcp.auth.login import cli_login, cli_logout
from kite_connect_mcp.cli import main
from kite_connect_mcp.config import unknown_variables
from kite_connect_mcp.errors import ConfigError
from kite_connect_mcp.safety.audit import AuditLog
from kite_connect_mcp.server import build_runtime, build_server
from kite_login import NEW_TOKEN, REQUEST_TOKEN, USER_ID, KiteSession

LOGOUT_OFF = {"KITE_MCP_ALLOW_LOGOUT": "false"}
FORCE_OFF = {"KITE_MCP_LOGIN_ALLOW_FORCE": "false"}
REFUSED_BY = {
    source: {"event": "logout_refused", "tool": "logout", "source": source, "reason": "logout_off"}
    for source in ("tool", "cli")
}


@pytest.fixture
def kite(fake_kite) -> KiteSession:
    return KiteSession(fake_kite)


def test_the_switches_default_to_true_and_are_known(settings_factory):
    s = settings_factory()
    assert s.allow_logout is True
    assert unknown_variables({"KITE_MCP_ALLOW_LOGOUT": "false"}) == []
    assert settings_factory(**LOGOUT_OFF).allow_logout is False
    with pytest.raises(ConfigError, match="KITE_MCP_ALLOW_LOGOUT"):
        settings_factory(KITE_MCP_ALLOW_LOGOUT="sometimes")


def test_logout_off_hides_the_tool_and_says_why(mcp_factory):
    _, reg, _ = mcp_factory(**LOGOUT_OFF)
    names = {info.name for info in reg.registered}
    assert "logout" not in names and {"session_status", "login", "complete_login"} <= names
    assert reg.skipped["logout"] == "KITE_MCP_ALLOW_LOGOUT=false"


def test_logout_off_names_the_general_reason_first(mcp_factory):
    _, reg, _ = mcp_factory(KITE_MCP_TOOLSETS="account", **LOGOUT_OFF)
    assert reg.skipped["logout"] == reg.skipped["complete_login"] == "toolset session is not in KITE_MCP_TOOLSETS"
    _, reg, _ = mcp_factory(KITE_MCP_EXCLUDED_TOOLS="logout", **LOGOUT_OFF)
    assert reg.skipped["logout"] == "listed in KITE_MCP_EXCLUDED_TOOLS"


async def test_logout_off_a_client_can_neither_list_nor_call_logout(mcp_factory, kite, fake_kite):
    server, _, rt = mcp_factory(**LOGOUT_OFF)
    assert len(rt.extras["http_routes"]) == 1  # the login callback route stays
    async with Client(server) as client:
        assert "logout" not in {t.name for t in (await client.list_tools()).tools}
        refused = await client.call_tool("logout", {"confirmation_token": "confirm_x"})
    assert refused.is_error
    assert fake_kite.requests == [] and rt.settings.token_file.exists()


def _refusal(line: dict) -> dict:
    return {k: line.get(k) for k in ("event", "tool", "source", "reason")}


async def test_logout_off_the_refusal_is_audited_without_the_token(runtime_factory):
    rt = runtime_factory(**LOGOUT_OFF)
    with pytest.raises(ConfigError):
        await rt.login.logout()
    (line,) = rt.audit.read()
    assert _refusal(line) == REFUSED_BY["tool"]
    assert ACCESS_TOKEN not in json.dumps(line)


async def test_logout_off_the_command_audits_its_refusal_without_the_token(settings_factory, fake_kite):
    # a logout timer left installed is refused every time: each run leaves a trace, with or without --yes
    settings = settings_factory(**LOGOUT_OFF)
    for yes in (True, False):
        assert await cli_logout(settings, yes=yes, transport=fake_kite.transport(), clock=lambda: FIXED_NOW) == 2
    lines = AuditLog(settings.audit_dir, clock=lambda: FIXED_NOW).read()
    assert [_refusal(line) for line in lines] == [REFUSED_BY["cli"]] * 2
    assert ACCESS_TOKEN not in json.dumps(lines) and fake_kite.requests == []


async def test_logout_off_an_unwritable_audit_log_changes_nothing_in_the_command(
    settings_factory, fake_kite, capsys, caplog
):
    settings = settings_factory(**LOGOUT_OFF)
    settings.audit_dir.write_text("not a folder")  # the audit folder cannot be created: OSError
    assert await cli_logout(settings, yes=True, transport=fake_kite.transport(), clock=lambda: FIXED_NOW) == 2
    _, err = capsys.readouterr()
    assert "KITE_MCP_ALLOW_LOGOUT=false" in err and ACCESS_TOKEN not in err
    assert "could not write the audit log" in caplog.text and ACCESS_TOKEN not in caplog.text
    assert settings.token_file.exists() and fake_kite.requests == []


async def test_logout_off_the_manager_refuses_without_calling_kite(runtime_factory, kite):
    rt = runtime_factory(**LOGOUT_OFF)
    before = rt.settings.token_file.read_text()
    with pytest.raises(ConfigError, match="KITE_MCP_ALLOW_LOGOUT=false"):
        await rt.login.logout()
    assert kite.logouts == [] and rt.settings.token_file.read_text() == before
    assert rt.tokens.current().access_token == ACCESS_TOKEN
    assert rt.audit.read()[-1]["event"] == "logout_refused"


async def test_logout_off_the_command_refuses_with_exit_2(settings_factory, fake_kite, kite, capsys):
    settings = settings_factory(**LOGOUT_OFF)
    assert await cli_logout(settings, yes=True, transport=fake_kite.transport(), clock=lambda: FIXED_NOW) == 2
    _, err = capsys.readouterr()
    assert "KITE_MCP_ALLOW_LOGOUT=false" in err
    assert kite.logouts == [] and settings.token_file.exists()
    assert AuditLog(settings.audit_dir, clock=lambda: FIXED_NOW).read()[-1]["event"] == "logout_refused"


def test_logout_off_main_exits_2_with_or_without_yes(cli_env, monkeypatch, capsys):
    monkeypatch.setenv("KITE_MCP_ALLOW_LOGOUT", "false")
    for args in (["logout"], ["logout", "--yes"]):
        assert main(args) == 2
        assert "KITE_MCP_ALLOW_LOGOUT=false" in capsys.readouterr().err
    assert Path(cli_env["KITE_TOKEN_FILE"]).exists()


async def test_external_mode_wins_over_logout_off(mcp_factory, settings_factory, fake_kite, capsys):
    both = {"KITE_MCP_AUTH_MODE": "external", **LOGOUT_OFF}
    _, reg, _ = mcp_factory(**both)
    assert reg.skipped["logout"] == "external auth mode"
    settings = settings_factory(**both)
    assert await cli_logout(settings, yes=True, transport=fake_kite.transport(), clock=lambda: FIXED_NOW) == 2
    _, err = capsys.readouterr()
    assert "KITE_MCP_AUTH_MODE=external" in err and "KITE_MCP_ALLOW_LOGOUT" not in err
    assert AuditLog(settings.audit_dir, clock=lambda: FIXED_NOW).read() == []  # not a logout-off refusal
    assert fake_kite.requests == [] and settings.token_file.exists()


async def test_logout_off_still_ends_a_refused_accounts_fresh_session(runtime_factory, fake_kite):
    kite = KiteSession(fake_kite, user_id="ZZ9999")
    rt = runtime_factory(KITE_MCP_ALLOWED_USER_ID=USER_ID, **LOGOUT_OFF)
    before = rt.settings.token_file.read_text()
    assert (await rt.login.complete(REQUEST_TOKEN)).reason == "account_mismatch"
    assert kite.logouts == [{"api_key": API_KEY, "access_token": NEW_TOKEN}]  # that session's token, not the stored one
    assert rt.settings.token_file.read_text() == before and rt.tokens.current().access_token == ACCESS_TOKEN


# --- forcing a new login off (KITE_MCP_LOGIN_ALLOW_FORCE) and `login --json` ----------------------------------------


async def call(server, name, args=None):
    async with Client(server) as client:
        return await client.call_tool(name, args or {})


def test_forcing_defaults_to_on(settings_factory):
    assert settings_factory().login_allow_force is True
    assert settings_factory(**FORCE_OFF).login_allow_force is False
    assert unknown_variables({"KITE_MCP_LOGIN_ALLOW_FORCE": "false"}) == []
    with pytest.raises(ConfigError, match="KITE_MCP_LOGIN_ALLOW_FORCE"):
        settings_factory(KITE_MCP_LOGIN_ALLOW_FORCE="sometimes")


async def test_forcing_off_refuses_a_forced_link_while_the_token_is_valid(mcp_factory, kite, fake_kite):
    server, _, rt = mcp_factory(**FORCE_OFF)
    result = await call(server, "login", {"force": True})
    text = tool_text(result)
    assert result.is_error and "KITE_MCP_LOGIN_ALLOW_FORCE=false" in text and "until 2026-11-05 06:00 IST" in text
    assert "session_status with verify=true asks Kite whether it still accepts the token" in text
    last = rt.audit.read()[-1]  # the manager's own refusal: an agent's forced attempt leaves a trace
    assert (last["event"], last["source"], last["reason"]) == ("login_link_refused", "tool", "token_valid")
    assert not (rt.settings.state_dir / "login_states.json").exists()
    assert fake_kite.requests == []  # the tool never asks Kite by itself: session_status(verify=true) does
    answer = tool_json(await call(server, "login"))
    assert answer["already_logged_in"] is True  # without force: as before
    assert "KITE_MCP_LOGIN_ALLOW_FORCE=false" in answer["message"] and "force=true" not in answer["message"]


async def test_the_manager_itself_refuses_and_audits_it(runtime_factory, kite, fake_kite):
    rt = runtime_factory(**FORCE_OFF)
    with pytest.raises(ConfigError, match="KITE_MCP_LOGIN_ALLOW_FORCE=false"):
        await rt.login.issue_link()
    last = rt.audit.read()[-1]
    assert (last["event"], last["reason"]) == ("login_link_refused", "token_valid")
    assert last["source"] == "tool" and ACCESS_TOKEN not in json.dumps(last)
    assert not (rt.settings.state_dir / "login_states.json").exists()
    assert fake_kite.requests == []  # without verify_with_kite, Kite is not asked


async def test_an_agent_gets_a_link_once_session_status_verify_finds_the_token_rejected(mcp_factory, kite, fake_kite):
    kite_rejects_the_token(fake_kite)  # the way out the refusal names for an agent
    server, _, _ = mcp_factory(**FORCE_OFF)
    assert tool_json(await call(server, "session_status", {"verify": True}))["logged_in"] is False
    assert "login_url" in tool_json(await call(server, "login"))


STATIC_TOKEN = "statictoken000000000000000000001"


@pytest.mark.parametrize(
    ("env", "until"),
    [({}, "exists until 2026-11-05 06:00 IST and"), ({"KITE_ACCESS_TOKEN": STATIC_TOKEN}, "exists and")],
)
async def test_the_refusal_names_the_expiry_when_it_is_known(runtime_factory, env, until):
    rt = runtime_factory(**FORCE_OFF, **env)  # a static KITE_ACCESS_TOKEN has no known expiry
    with pytest.raises(ConfigError) as refused:
        await rt.login.issue_link()
    message = str(refused.value)
    assert f"a valid Kite token {until} forcing a new login is off" in message
    assert ACCESS_TOKEN not in message and STATIC_TOKEN not in message


@pytest.mark.parametrize("why", ["expired", "rejected", "missing"])
async def test_forcing_off_issues_a_link_once_the_token_is_not_valid(settings_factory, fake_kite, kite, why):
    moment = FIXED_NOW.replace(day=5, hour=7) if why == "expired" else FIXED_NOW  # past the 06:00 reset
    rt = build_runtime(settings_factory(**FORCE_OFF), transport=fake_kite.transport(), clock=lambda: moment)
    try:
        if why == "rejected":
            rt.tokens.reject(ACCESS_TOKEN)
        elif why == "missing":
            rt.settings.token_file.unlink()
        server, _ = build_server(rt)
        assert "login_url" in tool_json(await call(server, "login"))
    finally:
        await rt.aclose()


async def test_with_forcing_on_a_forced_link_is_issued_as_before(mcp_factory, kite):
    server, _, _ = mcp_factory()
    assert "force=true" in tool_json(await call(server, "login"))["message"]
    assert "login_url" in tool_json(await call(server, "login", {"force": True}))


PROFILE = ("GET", "/user/profile")
ASKING = "asking Kite whether the stored token is still valid"


def kite_calls(fake_kite) -> list[tuple[str, str]]:
    return [(r.method, r.url.path) for r in fake_kite.requests]


def kite_rejects_the_token(fake_kite) -> None:
    fake_kite.on("GET", "/user/profile", status=403, error_type="TokenException", message="Invalid token")


def last_audit_line(settings) -> dict:
    return AuditLog(settings.audit_dir, clock=lambda: FIXED_NOW).read()[-1]


async def command_login(settings, fake_kite, **options) -> int:
    return await cli_login(
        settings, request_token=None, listen=False, transport=fake_kite.transport(), clock=lambda: FIXED_NOW, **options
    )


def test_the_login_command_refuses_with_forcing_off_and_a_valid_token(cli_env, monkeypatch, capsys, fake_kite):
    real_build = server_module.build_runtime
    monkeypatch.setattr(
        server_module,
        "build_runtime",
        lambda settings, **kw: real_build(settings, **{**kw, "transport": fake_kite.transport()}),
    )
    monkeypatch.setenv("KITE_ACCESS_TOKEN", STATIC_TOKEN)  # no expiry: main() runs on the real clock
    monkeypatch.setenv("KITE_MCP_LOGIN_ALLOW_FORCE", "false")
    fake_kite.on("GET", "/user/profile", {"user_id": USER_ID})
    assert main(["login"]) == 2
    out, err = capsys.readouterr()
    assert out == "" and "KITE_MCP_LOGIN_ALLOW_FORCE=false" in err and ASKING in err
    assert kite_calls(fake_kite) == [PROFILE]  # asked about the static token, once
    assert fake_kite.requests[0].headers["Authorization"] == f"token {API_KEY}:{STATIC_TOKEN}"
    assert STATIC_TOKEN not in err and USER_ID not in err


async def test_the_login_command_asks_kite_once_and_refuses_while_kite_accepts_the_token(
    settings_factory, fake_kite, capsys
):
    fake_kite.on("GET", "/user/profile", {"user_id": USER_ID})
    settings = settings_factory(**FORCE_OFF)
    with pytest.raises(ConfigError, match="KITE_MCP_LOGIN_ALLOW_FORCE=false") as refused:  # main() makes it exit 2
        await command_login(settings, fake_kite)
    message = str(refused.value)
    assert "until 2026-11-05 06:00 IST" in message and "Kite was asked just now and still accepts it" in message
    assert "session_status" not in message  # the command asked by itself
    assert kite_calls(fake_kite) == [PROFILE]  # one read-only call, never a session call
    last = last_audit_line(settings)
    assert (last["event"], last["source"], last["reason"]) == ("login_link_refused", "cli", "token_valid")
    out, err = capsys.readouterr()
    assert out == "" and ASKING in err
    assert ACCESS_TOKEN not in message + err + json.dumps(last) and USER_ID not in message + err
    assert not (settings.state_dir / "login_states.json").exists()


async def test_the_login_command_issues_a_link_once_kite_rejects_the_stored_token(settings_factory, fake_kite, capsys):
    kite_rejects_the_token(fake_kite)
    settings = settings_factory(**FORCE_OFF)
    assert await command_login(settings, fake_kite, as_json=True) == 0
    out, err = capsys.readouterr()
    assert json.loads(out)["url"].startswith("https://kite.zerodha.com/connect/login?v=3&api_key=")
    assert ASKING in err and "Kite no longer accepts the stored token" in err
    assert kite_calls(fake_kite) == [PROFILE]
    last = last_audit_line(settings)
    assert (last["event"], last["source"]) == ("login_link_issued", "cli")


async def test_asking_kite_marks_a_rejected_token_so_the_link_is_issued(runtime_factory, fake_kite, capsys):
    kite_rejects_the_token(fake_kite)
    rt = runtime_factory(**FORCE_OFF)
    url, _ = await rt.login.issue_link(source="cli", verify_with_kite=True)
    assert url.startswith("https://kite.zerodha.com/connect/login?v=3&api_key=")
    assert rt.tokens.current() is None and "rejected" in rt.tokens.status()["problem"]
    assert kite_calls(fake_kite) == [PROFILE]


def _unreachable(request: httpx2.Request) -> httpx2.Response:
    raise httpx2.ConnectError("connection refused", request=request)


@pytest.mark.parametrize(
    "answer",
    [{"handler": _unreachable}, {"status": 500, "error_type": "GeneralException"}],
    ids=["network_error", "server_error"],
)
async def test_asking_kite_refuses_when_kite_cannot_tell(runtime_factory, fake_kite, capsys, answer):
    fake_kite.on("GET", "/user/profile", **answer)
    rt = runtime_factory(**FORCE_OFF)
    with pytest.raises(ConfigError, match="KITE_MCP_LOGIN_ALLOW_FORCE=false") as refused:
        await rt.login.issue_link(source="cli", verify_with_kite=True)
    assert "Asking Kite whether it still accepts the token failed" in str(refused.value)
    assert rt.tokens.current() is not None  # not marked rejected
    assert set(kite_calls(fake_kite)) == {PROFILE}  # the client's own retries of that read, nothing else
    last = rt.audit.read()[-1]
    assert (last["event"], last["source"], last["reason"]) == ("login_link_refused", "cli", "token_valid")
    assert not (rt.settings.state_dir / "login_states.json").exists()


@pytest.mark.parametrize("case", ["forcing_on", "no_valid_token"])
async def test_the_login_command_asks_kite_only_when_forcing_off_meets_a_valid_token(
    settings_factory, fake_kite, capsys, case
):
    settings = settings_factory() if case == "forcing_on" else settings_factory(**FORCE_OFF)
    if case == "no_valid_token":
        settings.token_file.unlink()
    assert await command_login(settings, fake_kite) == 0
    assert fake_kite.requests == [] and ASKING not in capsys.readouterr().err


def test_the_login_command_help_says_when_it_exits_2(capsys):
    with pytest.raises(SystemExit):
        main(["login", "--help"])
    text = " ".join(capsys.readouterr().out.split())
    assert "exits 2" in text and "KITE_MCP_LOGIN_ALLOW_FORCE=false" in text and "still accepts" in text


async def test_login_json_prints_the_link_and_its_expiry_as_one_json_line(settings_factory, fake_kite, capsys):
    code = await cli_login(
        settings_factory(), request_token=None, listen=False, as_json=True, transport=fake_kite.transport(),
        clock=lambda: FIXED_NOW,
    )  # fmt: skip
    out, err = capsys.readouterr()
    assert code == 0 and len(out.splitlines()) == 1
    data = json.loads(out)
    assert set(data) == {"url", "expires_at"} and data["expires_at"] == "2026-11-04T11:00:00+05:30"
    assert data["url"].startswith("https://kite.zerodha.com/connect/login?v=3&api_key=")
    assert "WARNING: AI systems are unpredictable" in err  # stderr as without --json


def test_login_json_on_the_command_line(cli_env, capsys):
    assert main(["login", "--json"]) == 0
    out, _ = capsys.readouterr()
    assert set(json.loads(out)) == {"url", "expires_at"} and len(out.splitlines()) == 1


def test_login_json_cannot_be_combined_with_a_request_token(cli_env, capsys):
    with pytest.raises(SystemExit) as stopped:
        main(["login", "--json", "--request-token", REQUEST_TOKEN])
    assert stopped.value.code == 2
    assert "not allowed with argument" in capsys.readouterr().err  # the exclusive group, not an unknown --json
