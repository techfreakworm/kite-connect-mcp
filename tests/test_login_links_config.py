"""The scheduled login links, part 1: their settings, the skip-dates file, the next time and the link command runner.
Nothing here issues a link."""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import time

import pytest

from events_helpers import folders
from kite_connect_mcp.auth.reminders import (
    SkipDates,
    link_env,
    next_scheduled,
    program_problem,
    run_link_command,
    serving_problem,
)
from kite_connect_mcp.config import unknown_variables
from kite_connect_mcp.errors import ConfigError
from links_helpers import at, link_command

LINKS = {"KITE_MCP_PUBLIC_URL": "https://kite.example.com", "KITE_MCP_LOGIN_LINK_COMMAND": "/usr/local/bin/send-link"}
NAMES = (
    "KITE_MCP_LOGIN_REMINDER_TIMES",
    "KITE_MCP_LOGIN_REMINDER_DAYS",
    "KITE_MCP_LOGIN_REMINDER_SKIP_DATES_FILE",
    "KITE_MCP_LOGIN_LINK_COMMAND",
    "KITE_MCP_LOGIN_LINK_ENV_PASS",
    "KITE_MCP_LOGIN_LINK_TIMEOUT_S",
)


def test_off_by_default_and_known(settings_factory):
    s = settings_factory()
    assert s.login_reminder_times == () and s.login_reminder_days == frozenset(range(5))
    assert s.login_reminder_skip_dates_file is None and s.login_link_command is None
    assert s.login_link_env_pass == () and s.login_link_timeout_s == 30
    assert unknown_variables(dict.fromkeys(NAMES, "x")) == []


def test_the_settings(settings_factory, tmp_path):
    s = settings_factory(
        **LINKS,
        KITE_MCP_LOGIN_REMINDER_TIMES=" 08:30, 7:30,08:30 ",
        KITE_MCP_LOGIN_REMINDER_DAYS="mon,wed",
        KITE_MCP_LOGIN_REMINDER_SKIP_DATES_FILE=str(tmp_path / "skip.txt"),
        KITE_MCP_LOGIN_LINK_ENV_PASS="TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID",
        KITE_MCP_LOGIN_LINK_TIMEOUT_S="45",
    )
    assert s.login_reminder_times == (dt.time(7, 30), dt.time(8, 30))  # sorted, without repeats
    assert s.login_reminder_days == frozenset({0, 2}) and s.login_reminder_skip_dates_file == tmp_path / "skip.txt"
    assert s.login_link_env_pass == ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID") and s.login_link_timeout_s == 45
    assert s.redacted()["login_reminder_times"] == ["07:30", "08:30"]


def test_the_redacted_settings_are_plain_json(settings_factory):
    out = settings_factory(**LINKS, KITE_MCP_LOGIN_REMINDER_TIMES="07:30").redacted()
    assert json.loads(json.dumps(out))["login_link_command"] == "/usr/local/bin/send-link"
    assert out["login_reminder_days"] == [0, 1, 2, 3, 4] and out["login_link_timeout_s"] == 30


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"KITE_MCP_LOGIN_REMINDER_TIMES": "07:30"}, "needs KITE_MCP_LOGIN_LINK_COMMAND"),
        ({**LINKS, "KITE_MCP_AUTH_MODE": "external"}, "KITE_MCP_AUTH_MODE=managed"),
        ({"KITE_MCP_LOGIN_LINK_COMMAND": "/usr/local/bin/send-link"}, "KITE_MCP_PUBLIC_URL"),
        ({**LINKS, "KITE_MCP_LOGIN_LINK_COMMAND": "send-link"}, "absolute path"),
        ({**LINKS, "KITE_MCP_LOGIN_REMINDER_TIMES": "7.30"}, "HH:MM"),
        ({**LINKS, "KITE_MCP_LOGIN_REMINDER_TIMES": "24:00"}, "HH:MM"),
        ({**LINKS, "KITE_MCP_LOGIN_REMINDER_DAYS": "Fri-Mon"}, "KITE_MCP_LOGIN_REMINDER_DAYS"),
        ({**LINKS, "KITE_MCP_LOGIN_LINK_ENV_PASS": "KITE_API_SECRET"}, "must not pass KITE_"),
        ({**LINKS, "KITE_MCP_LOGIN_LINK_ENV_PASS": "BAD-NAME"}, "environment variable names"),
        ({**LINKS, "KITE_MCP_LOGIN_LINK_TIMEOUT_S": "0"}, "KITE_MCP_LOGIN_LINK_TIMEOUT_S"),
        ({**LINKS, "KITE_MCP_LOGIN_LINK_TIMEOUT_S": "601"}, "KITE_MCP_LOGIN_LINK_TIMEOUT_S"),
    ],
)
def test_bad_settings_are_refused(settings_factory, env, message):
    with pytest.raises(ConfigError, match=message):
        settings_factory(**env)


