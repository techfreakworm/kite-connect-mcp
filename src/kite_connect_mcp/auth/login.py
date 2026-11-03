"""Managed-mode login and logout (placeholder: filled in by its implementation).

INTERFACE CONTRACT: LoginManager(settings, client, tokens, audit, *, clock) - cheap to construct.
"""

from __future__ import annotations

from typing import Any

from ..config import Settings


class LoginManager:  # pragma: no cover - replaced by the real implementation
    def __init__(
        self, settings: Any = None, client: Any = None, tokens: Any = None, audit: Any = None, *, clock: Any = None
    ):
        self.settings, self.client, self.tokens, self.audit, self.clock = settings, client, tokens, audit, clock


async def cli_login(settings: Settings, *, request_token: str | None, listen: bool) -> int:  # pragma: no cover
    raise NotImplementedError


async def cli_logout(settings: Settings, *, yes: bool) -> int:  # pragma: no cover
    raise NotImplementedError
