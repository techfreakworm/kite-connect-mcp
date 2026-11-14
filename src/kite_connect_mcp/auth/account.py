"""Which Kite account this server serves: the account pin, given as the user id (KITE_MCP_ALLOWED_USER_ID) or as its
SHA-256 (KITE_MCP_ALLOWED_USER_ID_SHA256), so that the plain id need not be in any file.

Every account check goes through AccountPin.allows: the login exchange (the callback and a pasted request token) and
the postback route. It compares SHA-256 digests with hmac.compare_digest whichever way the pin was given, so the time
it takes says nothing about how much of an id matched.

The digest is SHA-256 over the user id, stripped and upper-cased, as UTF-8, in lower-case hex: a formula other tools
can use to keep a pin of their own (the tests pin it with the placeholder id AB1234).

With the hash pin set, where the server's own words named the account by its user id (the audit log, the session
tools, the status and login commands), they give the first PREFIX_CHARS hex digits of the digest instead (label,
describe). Kite's answers that the read tools pass on (the profile, an order's placed_by) are not changed. The digest
keeps the id out of plain sight, not secret: a user id is short, and its SHA-256 can be found by trying them all.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..config import Settings

PREFIX_CHARS = 12  # how much of the digest names an account in the audit log, tool results and command output


def user_id_digest(user_id: str) -> str:
    """SHA-256 (lower-case hex) of the user id, stripped and upper-cased, as UTF-8."""
    return hashlib.sha256(user_id.strip().upper().encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class AccountPin:
    digest: str | None  # the pinned account's digest; None: no pin, any account is accepted
    by_hash: bool = False  # given as KITE_MCP_ALLOWED_USER_ID_SHA256: the plain id is never written

    @classmethod
    def from_settings(cls, settings: Settings) -> AccountPin:
        if settings.allowed_user_id_sha256:
            return cls(settings.allowed_user_id_sha256, True)
        if settings.allowed_user_id:
            return cls(user_id_digest(settings.allowed_user_id))
        return cls(None)

    @property
    def pinned(self) -> bool:
        return self.digest is not None

    @property
    def setting(self) -> str:
        """The setting that holds the pin, for messages."""
        return "KITE_MCP_ALLOWED_USER_ID_SHA256" if self.by_hash else "KITE_MCP_ALLOWED_USER_ID"

    def allows(self, user_id: object) -> bool:
        """True without a pin. With one: whether `user_id`, as Kite gives it (any case, maybe padded), is the pinned
        account. Anything but a non-empty string is refused."""
        if self.digest is None:
            return True
        if not isinstance(user_id, str) or not user_id.strip():
            return False
        return hmac.compare_digest(user_id_digest(user_id), self.digest)

    def label(self, user_id: str | None) -> dict[str, str | None]:
        """How an account is named in the audit log and in results: {"user_id": ...} as before, or with the hash pin
        {"user_id_sha256_prefix": <the first PREFIX_CHARS hex digits of its digest>}. Something other than a string
        (a Kite answer passed on as it came) gives None there rather than an error."""
        if not self.by_hash:
            return {"user_id": user_id}
        if not isinstance(user_id, str) or not user_id.strip():
            return {"user_id_sha256_prefix": None}
        return {"user_id_sha256_prefix": user_id_digest(user_id)[:PREFIX_CHARS]}

    def describe(self, user_id: str) -> str:
        """The account in a sentence: "AB1234", or with the hash pin "sha256:f08b6edab3b2"."""
        return f"sha256:{user_id_digest(user_id)[:PREFIX_CHARS]}" if self.by_hash else user_id
