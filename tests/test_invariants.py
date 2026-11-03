"""Invariants over every tool, present and future.

1. Every write tool is a dry run by default: called with only its required arguments, nothing that could change the
   account reaches Kite. Each write tool must have an entry in SAMPLE_ARGS (the test fails for a new one without it).
2. No tool module can mint a write ticket: only safety/pipeline.py references the ticket issuer.
3. Read-only mode exposes no write tool.
"""

from __future__ import annotations

import importlib.util
import pathlib

from mcp import Client

from conftest import tool_text

# minimal valid arguments per write tool, one file per tool module: tests/samples/<module>.py with SAMPLE_ARGS = {...}
# (dry run is the default, so none of these may send anything)
SAMPLE_ARGS: dict[str, dict] = {}
for _path in sorted((pathlib.Path(__file__).parent / "samples").glob("*.py")):
    _spec = importlib.util.spec_from_file_location(f"samples_{_path.stem}", _path)
    assert _spec is not None and _spec.loader is not None
    _module = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_module)
    SAMPLE_ARGS.update(_module.SAMPLE_ARGS)


def answer_everything(fake):
    """A FakeKite that answers every path with a plausible success, so tools get as far as they can."""
    import httpx2

    def ok(request: httpx2.Request) -> httpx2.Response:
        path = request.url.path
        if request.method == "GET" and path.startswith("/orders/"):
            data = [
                {
                    "order_id": "111",
                    "status": "OPEN",
                    "exchange": "NSE",
                    "tradingsymbol": "INFY",
                    "transaction_type": "BUY",
                    "order_type": "LIMIT",
                    "product": "CNC",
                    "quantity": 1,
                    "price": 1500.0,
                }
            ]
        elif path == "/quote/ltp":
            data = {"NSE:INFY": {"instrument_token": 408065, "last_price": 1500.0}}
        else:
            data = {}
        return httpx2.Response(200, json={"status": "success", "data": data})

    original = fake._handle

    def handle(request):
        fake.requests.append(request)
        handler = fake.routes.get((request.method, request.url.path))
        return handler(request) if handler else ok(request)

    fake._handle = handle
    return original


async def test_every_write_tool_is_dry_run_by_default(mcp_factory, fake_kite):
    answer_everything(fake_kite)
    server, reg, _ = mcp_factory(KITE_MCP_TRADING_MODE="live", KITE_MCP_ALLOW_MF_WRITES="true")
    writes = [info.name for info in reg.registered if info.kind == "write"]
    missing = sorted(set(writes) - set(SAMPLE_ARGS))
    assert not missing, f"add these write tools to SAMPLE_ARGS: {missing}"
    async with Client(server) as client:
        for name in writes:
            result = await client.call_tool(name, SAMPLE_ARGS[name])
            assert fake_kite.writes() == [], f"{name} sent a write without dry_run=false"
            if not result.is_error:
                assert '"dry_run":true' in tool_text(result), name


def test_only_the_pipeline_mints_write_tickets():
    root = pathlib.Path(__file__).resolve().parents[1] / "src" / "kite_connect_mcp"
    users = sorted(
        str(p.relative_to(root)) for p in root.rglob("*.py") if "_TICKET_ISSUER" in p.read_text(encoding="utf-8")
    )
    assert users == ["client/http.py", "safety/pipeline.py"]


def test_tools_never_call_the_client_write_path():
    root = pathlib.Path(__file__).resolve().parents[1] / "src" / "kite_connect_mcp" / "tools"
    for path in root.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "client.write(" not in text, path.name
        assert "WriteTicket" not in text, path.name


async def test_read_only_exposes_no_write_tool(mcp_factory):
    _, reg, _ = mcp_factory(KITE_MCP_READ_ONLY="true", KITE_MCP_ALLOW_MF_WRITES="true")
    assert [i.name for i in reg.registered if i.kind == "write"] == []