@pytest.mark.parametrize("entry", ["TELEGRAM_CHAT_ID=123456789", "KITE_API_SECRET=abcd1234"])
def test_a_value_given_for_a_name_is_not_echoed(settings_factory, entry):
    """An entry of KITE_MCP_LOGIN_LINK_ENV_PASS that is not a name may be a NAME=value pasted by mistake: the error
    tells which entry, never what it holds."""
    with pytest.raises(ConfigError, match="entry 2 is not a name") as error:
        settings_factory(**LINKS, KITE_MCP_LOGIN_LINK_ENV_PASS=f"LINK_OUT,{entry}")
    assert entry.split("=")[1] not in str(error.value)


def test_the_http_transport_is_checked_where_the_server_starts(settings_factory):
    assert "HTTP transport" in serving_problem(settings_factory(**LINKS))
    assert serving_problem(settings_factory(**LINKS, KITE_MCP_TRANSPORT="http", KITE_MCP_BEARER_TOKEN="b" * 40)) is None
    assert serving_problem(settings_factory()) is None


def test_skip_dates_are_reread_and_problems_logged_once(tmp_path, caplog):
    path = tmp_path / "skip.txt"
    skip = SkipDates(path)
    with caplog.at_level(logging.WARNING):
        assert skip.dates() == frozenset() and skip.dates() == frozenset()  # missing: nothing skipped
        path.write_text("# holidays\n2026-11-09  # a Monday\n\nnot-a-date\n2026-11-10\n")
        assert skip.dates() == {dt.date(2026, 11, 9), dt.date(2026, 11, 10)}  # the bad line skips nothing
        assert skip.dates() == {dt.date(2026, 11, 9), dt.date(2026, 11, 10)}
    warnings = [r.getMessage() for r in caplog.records]
    assert len([w for w in warnings if "cannot read" in w]) == 1
    assert len([w for w in warnings if "line 4" in w]) == 1 and "not-a-date" not in caplog.text


def test_skip_dates_take_only_real_dates(tmp_path, caplog):
    path = tmp_path / "skip.txt"
    path.write_text("2026-02-30\n2026-11-9\n 2026-11-11 \n20261112\n")
    with caplog.at_level(logging.WARNING):
        assert SkipDates(path).dates() == {dt.date(2026, 11, 11)}
    assert [n for n in ("line 1", "line 2", "line 3", "line 4") if n in caplog.text] == ["line 1", "line 2", "line 4"]
    path.write_bytes(b"\xff\xfe2026-11-09\n")
    with caplog.at_level(logging.WARNING):
        assert SkipDates(path).dates() == frozenset()
    assert "cannot read the skip-dates file" in caplog.text and "UnicodeDecodeError" in caplog.text
    assert SkipDates(None).dates() == frozenset()


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (at(2, 5, 0), at(2, 7, 30)),  # Monday before the first time
        (at(2, 7, 30), at(2, 8, 30)),  # strictly after now
        (at(2, 9, 0), at(3, 7, 30)),  # the next day
        (at(6, 9, 0), at(9, 7, 30)),  # Friday after the last time: Monday (the 9th)
        (at(7, 5, 0), at(9, 7, 30)),  # Saturday
    ],
)
def test_the_next_time(settings_factory, now, expected):
    s = settings_factory(**LINKS, KITE_MCP_LOGIN_REMINDER_TIMES="07:30,08:30")
    assert next_scheduled(s, now, SkipDates(None)) == expected


def test_the_next_time_is_in_ist_whatever_the_moment_is_given_in(settings_factory):
    s = settings_factory(**LINKS, KITE_MCP_LOGIN_REMINDER_TIMES="07:30,08:30")
    assert next_scheduled(s, at(2, 7, 30).astimezone(dt.UTC), SkipDates(None)) == at(2, 8, 30)
    assert next_scheduled(s, dt.datetime(2026, 11, 2, 8, 0), SkipDates(None)) == at(2, 8, 30)  # naive: IST


def test_a_skip_date_moves_the_next_time_on(settings_factory, tmp_path):
    path = tmp_path / "skip.txt"
    path.write_text("2026-11-09\n")
    s = settings_factory(
        **LINKS, KITE_MCP_LOGIN_REMINDER_TIMES="07:30", KITE_MCP_LOGIN_REMINDER_SKIP_DATES_FILE=str(path)
    )
    assert next_scheduled(s, at(6, 9, 0), SkipDates(path)) == at(10, 7, 30)
    assert next_scheduled(settings_factory(**LINKS), at(2, 5, 0), SkipDates(None)) is None  # no times


def test_no_next_time_past_the_lookahead(settings_factory, tmp_path):
    """Mondays only, from Monday the 2nd after its time: the 9th, 16th, 23rd and 30th lie within 31 days."""
    path = tmp_path / "skip.txt"
    s = settings_factory(**LINKS, KITE_MCP_LOGIN_REMINDER_TIMES="07:30", KITE_MCP_LOGIN_REMINDER_DAYS="mon")
    path.write_text("2026-11-09\n2026-11-16\n2026-11-23\n")
    assert next_scheduled(s, at(2, 9, 0), SkipDates(path)) == at(30, 7, 30)
    path.write_text("2026-11-09\n2026-11-16\n2026-11-23\n2026-11-30\n")
    assert next_scheduled(s, at(2, 9, 0), SkipDates(path)) is None


