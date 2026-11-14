"""The login policy switches: logout off (KITE_MCP_ALLOW_LOGOUT) and forcing a new login off
(KITE_MCP_LOGIN_ALLOW_FORCE). Both default to true, which changes nothing."""

from __future__ import annotations

import json

import pytest
from mcp import Client

from conftest import ACCESS_TOKEN, API_KEY
from kite_connect_mcp.auth.login import cli_logout
from kite_connect_mcp.cli import main
from kite_connect_mcp.config import unknown_variables
from kite_connect_mcp.errors import ConfigError
from kite_login import NEW_TOKEN, REQUEST_TOKEN, USER_ID, KiteSession

LOGOUT_OFF = {"KITE_MCP_ALLOW_LOGOUT": "false"}


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


def test_logout_off_hides_the_tool_and_says_why(mcp_factory, kite):
    _, reg, _ = mcp_factory(**LOGOUT_OFF)
    names = {info.name for info in reg.registered}
    assert "logout" not in names and {"session_status", "login", "complete_login"} <= names
    assert reg.skipped["logout"] == "KITE_MCP_ALLOW_LOGOUT=false"


async def test_logout_off_a_client_can_neither_list_nor_call_logout(mcp_factory, kite, fake_kite):
    server, _, rt = mcp_factory(**LOGOUT_OFF)
    assert len(rt.extras["http_routes"]) == 1  # the login callback route stays
    async with Client(server) as client:
        assert "logout" not in {t.name for t in (await client.list_tools()).tools}
        refused = await client.call_tool("logout", {"confirmation_token": "confirm_x"})
    assert refused.is_error
    assert fake_kite.requests == [] and rt.settings.token_file.exists()


async def test_logout_off_the_refusal_is_audited_without_the_token(runtime_factory, kite):
    rt = runtime_factory(**LOGOUT_OFF)
    with pytest.raises(ConfigError):
        await rt.login.logout(source="cli")
    (line,) = rt.audit.read()
    assert {k: line[k] for k in ("event", "tool", "source", "reason")} == {
        "event": "logout_refused",
        "tool": "logout",
        "source": "cli",
        "reason": "logout_off",
    }
    assert ACCESS_TOKEN not in json.dumps(line)


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
    assert await cli_logout(settings, yes=True, transport=fake_kite.transport()) == 2
    _, err = capsys.readouterr()
    assert "KITE_MCP_ALLOW_LOGOUT=false" in err
    assert kite.logouts == [] and settings.token_file.exists()


def test_logout_off_main_exits_2_with_or_without_yes(cli_env, monkeypatch, capsys):
    monkeypatch.setenv("KITE_MCP_ALLOW_LOGOUT", "false")
    for args in (["logout"], ["logout", "--yes"]):
        assert main(args) == 2
        assert "KITE_MCP_ALLOW_LOGOUT=false" in capsys.readouterr().err


async def test_logout_off_still_ends_a_refused_accounts_fresh_session(runtime_factory, fake_kite):
    kite = KiteSession(fake_kite, user_id="ZZ9999")
    rt = runtime_factory(KITE_MCP_ALLOWED_USER_ID=USER_ID, **LOGOUT_OFF)
    before = rt.settings.token_file.read_text()
    assert (await rt.login.complete(REQUEST_TOKEN)).reason == "account_mismatch"
    assert kite.logouts == [{"api_key": API_KEY, "access_token": NEW_TOKEN}]  # that session's token, not the stored one
    assert rt.settings.token_file.read_text() == before and rt.tokens.current().access_token == ACCESS_TOKEN
