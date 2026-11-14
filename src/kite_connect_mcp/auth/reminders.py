"""Scheduled login links: at the times you set, and after Kite rejects the stored token, the server issues a login
link and hands it to one program of yours, the link command, which sends it to you. See docs/EVENTS.md, "Scheduled
login links".

    SkipDates            the skip-dates file (KITE_MCP_LOGIN_REMINDER_SKIP_DATES_FILE), re-read at each use
    allowed_day          a reminder day (KITE_MCP_LOGIN_REMINDER_DAYS) that is not a skip date
    next_scheduled       the next reminder time after a moment
    link_env             the command's environment: PATH, HOME, LANG and KITE_MCP_LOGIN_LINK_ENV_PASS, never KITE_*
    program_problem      why the command cannot be trusted or run (events/trust.py, as for consumer programs)
    run_link_command     run it once with the link on stdin: None when it exited 0, else a short error
    serving_problem      the links need the HTTP transport: checked when `serve` starts (cli.py) and by LoginLinks

The link is a bearer credential for one Kite login until it expires. It is never stored (auth/loginstate.py keeps only
its state's digest), never logged, never put in an event, a status or an error. The command's stdout and stderr go to
/dev/null for the same reason: nothing it prints is kept.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
import os
import re
import signal
import stat
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Literal, get_args

from ..events.trust import check_owned_file
from ..timeutil import IST, to_ist

if TYPE_CHECKING:
    from ..config import Settings

log = logging.getLogger(__name__)

Reason = Literal["scheduled", "token_rejected", "requested"]
REASONS: tuple[Reason, ...] = get_args(Reason)
LOOKAHEAD_DAYS = 31  # next_scheduled looks this far; a longer run of skipped days shows no next time
SKIP_DATES_MAX_BYTES = 64 * 1024  # years of dates: a larger skip-dates file is refused whole
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
# open without waiting: a FIFO or a device named by mistake must not hold the event loop, nor become our terminal
_OPEN_FLAGS = os.O_RDONLY | os.O_NONBLOCK | os.O_NOCTTY | os.O_CLOEXEC

Spawn = Callable[..., Awaitable[asyncio.subprocess.Process]]


class SkipDates:
    """The skip-dates file: one YYYY-MM-DD per line; `#` starts a comment, on a line of its own or after a date; blank
    lines are fine; a UTF-8 byte-order mark is too. Re-read at each use, so a file another job rewrites takes effect
    without a restart. A missing or unreadable file, a path that is not a regular file, a file larger than
    SKIP_DATES_MAX_BYTES, or a line that is not a date, is logged as a warning once per distinct problem (a line by
    its number, never its text) and skips nothing: a link on a holiday is better than none on a trading day."""

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._warned: set[str] = set()

    def dates(self) -> frozenset[dt.date]:
        if self.path is None:
            return frozenset()
        text = self._read(self.path)
        if text is None:
            return frozenset()
        found: set[dt.date] = set()
        for number, line in enumerate(text.splitlines(), 1):
            word = line.split("#", 1)[0].strip()
            if not word:
                continue
            try:
                if not _DATE_RE.fullmatch(word):
                    raise ValueError
                found.add(dt.date.fromisoformat(word))
            except ValueError:
                self._warn(
                    f"line {number} of the skip-dates file {self.path} is not a YYYY-MM-DD date: it skips nothing"
                )
        return frozenset(found)

    def _read(self, path: Path) -> str | None:
        """The file's text, or None after a warning. It is opened without waiting and read only when it is a regular
        file of at most SKIP_DATES_MAX_BYTES. A larger one is refused whole rather than read in part: the simpler rule,
        and a file that size is no list of holidays."""
        try:
            fd = os.open(path, _OPEN_FLAGS)
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):  # before fdopen, which refuses a folder in its own words
                    self._warn(f"the skip-dates file {path} is not a regular file: no day is skipped")
                    return None
                with os.fdopen(fd, "rb", closefd=False) as file:
                    data = file.read(SKIP_DATES_MAX_BYTES + 1)
            finally:
                os.close(fd)
            if len(data) > SKIP_DATES_MAX_BYTES:
                self._warn(
                    f"the skip-dates file {path} is larger than {SKIP_DATES_MAX_BYTES // 1024} KiB: no day is skipped"
                )
                return None
            return data.decode("utf-8-sig")
        except (OSError, UnicodeDecodeError) as exc:
            why = exc.strerror if isinstance(exc, OSError) and exc.strerror else type(exc).__name__
            self._warn(f"cannot read the skip-dates file {path} ({why}): no day is skipped")
            return None

    def _warn(self, problem: str) -> None:
        if problem not in self._warned:
            self._warned.add(problem)
            log.warning("login links: %s", problem)


def allowed_day(settings: Settings, day: dt.date, skipped: frozenset[dt.date]) -> bool:
    """Whether `day` may have scheduled links: one of KITE_MCP_LOGIN_REMINDER_DAYS, and not in `skipped`."""
    return day.weekday() in settings.login_reminder_days and day not in skipped


def next_scheduled(settings: Settings, now: dt.datetime, skip: SkipDates) -> dt.datetime | None:
    """The first reminder time strictly after `now` on an allowed day, within LOOKAHEAD_DAYS; None without times."""
    if not settings.login_reminder_times:
        return None
    local = to_ist(now)
    skipped = skip.dates()
    for offset in range(LOOKAHEAD_DAYS + 1):
        day = local.date() + dt.timedelta(days=offset)
        if not allowed_day(settings, day, skipped):
            continue
        for at in settings.login_reminder_times:
            moment = dt.datetime.combine(day, at, tzinfo=IST)
            if moment > local:
                return moment
    return None


def link_env(names: tuple[str, ...], environ: Mapping[str, str]) -> dict[str, str]:
    """PATH, HOME (when set), LANG and the named variables that are set; never a KITE_* name."""
    env = {"PATH": environ.get("PATH", os.defpath), "LANG": environ.get("LANG", "C.UTF-8")}
    if "HOME" in environ:
        env["HOME"] = environ["HOME"]
    for name in names:
        if not name.upper().startswith("KITE_") and name in environ:
            env[name] = environ[name]
    return env


def program_problem(program: Path) -> str | None:
    """Why `program` cannot be the link command, or None: it must be an absolute path to an executable regular file
    that only this user or root can change, reached through folders and links only they can change."""
    if not program.is_absolute():
        return f"{program} is not an absolute path"
    try:
        runnable = program.is_file() and os.access(program, os.X_OK)
    except OSError as exc:  # pathlib passes on all but "not found": a folder we cannot search, a name too long
        return f"cannot check the login link command {program}: {exc.strerror or type(exc).__name__}"
    if not runnable:
        return f"{program} is not an executable file"
    return check_owned_file(program, "the login link command")


def serving_problem(settings: Settings) -> str | None:
    """Why this server cannot send login links, or None. A server on stdio has no login callback, so a link it sent
    could not complete a login here."""
    if (settings.login_reminder_times or settings.login_link_command is not None) and settings.transport != "http":
        return (
            "KITE_MCP_LOGIN_REMINDER_TIMES and KITE_MCP_LOGIN_LINK_COMMAND need the HTTP transport (serve --transport "
            "http): the server that sends a link must also serve the login callback"
        )
    return None


def _signal_name(number: int) -> str:
    try:
        return signal.Signals(number).name
    except ValueError:
        return f"signal {number}"


def _kill(proc: asyncio.subprocess.Process) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)  # the command and whatever it started: a session of its own


async def _feed_and_wait(proc: asyncio.subprocess.Process, payload: bytes) -> None:
    """Write the payload, close stdin and wait for the command to exit. A command that exits without reading it is
    not an error here: its exit code tells how it went."""
    assert proc.stdin is not None  # spawned with stdin as a pipe
    try:
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            proc.stdin.write(payload)
            await proc.stdin.drain()
    finally:
        proc.stdin.close()
    await proc.wait()


async def run_link_command(
    program: Path, payload: bytes, *, env: Mapping[str, str], timeout_s: float, spawn: Spawn | None = None
) -> str | None:
    """Run the link command once with `payload` on stdin. None when it exited 0; otherwise a short error that never
    holds the payload: "exit 3", "killed by SIGTERM", "timed out after 30 s", "cannot start the command: ..."."""
    start = spawn or asyncio.create_subprocess_exec
    try:
        proc = await start(
            str(program),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            env=dict(env),
            start_new_session=True,
        )
    except OSError as exc:
        return f"cannot start the command: {exc.strerror or type(exc).__name__}"
    try:
        await asyncio.wait_for(_feed_and_wait(proc, payload), timeout_s)
    except TimeoutError:
        _kill(proc)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), 5)
        return f"timed out after {timeout_s:g} s"
    except BaseException:  # cancelled as the server stops: never leave the command running
        _kill(proc)
        raise
    code = proc.returncode
    if code == 0:
        return None
    if code is not None and code < 0:
        return f"killed by {_signal_name(-code)}"
    return f"exit {code}"
