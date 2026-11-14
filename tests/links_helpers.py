"""Helpers for the login-link tests: a link command that records what it was given, in a private folder (the trust
check of events/trust.py refuses a program others could change), and the settings that turn the links on."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

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
