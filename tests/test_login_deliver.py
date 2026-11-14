"""login(deliver=true): the link goes through the link command and never into the answer; the same validity and
forcing rules as a plain login. And the login links' status in session_status, `status`, `events status` and
get_event_status."""

from __future__ import annotations

import json

import pytest
from mcp import Client

from conftest import BEARER, tool_json, tool_text
from kite_connect_mcp.cli import main
from kite_connect_mcp.events.store import EventStore
from links_helpers import link_command, links_env

SERVICE = {"KITE_MCP_EVENTS": "true", "KITE_MCP_TRANSPORT": "http", "KITE_MCP_BEARER_TOKEN": BEARER}


async def call(server, name, args=None):
    async with Client(server) as client:
        return await client.call_tool(name, args or {})


@pytest.fixture
def deliver(mcp_factory, tmp_path, monkeypatch):
    out = tmp_path / "got.json"
    monkeypatch.setenv("LINK_OUT", str(out))

    def make(command=None, **env):
        return mcp_factory(**links_env(command or link_command(tmp_path), **env))

    return make, out


def sent_url(out) -> str:
    """The link the recording command was given (it writes its stdin before it exits, whatever its exit code)."""
    url = json.loads(out.read_text())["url"]
    assert url.startswith("https://")
    return url


def issued(rt) -> list[str]:
    return [e["source"] for e in rt.audit.read() if e["event"] == "login_link_issued"]


async def test_deliver_sends_the_link_and_answers_without_it(deliver):
    make, out = deliver
    server, _, rt = make()
    rt.settings.token_file.unlink()
    result = await call(server, "login", {"deliver": True})
    answer = tool_json(result)
    got = json.loads(out.read_text())
    assert got["reason"] == "requested" and answer["delivered"] is True
    assert answer["expires_at"] == got["expires_at"] == "2026-11-04T11:00:00+05:30"
    assert got["url"] not in tool_text(result) and "login_url" not in answer and "https://" not in tool_text(result)
    assert issued(rt) == ["tool"]


async def test_deliver_needs_a_link_command(mcp_factory):
    server, _, rt = mcp_factory()
    rt.settings.token_file.unlink()
    result = await call(server, "login", {"deliver": True})
    assert result.is_error and "KITE_MCP_LOGIN_LINK_COMMAND" in tool_text(result)
    assert issued(rt) == []  # refused before any link


async def test_deliver_follows_the_validity_and_forcing_rules(deliver):
    make, out = deliver
    server, _, _ = make(KITE_MCP_LOGIN_ALLOW_FORCE="false")
    assert tool_json(await call(server, "login", {"deliver": True}))["already_logged_in"] is True
    refused = await call(server, "login", {"deliver": True, "force": True})
    assert refused.is_error and "KITE_MCP_LOGIN_ALLOW_FORCE=false" in tool_text(refused)
    assert not out.exists()


async def test_deliver_with_force_sends_a_link_while_forcing_is_on(deliver):
    make, out = deliver
    server, _, rt = make()  # a valid token is stored
    assert tool_json(await call(server, "login", {"deliver": True}))["already_logged_in"] is True
    assert not out.exists()
    result = await call(server, "login", {"deliver": True, "force": True})
    assert tool_json(result)["delivered"] is True and json.loads(out.read_text())["reason"] == "requested"
    assert sent_url(out) not in tool_text(result) and "https://" not in tool_text(result)
    assert issued(rt) == ["tool"]


async def test_a_failed_delivery_says_so_without_the_link(deliver, tmp_path):
    make, out = deliver
    server, _, rt = make(command=link_command(tmp_path, "fails", exit_code=3))
    rt.settings.token_file.unlink()
    result = await call(server, "login", {"deliver": True})
    answer = tool_json(result)
    assert answer["delivered"] is False and answer["error"] == "exit 3"
    assert answer["expires_at"] == "2026-11-04T11:00:00+05:30"  # the link was issued; the command failed
    assert sent_url(out) not in tool_text(result) and "https://" not in tool_text(result)


