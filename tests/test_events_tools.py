"""The events tools: the service's own store, another process's store read-only, or hidden without a store."""

from __future__ import annotations

import sqlite3

from mcp import Client

from conftest import BEARER, tool_json, tool_text
from events_helpers import ORDER_ID, filled_row, order_row

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
        events = tool_json(await client.call_tool("get_order_events", {"order_id": ORDER_ID}))
        status = tool_json(await client.call_tool("get_event_status", {}))
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert [e["change"] for e in events["events"]] == ["placed", "filled"]
    assert all(e["text"] for e in events["events"])
    assert status["last_seq"] == 2
    assert status["configured_consumers"] == []
    assert {"get_order_events", "get_event_status"} <= {i.name for i in reg.registered}
    for name in TOOLS:
        assert tools[name].annotations.read_only_hint is True
        assert tools[name].annotations.open_world_hint is False
    assert fake_kite.writes() == []
    rt.events.store.close()


async def test_another_process_reads_the_store_read_only(mcp_factory, tmp_path):
    _, _, service_rt = mcp_factory(**SERVICE)
    service_rt.events.ingest_order(order_row(), source="poll")
    server, _, rt = mcp_factory(KITE_MCP_EVENTS_DB=str(service_rt.settings.events_db))
    assert rt.events is None
    async with Client(server) as client:
        events = tool_json(await client.call_tool("get_order_events", {"limit": 5}))
        status = tool_json(await client.call_tool("get_event_status", {}))
    assert len(events["events"]) == 1
    assert status["last_seq"] == 1 and "configured_consumers" not in status
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
    assert "does not exist" in reg.skipped["get_order_events"]
    assert not (tmp_path / "missing.sqlite3").exists()


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
    assert "does not exist" in reg.skipped["get_order_events"]


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
    assert [(e["seq"], e["order"]["order_id"]) for e in other["events"]] == [(2, OTHER_ORDER)]
    assert fake_kite.writes() == []
    rt.events.store.close()


async def test_limits_are_checked(mcp_factory):
    server, _, rt = mcp_factory(**SERVICE)
    async with Client(server) as client:
        result = await client.call_tool("get_order_events", {"limit": 0})
        too_many = await client.call_tool("get_order_events", {"limit": 201})
        before_the_first = await client.call_tool("get_order_events", {"after_seq": -1})
        most = await client.call_tool("get_order_events", {"limit": 200})
    assert result.is_error
    assert too_many.is_error and "limit must be between 1 and 200" in tool_text(too_many)
    assert before_the_first.is_error and "after_seq must be 0 or more" in tool_text(before_the_first)
    assert tool_json(most) == {"events": []}
    rt.events.store.close()
