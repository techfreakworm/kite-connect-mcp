"""Two-step confirmation for live writes.

A dry run of a write, while live trading is enabled and every check passes, issues a confirmation token: random,
single use, valid for KITE_MCP_CONFIRMATION_TTL_S seconds (default 300), and bound to the digest of the exact request
that would be sent. Repeating the call with dry_run=false and that token sends the request; any change to the
request (another price, quantity, symbol...) gives another digest and the token is refused.

Tokens live in memory only: a restart forgets them, which is the safe direction.
"""

from __future__ import annotations

import datetime as dt
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from ..errors import SafetyRefusal
from ..timeutil import now_ist

MAX_OUTSTANDING = 256


@dataclass
class _Pending:
    tool: str
    digest: str
    expires_mono: float
    expires_at: dt.datetime


class ConfirmationStore:
    def __init__(
        self,
        ttl_s: int,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        wall: Callable[[], dt.datetime] = now_ist,
    ):
        self.ttl_s = ttl_s
        self._monotonic = monotonic
        self._wall = wall
        self._pending: dict[str, _Pending] = {}
        self._lock = threading.Lock()

    def issue(self, tool: str, digest: str) -> tuple[str, dt.datetime]:
        token = "confirm_" + secrets.token_urlsafe(18)
        expires_at = self._wall() + dt.timedelta(seconds=self.ttl_s)
        with self._lock:
            self._purge()
            while len(self._pending) >= MAX_OUTSTANDING:
                self._pending.pop(next(iter(self._pending)))
            self._pending[token] = _Pending(tool, digest, self._monotonic() + self.ttl_s, expires_at)
        return token, expires_at

    def consume(self, token: str, tool: str, digest: str) -> None:
        """Use up `token` for this exact request, or raise SafetyRefusal saying why it cannot be used."""
        with self._lock:
            self._purge()
            pending = self._pending.get(token)
            if pending is None:
                raise SafetyRefusal(
                    "the confirmation token is unknown, used or expired",
                    [
                        "confirmation_token is unknown, already used or expired: run the same call with dry_run=true "
                        "to get a new one"
                    ],
                )
            if pending.tool != tool or not secrets.compare_digest(pending.digest, digest):
                raise SafetyRefusal(
                    "the confirmation token was issued for a different request",
                    [
                        "confirmation_token does not match this request: the arguments must be exactly those of "
                        "the dry run that issued it"
                    ],
                )
            del self._pending[token]

    def _purge(self) -> None:
        now = self._monotonic()
        for token in [t for t, p in self._pending.items() if p.expires_mono <= now]:
            del self._pending[token]
