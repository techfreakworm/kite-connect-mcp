"""Managed-mode login and logout (placeholder: filled in by its implementation)."""

from __future__ import annotations

from ..config import Settings


class LoginManager:  # pragma: no cover - replaced by the real implementation
    pass


async def cli_login(settings: Settings, *, request_token: str | None, listen: bool) -> int:  # pragma: no cover
    raise NotImplementedError


async def cli_logout(settings: Settings, *, yes: bool) -> int:  # pragma: no cover
    raise NotImplementedError
