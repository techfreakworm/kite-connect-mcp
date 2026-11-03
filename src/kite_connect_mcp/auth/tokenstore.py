"""The access token: where it comes from, when it expires, and how it is stored.

Sources, in order:

1. `KITE_ACCESS_TOKEN` in the environment: a static token (its expiry is unknown, so Kite's answer decides).
2. The token file (`KITE_TOKEN_FILE`), re-read whenever it changes on disk, so a token refreshed by another process
   (or by this server's own login callback) is picked up without a restart.

File format: a JSON object `{"access_token", "login_date", "logged_in_at", "expires_at"}` (ISO 8601 timestamps with
offsets, `login_date` the IST date). This is the format other tools already write, so their file can be pointed at
directly. A file holding only the bare token is accepted too; its expiry is then the first 06:00 IST after the file's
modification time.

In `external` auth mode the file belongs to someone else: this module only ever reads it. `write` and `remove` refuse,
and so does everything that would end the session (see auth/login.py). In `managed` mode this server owns the file and
writes it atomically with mode 600.

Tokens never appear in a repr, a log line or an error message.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import logging
import os
import re
import stat
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from secrets import token_hex

from ..errors import ConfigError
from ..timeutil import IST, next_token_reset, now_ist, to_ist

log = logging.getLogger(__name__)

_TOKEN_RE = re.compile(r"[A-Za-z0-9._\-]{8,512}")


@dataclass(frozen=True)
class TokenRecord:
    access_token: str = field(repr=False)
    login_date: dt.date | None
    logged_in_at: dt.datetime | None
    expires_at: dt.datetime | None  # None: unknown (an environment token)
    source: str = "file"  # "file", "env"

    def valid_at(self, now: dt.datetime) -> bool:
        return self.expires_at is None or now < self.expires_at

    def to_json(self) -> str:
        data = {
            "access_token": self.access_token,
            "login_date": self.login_date.isoformat() if self.login_date else None,
            "logged_in_at": self.logged_in_at.isoformat(timespec="seconds") if self.logged_in_at else None,
            "expires_at": self.expires_at.isoformat(timespec="seconds") if self.expires_at else None,
        }
        return json.dumps(data, indent=1) + "\n"

    @classmethod
    def new(cls, access_token: str, logged_in_at: dt.datetime) -> TokenRecord:
        local = to_ist(logged_in_at)
        return cls(access_token, local.date(), local, next_token_reset(local), "file")

    def describe(self, now: dt.datetime) -> dict[str, object]:
        """Facts about the token, never the token itself."""
        out: dict[str, object] = {
            "source": self.source,
            "login_date": self.login_date.isoformat() if self.login_date else None,
            "logged_in_at": self.logged_in_at.isoformat(timespec="seconds") if self.logged_in_at else None,
            "expires_at": to_ist(self.expires_at).isoformat(timespec="seconds") if self.expires_at else None,
            "expired": not self.valid_at(now),
        }
        if self.expires_at is not None:
            out["minutes_left"] = max(0, int((self.expires_at - now).total_seconds() // 60))
        return out


def write_private_file(path: Path, text: str) -> None:
    """Write `text` to `path` atomically with mode 600 (temp file in the same folder, fsync, rename). The folder is
    created with mode 700 if missing."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        with contextlib.suppress(OSError):
            dir_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def _parse_time(value: object) -> dt.datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    moment = dt.datetime.fromisoformat(value.strip())
    return moment if moment.tzinfo else moment.replace(tzinfo=IST)


def parse_token_file(text: str, mtime: dt.datetime) -> TokenRecord | None:
    """A TokenRecord from the file's text, or None if it holds nothing usable."""
    stripped = text.strip()
    if not stripped:
        return None
    if stripped.startswith("{"):
        try:
            data = json.loads(stripped)
            token = data.get("access_token")
            if not isinstance(token, str) or not _TOKEN_RE.fullmatch(token.strip()):
                return None
            logged_in_at = _parse_time(data.get("logged_in_at"))
            expires_at = _parse_time(data.get("expires_at"))
            login_date_raw = data.get("login_date")
            login_date = dt.date.fromisoformat(login_date_raw) if isinstance(login_date_raw, str) else None
        except (ValueError, TypeError, AttributeError, RecursionError):
            return None
        if expires_at is None:
            expires_at = next_token_reset(logged_in_at or mtime)
        if logged_in_at is not None and expires_at <= logged_in_at:
            return None
        if login_date is None and logged_in_at is not None:
            login_date = to_ist(logged_in_at).date()
        return TokenRecord(token.strip(), login_date, logged_in_at, expires_at, "file")
    if not _TOKEN_RE.fullmatch(stripped):
        return None
    local = to_ist(mtime)
    return TokenRecord(stripped, local.date(), local, next_token_reset(local), "file")


