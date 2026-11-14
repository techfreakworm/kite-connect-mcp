"""Kite's rejections of tokens, kept in the event store's meta table by a server with the order events on, so that a
restart does not forget that Kite refused the stored token (auth/tokenstore.py MemoryRejections otherwise).

Key KEY holds a JSON list of fingerprints (the SHA-256 hex of a token, events/model.py token_fingerprint), oldest
first, at most MAX_KEPT. Never a token.

    StoredRejections(store)     the serving process: read once when made, every change written through. A store that
                                cannot be written is logged, and the mark is still held in memory
    SnapshotRejections(path)    a command beside it (status, login, logout): the marks as the store holds them, read
                                once, read-only, the first time the command asks; a command that never asks (tools)
                                never opens the store, and a store that is not there is never made. Its own changes
                                (a logout, a login) stay in its memory: the service's marks are the service's
"""

from __future__ import annotations

import json
import logging
import re
import threading
from pathlib import Path

from ..auth.tokenstore import MemoryRejections
from .store import EventStore, StoreError

log = logging.getLogger(__name__)

KEY = "rejected_tokens"
MAX_KEPT = 8
_FINGERPRINT_RE = re.compile(r"[0-9a-f]{64}")


def parse(text: str | None) -> list[str]:
    """The fingerprints a meta value holds; anything damaged reads as none."""
    try:
        value = json.loads(text) if text else []
    except (ValueError, RecursionError):
        return []
    if not isinstance(value, list):
        return []
    return [v for v in value if isinstance(v, str) and _FINGERPRINT_RE.fullmatch(v)][-MAX_KEPT:]


class StoredRejections(MemoryRejections):
    """The serving process's marks: the store's, read when made, and every change written through (see the module
    docstring)."""

    def __init__(self, store: EventStore) -> None:
        try:
            held = parse(store.meta(KEY))
        except StoreError as exc:
            log.warning("cannot read the rejected-token marks from the event store (%s)", exc)
            held = []
        super().__init__(held)
        self._store = store

    def add(self, fingerprint: str) -> None:
        if fingerprint in self:
            return
        super().add(fingerprint)
        for old in self.fingerprints()[:-MAX_KEPT]:
            super().discard(old)
        self._write()

    def discard(self, fingerprint: str) -> None:
        if fingerprint not in self:
            return
        super().discard(fingerprint)
        self._write()

    def _write(self) -> None:
        try:
            self._store.set_meta(KEY, json.dumps(self.fingerprints()))
        except StoreError as exc:  # set_meta inside another write() is a StoreError too
            log.warning(
                "could not keep a rejected-token mark in the event store (%s); it is held in memory until the server "
                "stops",
                exc,
            )


class SnapshotRejections(MemoryRejections):
    """A command's copy of the service's marks, read lazily and read-only (see the module docstring)."""

    def __init__(self, path: Path) -> None:
        super().__init__()
        self._path = path
        self._loaded = False
        self._load_lock = threading.Lock()  # a second thread that asks meanwhile waits for the marks

    def _load(self) -> None:
        with self._load_lock:
            if self._loaded:
                return
            self._loaded = True  # once, also when it fails: a store out of reach is not asked again
            try:
                store = EventStore(self._path, readonly=True, create=False)
            except StoreError:
                return  # no store yet, or out of reach: no marks
            try:
                held = parse(store.meta(KEY))
            except StoreError:
                held = []
            finally:
                store.close()
            for fingerprint in held:
                super().add(fingerprint)

    def __contains__(self, fingerprint: object) -> bool:
        self._load()
        return super().__contains__(fingerprint)

    def add(self, fingerprint: str) -> None:
        self._load()
        super().add(fingerprint)

    def discard(self, fingerprint: str) -> None:
        self._load()
        super().discard(fingerprint)

    def fingerprints(self) -> list[str]:
        self._load()
        return super().fingerprints()
