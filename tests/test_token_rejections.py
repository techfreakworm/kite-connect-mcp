"""Kite's rejection of a token, kept by fingerprint: in memory by default, in the event store's meta table with the
events on (a restart remembers it), read by the commands beside the service, and cleared by a new token."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from pathlib import Path

import pytest

from conftest import ACCESS_TOKEN, BEARER, FIXED_NOW, write_token_file
from kite_connect_mcp.auth.tokenstore import MemoryRejections, TokenFile, TokenProvider
from kite_connect_mcp.cli import main
from kite_connect_mcp.errors import TokenError
from kite_connect_mcp.events import rejections
from kite_connect_mcp.events.model import token_fingerprint
from kite_connect_mcp.events.rejections import KEY, MAX_KEPT, SnapshotRejections, StoredRejections
from kite_connect_mcp.events.store import EventStore
from kite_connect_mcp.timeutil import now_ist

EVENTS = {"KITE_MCP_EVENTS": "true", "KITE_MCP_TRANSPORT": "http", "KITE_MCP_BEARER_TOKEN": BEARER}
OTHER_TOKEN = "testaccesstoken00000000000000009"


def test_a_rejection_is_kept_by_fingerprint_and_cleared_by_accept(runtime_factory):
    rt = runtime_factory()
    rt.tokens.reject(ACCESS_TOKEN)
    assert rt.tokens.current() is None and rt.tokens.status()["logged_in"] is False
    assert rt.tokens.rejections.fingerprints() == [token_fingerprint(ACCESS_TOKEN)]
    assert ACCESS_TOKEN not in repr(rt.tokens.rejections.fingerprints())
    rt.tokens.accept(ACCESS_TOKEN)
    assert rt.tokens.current().access_token == ACCESS_TOKEN


async def test_with_the_events_on_a_rejection_survives_a_restart(runtime_factory, fake_kite):
    fake_kite.on("GET", "/orders", status=403, error_type="TokenException", message="Invalid session")
    first = runtime_factory(**EVENTS)
    assert await first.events.poller.poll("window") is False  # Kite refuses the stored token
    assert json.loads(first.events.store.meta(KEY)) == [token_fingerprint(ACCESS_TOKEN)]
    await first.aclose()
    second = runtime_factory(**EVENTS)  # the same state folder: the same store
    assert second.tokens.current() is None and "rejected" in second.tokens.status()["problem"]
    write_token_file(second.settings.token_file, token=OTHER_TOKEN)  # a new login: a new token is not marked
    assert second.tokens.current().access_token == OTHER_TOKEN


async def test_a_tool_call_that_kite_refuses_marks_the_token_in_the_store(runtime_factory, fake_kite):
    fake_kite.on("GET", "/user/profile", status=403, error_type="TokenException", message="Incorrect token")
    rt = runtime_factory(**EVENTS)
    with pytest.raises(TokenError):
        await rt.client.get("/user/profile")
    assert json.loads(rt.events.store.meta(KEY)) == [token_fingerprint(ACCESS_TOKEN)]
    rt.tokens.accept(ACCESS_TOKEN)  # what a login of this token does
    assert json.loads(rt.events.store.meta(KEY)) == []


def test_the_stored_marks_are_bounded_newest_last(tmp_path):
    store = EventStore(tmp_path / "events.sqlite3")
    try:
        marks = StoredRejections(store)
        for i in range(MAX_KEPT + 3):
            marks.add(f"{i:064x}")
        kept = json.loads(store.meta(KEY))
        assert len(kept) == MAX_KEPT and kept[-1] == f"{MAX_KEPT + 2:064x}"
        assert StoredRejections(store).fingerprints() == kept  # read back as written
    finally:
        store.close()


def test_a_store_that_cannot_write_keeps_the_mark_in_memory(runtime_factory, caplog):
    rt = runtime_factory(**EVENTS)
    rt.events.store.close()
    with caplog.at_level(logging.WARNING):
        rt.tokens.reject(ACCESS_TOKEN)
    assert rt.tokens.current() is None and "rejected-token mark" in caplog.text
    assert ACCESS_TOKEN not in caplog.text


def test_the_status_command_beside_the_service_sees_its_marks(cli_env, monkeypatch, capsys):
    write_token_file(Path(cli_env["KITE_TOKEN_FILE"]), logged_in_at=now_ist())  # valid on the real clock main() uses
    store = EventStore(Path(cli_env["KITE_MCP_STATE_DIR"]) / "events.sqlite3")
    try:
        StoredRejections(store).add(token_fingerprint(ACCESS_TOKEN))
    finally:
        store.close()
    for key, value in EVENTS.items():
        monkeypatch.setenv(key, value)
    assert main(["status"]) == 0
    session = json.loads(capsys.readouterr().out)["session"]
    assert session["logged_in"] is False and "rejected" in session["problem"]


@pytest.mark.parametrize(
    "value",
    [
        "not json",
        json.dumps({"a": 1}),
        json.dumps(["not a fingerprint"]),
        json.dumps("a" * 64),
        "[[[[",
        "",
        "[" * 100_000 + "]" * 100_000,  # nested past the parser's depth
    ],
)
def test_a_damaged_stored_value_reads_as_no_marks(tmp_path, value):
    store = EventStore(tmp_path / "events.sqlite3")
    try:
        store.set_meta(KEY, value)
        assert StoredRejections(store).fingerprints() == []
    finally:
        store.close()


def test_only_the_fingerprints_of_a_damaged_list_are_read(tmp_path):
    good = token_fingerprint(ACCESS_TOKEN)
    store = EventStore(tmp_path / "events.sqlite3")
    try:
        store.set_meta(KEY, json.dumps(["not hex", 7, None, good.upper(), good]))
        assert StoredRejections(store).fingerprints() == [good]
    finally:
        store.close()


def test_a_snapshot_of_a_store_that_is_not_there_holds_nothing_and_makes_no_file(tmp_path, caplog):
    path = tmp_path / "state" / "events.sqlite3"
    marks = SnapshotRejections(path)
    with caplog.at_level(logging.WARNING):
        assert token_fingerprint(ACCESS_TOKEN) not in marks and marks.fingerprints() == []
    assert not path.exists() and not path.parent.exists()
    assert caplog.text == ""  # no service has run yet: nothing to warn about


def _newer_store(path: Path) -> None:
    store = EventStore(path)
    try:
        store.set_meta("schema_version", "999")
    finally:
        store.close()


def _not_a_database(path: Path) -> None:
    path.write_bytes(b"not a database, but a file all the same\n" * 64)


def _store_that_fails_to_read(path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    EventStore(path).close()

    def failing(self: EventStore, key: str) -> str | None:
        raise rejections.StoreError("cannot read the event store: disk I/O error")

    monkeypatch.setattr(EventStore, "meta", failing)


@pytest.mark.parametrize(
    "spoil",
    [
        lambda path, _: _newer_store(path),
        lambda path, _: _not_a_database(path),
        _store_that_fails_to_read,
    ],
    ids=["newer-schema", "not-a-database", "read-fails"],
)
def test_a_snapshot_of_a_store_it_cannot_read_warns(tmp_path, monkeypatch, caplog, spoil):
    path = tmp_path / "events.sqlite3"
    spoil(path, monkeypatch)
    marks = SnapshotRejections(path)
    with caplog.at_level(logging.WARNING):
        assert token_fingerprint(ACCESS_TOKEN) not in marks
        assert marks.fingerprints() == []  # read once: one warning
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert warnings[0].startswith("cannot read the service's rejected-token marks (")
    assert warnings[0].endswith("); a token it refused may show as valid")


def _hold_the_write_lock(path: Path) -> sqlite3.Connection:
    """Another process's write in progress: the store's own writes wait their busy timeout and fail."""
    other = sqlite3.connect(path, isolation_level=None)
    other.execute("BEGIN IMMEDIATE")
    return other


def _release(other: sqlite3.Connection) -> None:
    other.execute("ROLLBACK")
    other.close()


@pytest.fixture
def busy_store(tmp_path):
    store = EventStore(tmp_path / "events.sqlite3", timeout_s=0.05)
    try:
        yield store
    finally:
        store.close()


def _stored(store: EventStore) -> list[str] | None:
    value = store.meta(KEY)
    return None if value is None else json.loads(value)


A, B, C = ("a" * 64, "b" * 64, "c" * 64)
UPDATE_FAILED = "could not update the rejected-token marks in the event store ("
HOLDS_IN_MEMORY = "); the change holds in memory until the server stops"


def test_a_mark_that_could_not_be_written_is_written_when_it_is_added_again(busy_store, caplog):
    marks = StoredRejections(busy_store)
    other = _hold_the_write_lock(busy_store.path)
    try:
        with caplog.at_level(logging.WARNING):
            marks.add(A)
    finally:
        _release(other)
    assert A in marks and _stored(busy_store) is None  # held in memory only
    assert UPDATE_FAILED in caplog.text and HOLDS_IN_MEMORY in caplog.text
    marks.add(A)  # the same token rejected again: no change in memory, but the store has not taken it yet
    assert _stored(busy_store) == [A]


@pytest.mark.parametrize(
    ("next_change", "then_stored"),
    [
        (lambda marks: marks.add(C), [B, C]),
        (lambda marks: marks.discard(A), [B]),  # the same login again: not held any more, but still to be written
    ],
    ids=["another-mark", "the-same-clear"],
)
def test_a_clear_that_could_not_be_written_is_written_by_the_next_change(busy_store, caplog, next_change, then_stored):
    marks = StoredRejections(busy_store)
    marks.add(A)
    marks.add(B)
    other = _hold_the_write_lock(busy_store.path)
    try:
        with caplog.at_level(logging.WARNING):
            marks.discard(A)
    finally:
        _release(other)
    assert marks.fingerprints() == [B] and _stored(busy_store) == [A, B]  # a restart now would still see A
    assert UPDATE_FAILED in caplog.text and HOLDS_IN_MEMORY in caplog.text
    next_change(marks)
    assert _stored(busy_store) == then_stored == marks.fingerprints()


def test_a_stored_mark_added_again_becomes_the_newest(busy_store):
    marks = StoredRejections(busy_store)
    for fingerprint in (A, B, A):
        marks.add(fingerprint)
    assert marks.fingerprints() == [B, A] and _stored(busy_store) == [B, A]


def test_a_change_that_changes_nothing_writes_nothing(busy_store, monkeypatch):
    marks = StoredRejections(busy_store)
    marks.add(A)
    writes: list[str] = []
    set_meta = busy_store.set_meta

    def counting(key: str, value: str) -> None:
        writes.append(value)
        set_meta(key, value)

    monkeypatch.setattr(busy_store, "set_meta", counting)
    marks.add(A)  # already the newest, and stored
    marks.discard(B)  # not held: what every login of a token Kite never refused does
    assert writes == []
    marks.discard(A)
    assert writes == ["[]"] and _stored(busy_store) == []


def test_a_store_that_cannot_be_read_starts_with_no_marks_and_warns(tmp_path, caplog):
    store = EventStore(tmp_path / "events.sqlite3")
    store.close()  # every read fails now
    with caplog.at_level(logging.WARNING):
        marks = StoredRejections(store)
    assert marks.fingerprints() == []
    assert "cannot read the rejected-token marks from the event store (" in caplog.text


def test_a_change_from_another_thread_waits_for_the_write_of_the_one_before(busy_store, monkeypatch):
    marks = StoredRejections(busy_store)
    set_meta = busy_store.set_meta
    second = threading.Thread(target=marks.add, args=(B,))

    def interleaving(key: str, value: str) -> None:
        if second.ident is None:  # the first write: another thread's change tries to run before it is done
            second.start()
            second.join(timeout=0.2)
        set_meta(key, value)

    monkeypatch.setattr(busy_store, "set_meta", interleaving)
    marks.add(A)
    second.join()
    assert marks.fingerprints() == [A, B] and _stored(busy_store) == [A, B]  # not the older list written last


def test_a_token_provider_keeps_its_marks_where_it_is_told(tmp_path):
    marks = MemoryRejections([token_fingerprint(ACCESS_TOKEN)])
    tokens = TokenProvider(
        token_file=TokenFile(write_token_file(tmp_path / "access_token.json"), writable=True),
        clock=lambda: FIXED_NOW,
        rejections=marks,
    )
    assert tokens.rejections is marks
    assert tokens.current() is None and "rejected" in tokens.status()["problem"]
    tokens.reject(OTHER_TOKEN)
    assert marks.fingerprints() == [token_fingerprint(ACCESS_TOKEN), token_fingerprint(OTHER_TOKEN)]
    tokens.accept(ACCESS_TOKEN)
    assert tokens.current().access_token == ACCESS_TOKEN and marks.fingerprints() == [token_fingerprint(OTHER_TOKEN)]


def test_a_snapshot_reads_the_store_once_and_never_writes_it(tmp_path, monkeypatch):
    path = tmp_path / "events.sqlite3"
    store = EventStore(path)
    try:
        StoredRejections(store).add(token_fingerprint(ACCESS_TOKEN))
        opened: list[dict[str, object]] = []

        def opening(where: Path, **kwargs: bool) -> EventStore:
            opened.append(kwargs)
            return EventStore(where, **kwargs)

        monkeypatch.setattr(rejections, "EventStore", opening)
        marks = SnapshotRejections(path)
        assert opened == []  # nothing is read until the command asks
        assert token_fingerprint(ACCESS_TOKEN) in marks
        marks.discard(token_fingerprint(ACCESS_TOKEN))  # a login in the command: its own memory only
        marks.add(token_fingerprint(OTHER_TOKEN))
        assert marks.fingerprints() == [token_fingerprint(OTHER_TOKEN)]
        assert opened == [{"readonly": True, "create": False}]  # once, read-only, never made
        assert json.loads(store.meta(KEY)) == [token_fingerprint(ACCESS_TOKEN)]  # the service's marks, unchanged
    finally:
        store.close()


def test_keep_rejections_in_carries_the_marks_held_so_far(runtime_factory, tmp_path):
    rt = runtime_factory()
    rt.tokens.reject(ACCESS_TOKEN)
    rt.tokens.reject(OTHER_TOKEN)
    store = EventStore(tmp_path / "other" / "events.sqlite3")
    try:
        rt.tokens.keep_rejections_in(StoredRejections(store))
        held = [token_fingerprint(ACCESS_TOKEN), token_fingerprint(OTHER_TOKEN)]
        assert isinstance(rt.tokens.rejections, StoredRejections) and rt.tokens.rejections.fingerprints() == held
        assert json.loads(store.meta(KEY)) == held  # written through
        assert rt.tokens.current() is None
        rt.tokens.accept(ACCESS_TOKEN)
        assert rt.tokens.current().access_token == ACCESS_TOKEN
        assert json.loads(store.meta(KEY)) == [token_fingerprint(OTHER_TOKEN)]
    finally:
        store.close()


def test_memory_marks_keep_the_newest_last():
    marks = MemoryRejections(["a" * 64, "b" * 64])
    marks.add("a" * 64)  # seen again: now the newest
    assert marks.fingerprints() == ["b" * 64, "a" * 64]
    marks.discard("c" * 64)  # not held: nothing happens
    assert "b" * 64 in marks and "c" * 64 not in marks
