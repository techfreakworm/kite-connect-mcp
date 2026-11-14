"""The login link service: the link reaches the command and nothing else; the state file keeps the last attempt, the
last scheduled slot and the last link for a rejected token, never a link (this part); and the schedule, the links
after a rejected token and the server's lifespan (Task 9)."""

from __future__ import annotations

import asyncio
import json
import logging
import stat

import pytest

from conftest import BEARER, close_runtimes
from events_helpers import Clock
from kite_connect_mcp.auth import reminders
from kite_connect_mcp.auth.reminders import STATE_FILE, LoginLinks
from kite_connect_mcp.errors import ConfigError, KiteMCPError
from kite_connect_mcp.server import build_runtime
from kite_login import state_of
from links_helpers import at, link_command, links_env

HTTP = {"KITE_MCP_TRANSPORT": "http", "KITE_MCP_BEARER_TOKEN": BEARER}
EVENTS = {**HTTP, "KITE_MCP_EVENTS": "true"}
TIMES = {"KITE_MCP_LOGIN_REMINDER_TIMES": "07:30,08:30"}
SLOT = at(2, 7, 30)  # Monday 2 November, 07:30


async def _no_sleep(_: float) -> None:
    return None


@pytest.fixture
def links(settings_factory, fake_kite, tmp_path, monkeypatch):
    """(make, out): make(clock, command=None, **env) builds a runtime whose link service runs a recording link command;
    `out` is where that command writes what it was given (the last run only)."""
    out = tmp_path / "got.json"
    monkeypatch.setenv("LINK_OUT", str(out))
    monkeypatch.setenv("NOT_PASSED", "x")
    default = link_command(tmp_path)
    made = []

    def make(clock, command=None, **env):
        settings = settings_factory(**links_env(command or default, **TIMES, **env))
        rt = build_runtime(settings, transport=fake_kite.transport(), clock=clock, sleep=_no_sleep)
        made.append(rt)
        return rt

    yield make, out
    close_runtimes(made)


def logged_out(rt):
    rt.settings.token_file.unlink()
    return rt


def issued(rt) -> list[str]:
    """The source of every login link issued so far (the audit log): one per link handed to the command."""
    return [e["source"] for e in rt.audit.read() if e["event"] == "login_link_issued"]


async def test_a_link_goes_to_the_command_and_nowhere_else(links, caplog):
    make, out = links
    rt = logged_out(make(Clock(SLOT), **EVENTS))
    caplog.set_level(logging.DEBUG)
    attempt = await rt.links.send("scheduled", slot=SLOT)
    got = json.loads(out.read_text())
    assert set(got) == {"url", "expires_at", "reason"} and got["reason"] == "scheduled"
    assert got["url"].startswith("https://kite.zerodha.com/connect/login?v=3&api_key=")
    assert got["expires_at"] == "2026-11-02T08:30:00+05:30"  # KITE_MCP_LOGIN_LINK_TTL_S, 3600 by default
    names = set(out.with_name("got.json.env").read_text().split())
    assert {"PATH", "LANG", "LINK_OUT"} <= names and "NOT_PASSED" not in names
    assert not [n for n in names if n.startswith("KITE_")]
    assert attempt.outcome == "sent" and rt.links.status()["last"] == {
        "at": "2026-11-02T07:30:00+05:30", "reason": "scheduled", "outcome": "sent", "error": None,
        "link_expires_at": "2026-11-02T08:30:00+05:30",
    }  # fmt: skip
    state = state_of(got["url"])
    db = rt.settings.events_db
    stored = b"".join(p.read_bytes() for p in db.parent.glob(db.name + "*")).decode("utf-8", "replace")  # and its WAL
    places = [
        caplog.text, (rt.settings.state_dir / STATE_FILE).read_text(), json.dumps(rt.audit.read()),
        json.dumps(rt.links.status()), stored,
    ]  # fmt: skip
    for place in places:  # spec 5.13: never in a log, the state file, the audit log, a status or the event store
        assert state not in place and got["url"] not in place
    assert issued(rt) == ["scheduled"]


async def test_a_failing_command_is_recorded_with_a_short_error_and_uses_up_its_slot(links, tmp_path):
    make, _ = links
    rt = logged_out(make(Clock(SLOT), command=link_command(tmp_path, "fails", exit_code=3)))
    attempt = await rt.links.send("scheduled", slot=SLOT)
    assert (attempt.outcome, attempt.error) == ("failed", "exit 3")
    assert rt.links.status()["last_scheduled_for"] == "2026-11-02T07:30+05:30"


async def test_a_command_someone_else_could_change_is_never_run(links):
    make, out = links
    rt = logged_out(make(Clock(at(2, 12, 0))))
    rt.links.command.chmod(0o777)
    attempt = await rt.links.send("requested")
    assert attempt.outcome == "failed" and "writable by group or others" in attempt.error
    assert not out.exists() and not (rt.settings.state_dir / "login_states.json").exists()  # no link was issued


