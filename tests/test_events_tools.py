"""The events tools: the service's own store, another process's store read-only, or hidden without a store."""

from __future__ import annotations

import logging
import os
import sqlite3

import pytest
from mcp import Client

from conftest import BEARER, tool_json, tool_text
from events_helpers import ORDER_ID, filled_row, order_row
from kite_connect_mcp.events.store import EventStore

SERVICE = {"KITE_MCP_EVENTS": "true", "KITE_MCP_TRANSPORT": "http", "KITE_MCP_BEARER_TOKEN": BEARER}
TOOLS = {"get_order_events", "get_event_status"}
OTHER_ORDER = "250101000000002"


async def test_hidden_without_a_store(mcp_factory):
    _, reg, _ = mcp_factory()
    assert "no event store" in reg.skipped["get_order_events"]
    assert "no event store" in reg.skipped["get_event_status"]


async def test_the_service_reads_its_own_store(mcp_factory, fake_kite):
    server, reg, rt = mcp_factory(**SERVICE)
    rt.events.ingest_order(order_row(), source="poll")
    rt.events.ingest_order(filled_row(), source="postback")
    async with Client(server) as client:
        result = await client.call_tool("get_order_events", {"order_id": ORDER_ID})
        status = tool_json(await client.call_tool("get_event_status", {}))
        tools = {t.name: t for t in (await client.list_tools()).tools}
    events = tool_json(result)
    assert [e["change"] for e in events["events"]] == ["placed", "filled"]
    assert all(e["text"] for e in events["events"])
    assert events["last_seq"] == 2
    assert "AB1234" not in tool_text(result)  # the rows' user_id and placed_by stay out
    assert status["last_seq"] == 2
    assert status["configured_consumers"] == []
    assert status["poll"]["window"] == "09:00-16:00"  # the service knows its own poll settings
    assert {"get_order_events", "get_event_status"} <= {i.name for i in reg.registered}
    for name in TOOLS:
        assert tools[name].annotations.read_only_hint is True
        assert tools[name].annotations.open_world_hint is False
    assert fake_kite.writes() == []
    rt.events.store.close()


async def test_another_process_reads_the_store_read_only(mcp_factory):
    _, _, service_rt = mcp_factory(**SERVICE)
    service_rt.events.ingest_order(order_row(), source="poll")
    server, _, rt = mcp_factory(KITE_MCP_EVENTS_DB=str(service_rt.settings.events_db))
    assert rt.events is None
    assert len(rt.closers) == 1  # a store of its own, opened read-only
    opened = rt.closers[0].__self__
    assert isinstance(opened, EventStore) and opened.readonly and opened is not service_rt.events.store
    async with Client(server) as client:
        events = tool_json(await client.call_tool("get_order_events", {"limit": 5}))
        status = tool_json(await client.call_tool("get_event_status", {}))
    assert len(events["events"]) == 1 and events["last_seq"] == 1
    assert status["last_seq"] == 1 and "configured_consumers" not in status
    service_rt.events.store.close()


async def test_another_process_leaves_the_poll_to_the_serving_process(mcp_factory):
    """The poll section and the after-close warning come from the poll settings, which a reader may not share."""
    _, _, service_rt = mcp_factory(**SERVICE)
    service_rt.events.store.set_meta("baseline_done", "2026-11-02")  # polled Monday, not after Tuesday's close
    assert "Tue 2026-11-03" in service_rt.events.status()["warning"]
    server, _, _ = mcp_factory(KITE_MCP_EVENTS_DB=str(service_rt.settings.events_db))
    async with Client(server) as client:
        status = tool_json(await client.call_tool("get_event_status", {}))
    assert status["poll"] == "see the serving process's status"
    assert "warning" not in status
    assert status["baseline_done"] == "2026-11-02"  # what the store records is still shown
    service_rt.events.store.close()


async def test_the_read_only_store_is_closed_with_the_runtime(mcp_factory):
    _, _, service_rt = mcp_factory(**SERVICE)
    service_rt.events.ingest_order(order_row(), source="poll")
    service_rt.events.store.close()
    server, _, rt = mcp_factory(KITE_MCP_EVENTS_DB=str(service_rt.settings.events_db))
    await rt.aclose()
    async with Client(server) as client:
        result = await client.call_tool("get_order_events", {})
    assert result.is_error and "cannot read the event store" in tool_text(result)


async def test_a_store_that_does_not_exist_yet_hides_the_tools(mcp_factory, tmp_path):
    _, reg, _ = mcp_factory(KITE_MCP_EVENTS_DB=str(tmp_path / "missing.sqlite3"))
    assert "no event store at" in reg.skipped["get_order_events"]
    assert "no event store at" in reg.skipped["get_event_status"]
    assert not (tmp_path / "missing.sqlite3").exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root can enter any folder")