async def test_deliver_in_external_mode_only_explains(mcp_factory):
    """External mode cannot have a link command (the settings refuse it); even a link service would not be asked."""

    class NoLinks:
        async def send(self, *args, **kwargs):
            raise AssertionError("no link may be sent in external auth mode")

        def _release_lock(self):  # what the runtimes' teardown calls (conftest.close_runtimes)
            pass

    server, _, rt = mcp_factory(KITE_MCP_AUTH_MODE="external")
    assert rt.links is None
    rt.links = NoLinks()
    answer = tool_json(await call(server, "login", {"deliver": True}))
    assert answer["login_managed_here"] is False and "KITE_MCP_AUTH_MODE=external" in answer["message"]
    assert "delivered" not in answer and issued(rt) == []


async def test_session_status_shows_the_links(deliver):
    make, out = deliver
    server, _, rt = make(KITE_MCP_LOGIN_REMINDER_TIMES="07:30,08:30")
    rt.settings.token_file.unlink()
    await call(server, "login", {"deliver": True})
    result = await call(server, "session_status")
    links = tool_json(result)["login_links"]
    assert links["times"] == ["07:30", "08:30"] and links["next_at"] == "2026-11-05T07:30+05:30"
    assert links["last"]["reason"] == "requested" and links["last"]["outcome"] == "sent"
    assert links["last_scheduled_for"] is None  # a requested link never uses up a slot
    assert sent_url(out) not in tool_text(result) and "https://" not in json.dumps(links)


async def test_without_a_link_command_no_status_changes(mcp_factory):
    server, _, _ = mcp_factory()
    assert "login_links" not in tool_json(await call(server, "session_status"))


def test_the_status_commands_show_the_links(cli_env, monkeypatch, tmp_path, capsys):
    for key, value in {**links_env(link_command(tmp_path)), "KITE_MCP_LOGIN_REMINDER_TIMES": "07:30"}.items():
        monkeypatch.setenv(key, value)
    assert main(["status"]) == 0
    assert json.loads(capsys.readouterr().out)["login_links"]["times"] == ["07:30"]


def test_the_status_command_without_a_link_command_has_no_links(cli_env, capsys):
    assert main(["status"]) == 0
    assert "login_links" not in json.loads(capsys.readouterr().out)


async def test_get_event_status_of_the_serving_process_shows_the_links(deliver):
    make, out = deliver
    server, _, rt = make(KITE_MCP_LOGIN_REMINDER_TIMES="07:30,08:30", **SERVICE)
    rt.settings.token_file.unlink()
    assert tool_json(await call(server, "login", {"deliver": True}))["delivered"] is True
    result = await call(server, "get_event_status")
    links = tool_json(result)["login_links"]
    assert links["times"] == ["07:30", "08:30"] and links["next_at"] == "2026-11-05T07:30+05:30"
    assert links["last"]["reason"] == "requested" and links["last"]["outcome"] == "sent"
    assert sent_url(out) not in tool_text(result) and "https://" not in json.dumps(links)


async def test_get_event_status_without_a_link_command_has_no_links(mcp_factory):
    server, _, _ = mcp_factory(**SERVICE)
    assert "login_links" not in tool_json(await call(server, "get_event_status"))


async def test_a_reader_of_the_store_leaves_the_links_to_the_serving_process(deliver, tmp_path):
    """A reader (KITE_MCP_EVENTS_DB only) shows no poll and no links, even with a link command of its own: they are
    the serving process's."""
    make, _ = deliver
    command = link_command(tmp_path)
    _, _, service_rt = make(command, **SERVICE)
    server, _, rt = make(command, KITE_MCP_EVENTS_DB=str(service_rt.settings.events_db))
    assert rt.events is None and rt.links is not None
    status = tool_json(await call(server, "get_event_status"))
    assert status["poll"] == "see the serving process's status" and "login_links" not in status


def test_events_status_shows_the_links_with_a_link_command(cli_env, monkeypatch, tmp_path, capsys):
    path = tmp_path / "events" / "events.sqlite3"
    path.parent.mkdir(mode=0o700)
    EventStore(path).close()
    monkeypatch.setenv("KITE_MCP_EVENTS_DB", str(path))
    assert main(["events", "status"]) == 0
    assert "login_links" not in json.loads(capsys.readouterr().out)
    for key, value in {**links_env(link_command(tmp_path)), "KITE_MCP_LOGIN_REMINDER_TIMES": "07:30"}.items():
        monkeypatch.setenv(key, value)
    assert main(["events", "status"]) == 0
    links = json.loads(capsys.readouterr().out)["login_links"]
    assert links["times"] == ["07:30"] and links["last"] is None and links["last_scheduled_for"] is None