class TokenFile:
    """The token file. Reads are cached by (mtime, size, inode) so a busy server does not re-read it per request."""

    def __init__(self, path: Path, *, writable: bool):
        self.path = Path(path)
        self.writable = writable
        self._lock = threading.Lock()
        self._cached_key: tuple[int, int, int] | None = None
        self._cached: TokenRecord | None = None

    def read(self) -> TokenRecord | None:
        try:
            st = self.path.stat()
        except FileNotFoundError:
            with self._lock:
                self._cached_key, self._cached = None, None
            return None
        except OSError as exc:
            log.warning("cannot stat the token file %s: %s", self.path, exc.strerror)
            return None
        key = (st.st_mtime_ns, st.st_size, st.st_ino)
        with self._lock:
            if key == self._cached_key:
                return self._cached
        if stat.S_IMODE(st.st_mode) & 0o077:
            log.warning(
                "the token file %s is readable by group/others (mode %o); run chmod 600",
                self.path,
                stat.S_IMODE(st.st_mode),
            )
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError as exc:
            log.warning("cannot read the token file %s: %s", self.path, exc.strerror)
            return None
        record = parse_token_file(text, dt.datetime.fromtimestamp(st.st_mtime, IST))
        if record is None:
            log.warning("the token file %s does not hold a usable token", self.path)
        with self._lock:
            self._cached_key, self._cached = key, record
        return record

    def write(self, record: TokenRecord) -> None:
        if not self.writable:
            raise ConfigError("the token file is managed by another program (KITE_MCP_AUTH_MODE=external); not writing")
        for orphan in self.path.parent.glob(f".{self.path.name}.*.tmp"):
            with contextlib.suppress(OSError):
                orphan.unlink()
        write_private_file(self.path, record.to_json())

    def remove(self) -> bool:
        if not self.writable:
            raise ConfigError(
                "the token file is managed by another program (KITE_MCP_AUTH_MODE=external); not removing"
            )
        try:
            self.path.unlink()
        except FileNotFoundError:
            return False
        return True


class TokenProvider:
    """The token to use right now, with the knowledge of whether Kite has already rejected it."""

    def __init__(
        self,
        *,
        token_file: TokenFile,
        env_token: str | None = None,
        clock: Callable[[], dt.datetime] = now_ist,
    ):
        self.file = token_file
        self._env = TokenRecord(env_token, None, None, None, "env") if env_token else None
        self._clock = clock
        self._rejected: set[str] = set()  # sha-free: held only in memory, never logged
        self._lock = threading.Lock()

    def current(self) -> TokenRecord | None:
        """The token to send: the environment token if set, else the file's, else None. Expired or rejected tokens are
        not returned (status() says why)."""
        record = self._env or self.file.read()
        if record is None:
            return None
        with self._lock:
            if record.access_token in self._rejected:
                return None
        if not record.valid_at(self._clock()):
            return None
        return record

    def reject(self, access_token: str) -> None:
        """Kite said TokenException for this token: stop using it until a different token appears."""
        with self._lock:
            self._rejected.add(access_token)

    def accept(self, access_token: str) -> None:
        with self._lock:
            self._rejected.discard(access_token)

    def status(self) -> dict[str, object]:
        now = self._clock()
        record = self._env or self.file.read()
        if record is None:
            return {"logged_in": False, "problem": "no access token (log in, or check KITE_TOKEN_FILE)"}
        out: dict[str, object] = {"logged_in": True, **record.describe(now)}
        with self._lock:
            rejected = record.access_token in self._rejected
        if rejected:
            out.update(logged_in=False, problem="Kite rejected this token (TokenException); log in again")
        elif not record.valid_at(now):
            out.update(logged_in=False, problem="the token has expired (tokens reset at 06:00 IST); log in again")
        return out
