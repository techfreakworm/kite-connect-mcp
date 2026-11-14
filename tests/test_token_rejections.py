"""Kite's rejection of a token, kept by fingerprint: in memory by default, in the event store's meta table with the
events on (a restart remembers it), read by the commands beside the service, and cleared by a new token."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from conftest import ACCESS_TOKEN, BEARER, write_token_file
from kite_connect_mcp.auth.tokenstore import MemoryRejections
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
    StoredRejections(store).add(token_fingerprint(ACCESS_TOKEN))
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


def test_a_snapshot_of_a_store_that_is_not_there_holds_nothing_and_makes_no_file(tmp_path):
    path = tmp_path / "state" / "events.sqlite3"
    marks = SnapshotRejections(path)
    assert token_fingerprint(ACCESS_TOKEN) not in marks and marks.fingerprints() == []
    assert not path.exists() and not path.parent.exists()


def test_a_snapshot_reads_the_store_once_and_never_writes_it(tmp_path, monkeypatch):
    path = tmp_path / "events.sqlite3"
    store = EventStore(path)
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
    try:
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
