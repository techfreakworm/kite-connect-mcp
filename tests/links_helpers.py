"""Helpers for the login-link tests: a link command that records what it was given, in a private folder (the trust
check of events/trust.py refuses a program others could change), the settings that turn the links on, and the means to
see that a command and what it started were killed."""

from __future__ import annotations

import asyncio
import datetime as dt
import os
from collections.abc import Callable
from pathlib import Path

import pytest

from events_helpers import folders
from kite_connect_mcp.timeutil import IST


def link_command(parent: Path, name: str = "send-link", *, exit_code: int = 0, sleep: float = 0) -> Path:
    """A link command (one program, no arguments) in a new mode-700 folder `parent/<name>-bin`: it copies its stdin to
    $LINK_OUT, the names of its environment to $LINK_OUT.env, then sleeps `sleep` seconds and exits `exit_code`."""
    (folder,) = folders(parent, f"{name}-bin")
    path = folder / name
    pause = f"sleep {sleep}\n" if sleep else ""
    path.write_text(f'#!/bin/sh\ncat > "$LINK_OUT"\nenv | cut -d= -f1 > "$LINK_OUT.env"\n{pause}exit {exit_code}\n')
    path.chmod(0o700)
    return path


def links_env(command: Path, **extra: str) -> dict[str, str]:
    """Settings that turn the login links on, with `command` as the link command."""
    return {
        "KITE_MCP_PUBLIC_URL": "https://kite.example.com",
        "KITE_MCP_LOGIN_LINK_COMMAND": str(command),
        "KITE_MCP_LOGIN_LINK_ENV_PASS": "LINK_OUT",
        **extra,
    }


def at(day: int, hour: int, minute: int = 0) -> dt.datetime:
    """A moment of November 2026 in IST: the 2nd is a Monday, the 7th a Saturday."""
    return dt.datetime(2026, 11, day, hour, minute, tzinfo=IST)


def script(parent: Path, name: str, body: str) -> Path:
    """A program `name` in a new mode-700 folder of `parent`, running the shell `body`."""
    (folder,) = folders(parent, f"{name}-bin")
    path = folder / name
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(0o700)
    return path


BACKGROUND = 'sleep 30 &\necho $$ $! > "$PID_OUT"\nwait'  # the shell's pid and its background child's


def ended(pid: int) -> bool:
    """Whether the process `pid` has ended: gone, or a zombie that its new parent has not reaped yet."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] == "Z"
    except FileNotFoundError:  # gone between the two looks, where /proc shows the processes
        return Path("/proc/self").is_dir()


async def until(condition: Callable[[], bool], failure: str) -> None:
    for _ in range(500):
        if condition():
            return
        await asyncio.sleep(0.01)
    pytest.fail(failure)


def pids(path: Path) -> tuple[int, ...]:
    """The pids BACKGROUND wrote to `path`, once the whole line is there."""
    text = path.read_text() if path.exists() else ""
    return tuple(int(n) for n in text.split()) if text.endswith("\n") else ()