async def test_a_store_out_of_reach_hides_the_tools_not_the_server(mcp_factory, tmp_path):
    """Another user's state folder, say: the server still starts, with every other tool."""
    path = tmp_path / "state" / "events.sqlite3"
    EventStore(path).close()
    path.parent.chmod(0)
    try:
        server, reg, rt = mcp_factory(KITE_MCP_EVENTS_DB=str(path))
        async with Client(server) as client:
            listed = {t.name for t in (await client.list_tools()).tools}
    finally:
        path.parent.chmod(0o700)
    for name in TOOLS:
        assert "cannot open the event store" in reg.skipped[name] and "Permission denied" in reg.skipped[name]
    assert not TOOLS & listed and "get_profile" in listed
    assert rt.closers == []


async def test_a_store_of_another_version_hides_the_tools(mcp_factory, tmp_path):
    path = tmp_path / "other.sqlite3"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    con.execute("INSERT INTO meta VALUES ('schema_version', '99')")
    con.commit()
    con.close()
    _, reg, _ = mcp_factory(KITE_MCP_EVENTS_DB=str(path))
    assert "schema version 99" in reg.skipped["get_order_events"]
    assert "schema version 99" in reg.skipped["get_event_status"]


async def test_the_toolsets_turn_them_off_before_any_store_is_opened(mcp_factory, tmp_path):
    _, _, service_rt = mcp_factory(**SERVICE)
    _, reg, rt = mcp_factory(KITE_MCP_EVENTS_DB=str(service_rt.settings.events_db), KITE_MCP_TOOLSETS="account")
    assert (
        reg.skipped["get_order_events"]
        == reg.skipped["get_event_status"]
        == "toolset events is not in KITE_MCP_TOOLSETS"
    )
    assert rt.closers == []  # the store exists and is readable, but nothing opened it
    service_rt.events.store.close()
    _, reg, _ = mcp_factory(
        KITE_MCP_EVENTS_DB=str(tmp_path / "missing.sqlite3"), KITE_MCP_EXCLUDED_TOOLS="get_event_status"
    )
    assert reg.skipped["get_event_status"] == "listed in KITE_MCP_EXCLUDED_TOOLS"
    assert "no event store at" in reg.skipped["get_order_events"]


async def test_one_order_and_paging(mcp_factory, fake_kite):
    server, _, rt = mcp_factory(**SERVICE)
    rt.events.ingest_order(order_row(), source="poll")
    rt.events.ingest_order(order_row(order_id=OTHER_ORDER), source="poll")
    rt.events.ingest_order(filled_row(), source="postback")
    async with Client(server) as client:
        newest = tool_json(await client.call_tool("get_order_events", {"limit": 2}))
        after = tool_json(await client.call_tool("get_order_events", {"after_seq": 1, "limit": 1}))
        other = tool_json(await client.call_tool("get_order_events", {"order_id": OTHER_ORDER}))
    assert [e["seq"] for e in newest["events"]] == [2, 3]
    assert [e["seq"] for e in after["events"]] == [2]
    assert after["last_seq"] == 3  # the page ends before the newest event: more follow
    assert [(e["seq"], e["order"]["order_id"]) for e in other["events"]] == [(2, OTHER_ORDER)]
    assert fake_kite.writes() == []
    rt.events.store.close()


async def test_limits_are_checked(mcp_factory):
    server, _, rt = mcp_factory(**SERVICE)
    async with Client(server) as client:
        result = await client.call_tool("get_order_events", {"limit": 0})
        too_many = await client.call_tool("get_order_events", {"limit": 201})
        before_the_first = await client.call_tool("get_order_events", {"after_seq": -1})
        too_big = [await client.call_tool("get_order_events", {"after_seq": n}) for n in (2**63, 2**70)]
        largest = await client.call_tool("get_order_events", {"after_seq": 2**63 - 1})
        most = await client.call_tool("get_order_events", {"limit": 200})
    assert result.is_error
    assert too_many.is_error and "limit must be between 1 and 200" in tool_text(too_many)
    for wrong in (before_the_first, *too_big):
        assert wrong.is_error and f"after_seq must be between 0 and {2**63 - 1}" in tool_text(wrong)
    assert tool_json(largest) == tool_json(most) == {"events": [], "last_seq": 0}
    rt.events.store.close()


async def test_every_closer_runs_newest_first_and_a_failing_one_hides_nothing(runtime_factory, caplog):
    rt = runtime_factory()
    closed: list[str] = []

    def failing() -> None:
        closed.append("failing")
        raise OSError("cannot close")

    rt.closers.extend([lambda: closed.append("first"), failing, lambda: closed.append("last")])
    try:
        with caplog.at_level(logging.ERROR):
            await rt.aclose()
        assert closed == ["last", "failing", "first"]
        assert "cannot close" in caplog.text

        async def broken() -> None:
            raise RuntimeError("the client did not close")

        rt.client.aclose = broken  # type: ignore[method-assign]
        closed.clear()
        with pytest.raises(RuntimeError, match="the client did not close"):  # not the closer's OSError
            await rt.aclose()
        assert closed == ["last", "failing", "first"]
    finally:
        rt.closers.clear()  # the fixture's teardown would run them again
