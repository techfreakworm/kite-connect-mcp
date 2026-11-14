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
    LoginLinks           the service: send() issues one link and gives it to the command, once, and records the attempt
    describe_links       the `login_links` status: the times, the days, the next time, the last slot and attempt

    <state_dir>/login_links.json    mode 600, never a link: the last attempt (`last`), the last slot handled
                                    (`last_scheduled_for`, so neither the loop nor a restart runs a slot twice) and the
                                    last link after a rejected token (`token_rejected_link_at`: the two hours start
                                    there). A missing or damaged file reads as nothing, a time in it later than now
                                    is ignored, and what a file that cannot be written misses is held in memory.

The link is a bearer credential for one Kite login until it expires. It is never stored (auth/loginstate.py keeps only
its state's digest), never logged, never put in an event, a status or an error. The command's stdout and stderr go to
/dev/null for the same reason: nothing it prints is kept.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import json
import logging
import os
import re
import signal
import stat
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, get_args

from ..errors import ConfigError, KiteMCPError
from ..events.model import MAX_ERROR_CHARS, clean_error_text, clip_words
from ..events.trust import check_owned_file
from ..timeutil import IST, now_ist, to_ist
from .tokenstore import write_private_file

if TYPE_CHECKING:
    from ..config import Settings
    from .login import LoginManager

log = logging.getLogger(__name__)

Reason = Literal["scheduled", "token_rejected", "requested"]
REASONS: tuple[Reason, ...] = get_args(Reason)
LOOKAHEAD_DAYS = 31  # next_scheduled looks this far; a longer run of skipped days shows no next time
SKIP_DATES_MAX_BYTES = 64 * 1024  # years of dates: a larger skip-dates file is refused whole
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
# open without waiting: a FIFO or a device named by mistake must not hold the event loop, nor become our terminal
_OPEN_FLAGS = os.O_RDONLY | os.O_NONBLOCK | os.O_NOCTTY | os.O_CLOEXEC

Spawn = Callable[..., Awaitable[asyncio.subprocess.Process]]

STATE_FILE = "login_links.json"  # under the state folder: the last attempt, the last slot, the last rejected-token link
REJECTED_EVERY = dt.timedelta(hours=2)  # at most one token_rejected link this often
SOURCES: dict[Reason, str] = {"scheduled": "scheduled", "token_rejected": "token_rejected", "requested": "tool"}

Outcome = Literal["sent", "skipped", "failed"]
OUTCOMES: tuple[Outcome, ...] = get_args(Outcome)


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


# --- the service ------------------------------------------------------------------------------------------------------


def _short(error: str) -> str:
    """An attempt's error as it is kept, logged and shown: one clean line, without control characters or links, of
    at most MAX_ERROR_CHARS (events/model.py, as a consumer's error)."""
    return clip_words(clean_error_text(error), MAX_ERROR_CHARS)


def _iso(moment: dt.datetime | None) -> str | None:
    return to_ist(moment).isoformat(timespec="seconds") if moment is not None else None


def _when(value: object) -> dt.datetime | None:
    """A moment from the state file; anything else (missing, damaged, without an offset) is None."""
    if not isinstance(value, str):
        return None
    try:
        moment = dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else None


def _later(*moments: dt.datetime | None) -> dt.datetime | None:
    return max((m for m in moments if m is not None), default=None)


def _past(moment: dt.datetime | None, now: dt.datetime) -> dt.datetime | None:
    """`moment`, or None when it is later than `now`."""
    return moment if moment is not None and moment <= now else None


@dataclass(frozen=True)
class Attempt:
    """One link attempt: when, why, how it went, a short error when it failed and when its link expires (never the
    link itself)."""

    at: dt.datetime
    reason: Reason
    outcome: Outcome
    error: str | None = None
    link_expires_at: dt.datetime | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "at": _iso(self.at),
            "reason": self.reason,
            "outcome": self.outcome,
            "error": self.error,
            "link_expires_at": _iso(self.link_expires_at),
        }

    @classmethod
    def from_dict(cls, data: Any) -> Attempt | None:
        """The attempt the state file holds, or None when it is missing or damaged. Its error is cleaned and clipped
        again, as it is shown: a file edited by hand holds any text."""
        try:
            reason, outcome, at = data["reason"], data["outcome"], _when(data["at"])
            if reason not in REASONS or outcome not in OUTCOMES or at is None:
                return None
            error = data.get("error")
            error = (_short(error) or None) if isinstance(error, str) else None
            return cls(at, reason, outcome, error, _when(data.get("link_expires_at")))
        except (KeyError, TypeError, AttributeError):
            return None


