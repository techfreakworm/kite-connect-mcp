"""The WebSocket ticker (placeholder: filled in by its implementation).

INTERFACE CONTRACT: TickerManager(settings, tokens, *, clock, connect=None) - cheap to construct; connects lazily on
the first subscription. `await stop()` closes everything (called on shutdown).
"""

from __future__ import annotations

from typing import Any


class TickerManager:  # pragma: no cover - replaced by the real implementation
    def __init__(self, settings: Any = None, tokens: Any = None, *, clock: Any = None, connect: Any = None):
        self.settings, self.tokens, self.clock, self.connect = settings, tokens, clock, connect

    async def stop(self) -> None:
        return None