async def test_only_a_scheduled_attempt_records_its_slot(links):
    make, _ = links
    clock = Clock(SLOT)
    rt = logged_out(make(clock))
    await rt.links.send("scheduled", slot=SLOT)
    clock.now = at(2, 7, 45)
    await rt.links.send("requested")
    await rt.links.send("token_rejected")
    path = rt.settings.state_dir / STATE_FILE
    data = json.loads(path.read_text())
    assert data["last_scheduled_for"] == "2026-11-02T07:30:00+05:30"  # requested and token_rejected never touch it
    assert data["token_rejected_link_at"] == "2026-11-02T07:45:00+05:30" and data["last"]["reason"] == "token_rejected"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and "https://" not in path.read_text()
    assert rt.links.handled_slot() == SLOT
    assert make(clock).links.handled_slot() == SLOT  # a new process on the same state folder knows it too
    with pytest.raises(ValueError):
        await rt.links.send("requested", slot=SLOT)  # only a scheduled attempt has a slot


async def test_a_state_file_that_cannot_be_written_still_holds_the_slot(links, monkeypatch, caplog):
    make, _ = links
    rt = logged_out(make(Clock(SLOT)))

    def cannot(path, state):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(reminders, "write_state", cannot)
    with caplog.at_level(logging.WARNING):
        assert (await rt.links.send("scheduled", slot=SLOT)).outcome == "sent"
    assert rt.links.handled_slot() == SLOT and "could not write" in caplog.text


@pytest.mark.parametrize(
    "damaged",
    [
        "not json",
        "[]",
        '{"last": 5, "last_scheduled_for": "x"}',
        # a time without an offset, a moment that is not a string
        '{"last": {"at": "2026-11-02T07:30:00", "reason": "scheduled", "outcome": "sent"}, '
        '"token_rejected_link_at": 7}',
        '{"last": {"at": "2026-11-02T07:30:00+05:30", "reason": "other", "outcome": "sent"}}',  # an unknown reason
    ],
)
async def test_a_damaged_state_file_reads_as_nothing_and_is_replaced(links, damaged):
    make, _ = links
    rt = logged_out(make(Clock(SLOT)))
    path = rt.settings.state_dir / STATE_FILE
    path.write_text(damaged)
    status = rt.links.status()
    assert (status["last"], status["last_scheduled_for"], rt.links.handled_slot()) == (None, None, None)
    assert (await rt.links.send("scheduled", slot=SLOT)).outcome == "sent"
    data = json.loads(path.read_text())
    assert data["last_scheduled_for"] == "2026-11-02T07:30:00+05:30" and data["token_rejected_link_at"] is None
    assert data["last"]["outcome"] == "sent" and stat.S_IMODE(path.stat().st_mode) == 0o600


def test_the_link_service_needs_a_link_command(runtime_factory):
    rt = runtime_factory()
    assert rt.links is None  # without KITE_MCP_LOGIN_LINK_COMMAND there is no link service
    with pytest.raises(ConfigError, match="KITE_MCP_LOGIN_LINK_COMMAND"):
        LoginLinks(rt.settings, rt.login)


def test_the_status_of_a_process_that_never_sends(links):
    make, out = links
    rt = make(Clock(SLOT))
    assert rt.links.status() == {
        "times": ["07:30", "08:30"], "days": [0, 1, 2, 3, 4], "next_at": "2026-11-02T08:30+05:30",
        "last_scheduled_for": None, "last": None,
    }  # fmt: skip
    assert rt.links.handled_slot() is None
    assert not out.exists() and not (rt.settings.state_dir / STATE_FILE).exists()  # reading the status writes nothing


async def test_no_link_while_forcing_is_off_and_a_token_is_valid(links):
    make, out = links
    rt = make(Clock(SLOT), KITE_MCP_LOGIN_ALLOW_FORCE="false")
    with pytest.raises(ConfigError, match="KITE_MCP_LOGIN_ALLOW_FORCE"):
        await rt.links.send("scheduled", slot=SLOT)
    assert (
        not out.exists() and not (rt.settings.state_dir / STATE_FILE).exists()
    )  # nothing recorded: the caller decides
    assert rt.links.handled_slot() is None and issued(rt) == []
    assert [e["source"] for e in rt.audit.read() if e["event"] == "login_link_refused"] == ["scheduled"]


async def test_a_link_that_cannot_be_issued_is_a_failed_attempt_with_a_clipped_error(links, monkeypatch):
    make, out = links
    rt = logged_out(make(Clock(SLOT)))

    async def broken(**_):
        raise KiteMCPError("x" * 300)

    monkeypatch.setattr(rt.login, "issue_link", broken)
    attempt = await rt.links.send("scheduled", slot=SLOT)
    assert attempt.outcome == "failed" and attempt.error == "x" * 197 + "..."
    assert not out.exists() and rt.links.handled_slot() == SLOT


async def test_one_attempt_at_a_time(links, monkeypatch):
    make, _ = links
    rt = logged_out(make(Clock(SLOT)))
    running, most = 0, 0

    async def run(*_, **__):
        nonlocal running, most
        running += 1
        most = max(most, running)
        await asyncio.sleep(0.01)
        running -= 1
        return None

    monkeypatch.setattr(reminders, "run_link_command", run)
    attempts = await asyncio.gather(rt.links.send("requested"), rt.links.send("requested"))
    assert most == 1 and [a.outcome for a in attempts] == ["sent", "sent"] and issued(rt) == ["tool", "tool"]