@dataclass(frozen=True)
class LinkState:
    """What the state file holds."""

    last: Attempt | None = None
    token_rejected_link_at: dt.datetime | None = None  # the last token_rejected attempt: the two hours start there
    last_scheduled_for: dt.datetime | None = None  # the last slot handled (sent, skipped or failed)


def read_state(path: Path) -> LinkState:
    """What the state file holds; a missing or damaged file reads as nothing, a damaged field as None."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):  # RecursionError: nested too deep for the parser
        return LinkState()
    if not isinstance(data, dict):
        return LinkState()
    return LinkState(
        Attempt.from_dict(data.get("last")),
        _when(data.get("token_rejected_link_at")),
        _when(data.get("last_scheduled_for")),
    )


def write_state(path: Path, state: LinkState) -> None:
    """Replace the state file (mode 600, atomically). Never a link: an Attempt holds none."""
    data = {
        "last": state.last.as_dict() if state.last else None,
        "token_rejected_link_at": _iso(state.token_rejected_link_at),
        "last_scheduled_for": _iso(state.last_scheduled_for),
    }
    write_private_file(path, json.dumps(data, indent=1) + "\n")


def describe_links(settings: Settings, now: dt.datetime, skip: SkipDates | None = None) -> dict[str, Any]:
    """The `login_links` status: the times, the days, the next time, the last slot handled and the last attempt.
    Never a link."""
    upcoming = next_scheduled(settings, now, skip or SkipDates(settings.login_reminder_skip_dates_file))
    state = read_state(settings.state_dir / STATE_FILE)
    return {
        "times": [t.strftime("%H:%M") for t in settings.login_reminder_times],
        "days": sorted(settings.login_reminder_days),
        "next_at": to_ist(upcoming).isoformat(timespec="minutes") if upcoming else None,
        "last_scheduled_for": (
            to_ist(state.last_scheduled_for).isoformat(timespec="minutes") if state.last_scheduled_for else None
        ),
        "last": state.last.as_dict() if state.last else None,
    }


class LoginLinks:
    """The scheduled links and the links after a rejected token, sent through the link command. See the module
    docstring. Built for every process with a link command (cheap: nothing is read until used), so any process can
    show its status; its loop runs only in the HTTP server."""

    def __init__(
        self,
        settings: Settings,
        login: LoginManager,
        *,
        clock: Callable[[], dt.datetime] = now_ist,
        environ: Mapping[str, str] | None = None,
        spawn: Spawn | None = None,
    ):
        if settings.login_link_command is None:
            raise ConfigError("the login links need KITE_MCP_LOGIN_LINK_COMMAND")
        self.settings = settings
        self.login = login
        self.clock = clock
        self.command: Path = settings.login_link_command
        self.skip = SkipDates(settings.login_reminder_skip_dates_file)
        self._environ = dict(os.environ if environ is None else environ)
        self._spawn = spawn
        self._state_path = settings.state_dir / STATE_FILE
        self._lock = asyncio.Lock()
        # What this process handled, should the state file fail to keep it: the last slot, the last token_rejected
        # attempt. Each is read back as the later of the file's and the memory's (handled_slot, token_rejected_link_at)
        # and written with the next attempt that can be.
        self._last_slot: dt.datetime | None = None
        self._rejected_at: dt.datetime | None = None
        self._warned_future_slot = False

    def valid_token(self) -> bool:
        return self.login.tokens.current() is not None

    def status(self) -> dict[str, Any]:
        return describe_links(self.settings, self.clock(), self.skip)

    def handled_slot(self) -> dt.datetime | None:
        """The latest slot that had an attempt: the later of the state file's and this process's own memory (which
        holds it when the file cannot be written), so no slot runs twice. A slot in the file later than now (a clock
        that ran ahead) is ignored, with a warning once: kept, it would hold back every slot until then. The next
        attempt writes over it."""
        return self._handled_slot(read_state(self._state_path), self.clock())

    def token_rejected_link_at(self) -> dt.datetime | None:
        """When the last token_rejected attempt was, where the two hours start: the later of the state file's and this
        process's own memory. A time in the file later than now is ignored, as a slot is."""
        return self._rejected_link_at(read_state(self._state_path), self.clock())

    def _handled_slot(self, state: LinkState, now: dt.datetime) -> dt.datetime | None:
        stored = _past(state.last_scheduled_for, now)
        if stored is None and state.last_scheduled_for is not None and not self._warned_future_slot:
            self._warned_future_slot = True
            log.warning("the login links' state file holds a slot in the future; ignoring it")
        return _later(stored, self._last_slot)

    def _rejected_link_at(self, state: LinkState, now: dt.datetime) -> dt.datetime | None:
        return _later(_past(state.token_rejected_link_at, now), self._rejected_at)

    async def send(self, reason: Reason, *, slot: dt.datetime | None = None) -> Attempt:
        """Issue a link and give it to the link command, once, and record the attempt: a scheduled one with its slot (a
        naive slot is taken as IST).

        A failed Attempt, logged with its short error, when the command may not be run (program_problem), when
        LoginManager.issue_link raises a KiteMCPError other than ConfigError (the login state file is locked, say) or
        when the command fails (run_link_command: an exit code, a signal, the timeout). A ConfigError from issue_link,
        when no link may be issued now (forcing off with a valid token), propagates and nothing is recorded: the
        caller decides. So does any other exception. ValueError for a scheduled attempt without a slot, or another
        attempt with one."""
        if (slot is None) == (reason == "scheduled"):
            raise ValueError("a scheduled attempt needs its slot, and no other attempt has one")
        slot = to_ist(slot) if slot is not None else None
        async with self._lock:
            now = self.clock()
            problem = await asyncio.to_thread(program_problem, self.command)
            if problem is not None:
                error = _short(problem)
                log.warning("login links: the link command is not run: %s", error)
                return self._record(Attempt(now, reason, "failed", error), slot)
            try:
                url, expires_at = await self.login.issue_link(source=SOURCES[reason])
            except ConfigError:
                raise
            except KiteMCPError as exc:  # raised before the link is built: its text never holds one
                error = _short(str(exc)) or type(exc).__name__
                log.warning("login links: no %s link could be issued (%s)", reason, error)
                return self._record(Attempt(now, reason, "failed", error), slot)
            body = {"url": url, "expires_at": to_ist(expires_at).isoformat(timespec="seconds"), "reason": reason}
            failure = await run_link_command(
                self.command,
                (json.dumps(body) + "\n").encode(),
                env=link_env(self.settings.login_link_env_pass, self._environ),
                timeout_s=self.settings.login_link_timeout_s,
                spawn=self._spawn,
            )
            if failure is None:
                log.info("login links: a %s link was sent through the link command", reason)
            else:
                log.warning("login links: the link command failed for a %s link (%s)", reason, failure)
            outcome: Outcome = "sent" if failure is None else "failed"
            return self._record(Attempt(now, reason, outcome, failure, expires_at), slot)

    def _record(self, attempt: Attempt, slot: dt.datetime | None = None) -> Attempt:
        """Keep `attempt` as the last one in the state file (never a link). A scheduled attempt also keeps its slot, so
        neither the loop nor a restart runs that slot again; a token_rejected attempt starts the two hours. Both are
        held in memory too: a file that cannot be written is logged, and what it missed is written with the next
        attempt that can be (the later of the file's and the memory's, a time in the future dropped)."""
        if slot is not None:
            self._last_slot = _later(self._last_slot, slot)
        if attempt.reason == "token_rejected":
            self._rejected_at = attempt.at
        before, now = read_state(self._state_path), self.clock()
        state = LinkState(attempt, self._rejected_link_at(before, now), self._handled_slot(before, now))
        try:
            write_state(self._state_path, state)
        except OSError as exc:
            log.warning("login links: could not write %s (%s)", self._state_path, exc.strerror or type(exc).__name__)
        return attempt