def test_the_commands_environment():
    environ = {"PATH": "/bin", "HOME": "/home/x", "LANG": "C.UTF-8", "A": "1", "B": "2", "KITE_API_SECRET": "s"}
    assert link_env(("A", "KITE_API_SECRET", "MISSING"), environ) == {
        "PATH": "/bin",
        "HOME": "/home/x",
        "LANG": "C.UTF-8",
        "A": "1",
    }


def test_the_commands_environment_without_home_or_lang():
    assert link_env(("kite_x",), {"PATH": "/bin", "kite_x": "1"}) == {"PATH": "/bin", "LANG": "C.UTF-8"}


def test_the_program_check(tmp_path):
    command = link_command(tmp_path)
    assert program_problem(command) is None
    command.chmod(0o777)
    assert "writable by group or others" in program_problem(command)
    assert "absolute" in program_problem(command.relative_to(command.anchor))
    assert "not an executable file" in program_problem(tmp_path / "missing")


def test_the_program_must_be_an_executable_file(tmp_path):
    command = link_command(tmp_path)
    command.chmod(0o600)
    assert "not an executable file" in program_problem(command)
    assert "not an executable file" in program_problem(command.parent)  # a folder


async def test_the_runner_gives_the_payload_and_reads_only_the_exit(tmp_path):
    out = tmp_path / "got.json"
    env = {"PATH": os.environ["PATH"], "LINK_OUT": str(out)}
    payload = (
        json.dumps({"url": "https://kite.example.com/x", "expires_at": "e", "reason": "scheduled"}) + "\n"
    ).encode()
    assert await run_link_command(link_command(tmp_path), payload, env=env, timeout_s=10) is None
    assert out.read_bytes() == payload
    assert (
        await run_link_command(link_command(tmp_path, "fails", exit_code=3), payload, env=env, timeout_s=10) == "exit 3"
    )
    assert (await run_link_command(tmp_path / "missing", payload, env=env, timeout_s=10)).startswith(
        "cannot start the command: "
    )


async def test_the_runner_kills_a_command_past_its_timeout(tmp_path):
    env = {"PATH": os.environ["PATH"], "LINK_OUT": str(tmp_path / "got.json")}
    started = time.monotonic()
    error = await run_link_command(link_command(tmp_path, "slow", sleep=30), b"{}\n", env=env, timeout_s=0.3)
    assert error == "timed out after 0.3 s" and time.monotonic() - started < 10


def _script(parent, name, body):
    """A program `name` in a new mode-700 folder of `parent`, running the shell `body`."""
    (folder,) = folders(parent, f"{name}-bin")
    path = folder / name
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(0o700)
    return path


async def test_the_runner_names_the_signal_that_killed_the_command(tmp_path):
    command = _script(tmp_path, "killed", "kill -TERM $$\nexit 0")
    assert (
        await run_link_command(command, b"{}\n", env={"PATH": os.environ["PATH"]}, timeout_s=10) == "killed by SIGTERM"
    )


@pytest.mark.parametrize(("code", "expected"), [(0, None), (4, "exit 4")])
async def test_a_command_that_does_not_read_its_stdin_ends_by_its_exit(tmp_path, code, expected):
    """A payload larger than a pipe holds, to a command that never reads it: the broken pipe is not an error."""
    command = _script(tmp_path, "deaf", f"exit {code}")
    payload = b"x" * (1 << 20)
    assert await run_link_command(command, payload, env={"PATH": os.environ["PATH"]}, timeout_s=10) == expected


async def test_the_runner_closes_stdin_so_a_reader_ends(tmp_path):
    out = tmp_path / "got.json"
    payload = b"y" * (1 << 20) + b"\n"
    started = time.monotonic()
    env = {"PATH": os.environ["PATH"], "LINK_OUT": str(out)}
    assert await run_link_command(link_command(tmp_path), payload, env=env, timeout_s=10) is None
    assert out.read_bytes() == payload and time.monotonic() - started < 5


async def test_the_command_gets_only_its_environment(tmp_path):
    out = tmp_path / "got.json"
    environ = {"PATH": os.environ["PATH"], "LINK_OUT": str(out), "KITE_API_SECRET": "s", "OTHER": "1"}
    env = link_env(("LINK_OUT",), environ)
    assert await run_link_command(link_command(tmp_path), b"{}\n", env=env, timeout_s=10) is None
    names = set(out.with_name("got.json.env").read_text().split())
    assert {"PATH", "LANG", "LINK_OUT"} <= names and not names & {"KITE_API_SECRET", "OTHER"}
