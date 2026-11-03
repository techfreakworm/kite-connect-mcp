"""One-time login states: which Kite login links this server issued and which of them may still be used.

A login link carries a random `state`, which Kite hands back on the redirect. The callback accepts a request token only
together with a state issued here that is still open, so a request token planted by someone else (login CSRF) is
refused. Only the SHA-256 digest of a state is stored: the file cannot be used to complete a login.

    <state_dir>/login_states.json     {"version": 1, "states": {"<sha256 hex of the state>": {...}}}      mode 600
    <state_dir>/.login_states.lock    the flock target (the data file is replaced by rename, so it cannot be locked)

    entry   issued_at, expires_at       ISO 8601 with offset
            status                      "pending" (usable) or "used" (committed: a token was stored)
            reservation {id, until}     present while one exchange holds the state; it lapses at `until`, so a crashed
                                        exchange does not burn the link
            used_at                     when it was committed

Life of a state: issue -> reserve (one exchange at a time) -> commit when the token is on disk (which also closes every
other pending state), or release when the exchange failed (the same link can then be tried again).

Every operation runs under a threading lock (this process) and an fcntl flock (other processes: the CLI login and a
running server share the file), reads the file afresh and rewrites it atomically.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import json
import logging
import os
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from secrets import token_hex
from typing import Any

from ..errors import KiteMCPError
from ..timeutil import to_ist
from .tokenstore import write_private_file

try:  # POSIX only; where it is missing the in-process lock is all there is
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

MAX_STATES = 32  # entries kept; the oldest are dropped first
RESERVATION_TTL_S = 120  # an exchange has the state this long (Kite calls time out after 30 s)
LOCK_TIMEOUT_S = 5.0  # how long to wait for another process holding the file lock
_VERSION = 1


class StateStoreError(KiteMCPError):
    """The login state file could not be locked, read or written."""


@dataclass(frozen=True)
class Reservation:
    """The right to run one token exchange for one state, until committed, released or lapsed."""

    digest: str
    reservation_id: str = field(repr=False)


def digest_of(state: str) -> str:
    return hashlib.sha256(state.encode("utf-8")).hexdigest()


def _when(entry: dict[str, Any], key: str) -> dt.datetime | None:
    value = entry.get(key)
    if not isinstance(value, str):
        return None
    try:
        return to_ist(dt.datetime.fromisoformat(value))
    except ValueError:
        return None


def _stamp(moment: dt.datetime) -> str:
    return to_ist(moment).isoformat()  # full precision: "newest" must mean newest even within one second


_LONG_AGO = dt.datetime(1970, 1, 1, tzinfo=dt.UTC)


def _prune(entries: dict[str, dict[str, Any]], now: dt.datetime) -> None:
    """Drop expired entries (and ones whose expiry is unreadable), then the oldest beyond MAX_STATES."""
    for digest in [d for d, e in entries.items() if (_when(e, "expires_at") or now) <= now]:
        del entries[digest]
    if len(entries) > MAX_STATES:
        oldest_first = sorted(entries, key=lambda d: (_when(entries[d], "issued_at") or _LONG_AGO, d))
        for digest in oldest_first[: len(entries) - MAX_STATES]:
            del entries[digest]


class _Transaction:
    def __init__(self, entries: dict[str, dict[str, Any]]):
        self.entries = entries
        self.dirty = False


class StateStore:
    """The file of issued login states. Cheap to construct; nothing is touched until the first operation."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock_path = self.path.with_name(f".{self.path.stem}.lock")
        self._thread_lock = threading.Lock()

    # --- operations -------------------------------------------------------------------------------------------------

    def issue(self, state: str, now: dt.datetime, expires_at: dt.datetime) -> None:
        """Record a new state (by digest) that is usable until `expires_at`."""
        with self._transaction() as txn:
            txn.entries[digest_of(state)] = {
                "issued_at": _stamp(now),
                "expires_at": _stamp(expires_at),
                "status": "pending",
            }
            _prune(txn.entries, to_ist(now))
            txn.dirty = True

    def reserve(self, state: str, now: dt.datetime) -> Reservation | None:
        """Take `state` for one exchange. None if it is unknown, expired, used, or reserved by another exchange.
        An unknown state writes nothing."""
        digest = digest_of(state)
        now = to_ist(now)
        with self._transaction() as txn:
            entry = txn.entries.get(digest)
            if entry is None or entry.get("status") != "pending":
                return None
            expires_at = _when(entry, "expires_at")
            if expires_at is None or expires_at <= now:
                return None
            held = entry.get("reservation")
            if isinstance(held, dict) and (_when(held, "until") or now) > now:
                return None
            reservation = Reservation(digest, token_hex(16))
            entry["reservation"] = {
                "id": reservation.reservation_id,
                "until": _stamp(now + dt.timedelta(seconds=RESERVATION_TTL_S)),
            }
            txn.dirty = True
            return reservation

    def commit(self, reservation: Reservation | None, now: dt.datetime) -> None:
        """A token is on disk: mark the reserved state used and close every other pending state. With no reservation
        (a login completed by hand without a state) it only closes the pending states."""
        now = to_ist(now)
        with self._transaction() as txn:
            for digest, entry in list(txn.entries.items()):
                if reservation is not None and digest == reservation.digest:
                    entry.pop("reservation", None)
                    entry.update(status="used", used_at=_stamp(now))
                elif entry.get("status") == "pending":
                    del txn.entries[digest]
            _prune(txn.entries, now)
            txn.dirty = True

    def close_pending(self, now: dt.datetime) -> None:
        """Close every pending state (a logout leaves no login link open)."""
        self.commit(None, now)

    def release(self, reservation: Reservation) -> None:
        """The exchange failed: let the same link be tried again (only if this reservation still holds the state)."""
        with self._transaction() as txn:
            entry = txn.entries.get(reservation.digest)
            held = entry.get("reservation") if entry else None
            if entry is not None and isinstance(held, dict) and held.get("id") == reservation.reservation_id:
                del entry["reservation"]
                txn.dirty = True

    # --- the file -----------------------------------------------------------------------------------------------------

    @contextlib.contextmanager
    def _transaction(self) -> Iterator[_Transaction]:
        with self._thread_lock:
            fd = self._acquire_file_lock()
            try:
                txn = _Transaction(self._load())
                yield txn
                if txn.dirty:
                    self._save(txn.entries)
            finally:
                self._release_file_lock(fd)

    def _acquire_file_lock(self) -> int:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(self._lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as exc:
            raise StateStoreError(f"cannot open the login state lock {self._lock_path}: {exc.strerror}") from None
        if fcntl is None:  # pragma: no cover
            return fd
        deadline = time.monotonic() + LOCK_TIMEOUT_S
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    os.close(fd)
                    raise StateStoreError("the login state file is locked by another process") from None
                time.sleep(0.005)
            except OSError as exc:
                os.close(fd)
                raise StateStoreError(f"cannot lock the login state file: {exc.strerror}") from None

    @staticmethod
    def _release_file_lock(fd: int) -> None:
        if fcntl is not None:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except OSError as exc:
            raise StateStoreError(f"cannot read the login state file {self.path}: {exc.strerror}") from None
        try:
            data = json.loads(text)
        except ValueError:
            log.warning("the login state file %s is not valid JSON; starting with no open login links", self.path)
            return {}
        states = data.get("states") if isinstance(data, dict) else None
        if not isinstance(states, dict):
            return {}
        return {k: v for k, v in states.items() if isinstance(k, str) and isinstance(v, dict)}

    def _save(self, entries: dict[str, dict[str, Any]]) -> None:
        text = json.dumps({"version": _VERSION, "states": entries}, indent=1, sort_keys=True) + "\n"
        try:
            write_private_file(self.path, text)
        except OSError as exc:
            raise StateStoreError(f"cannot write the login state file {self.path}: {exc.strerror}") from None
