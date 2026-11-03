"""The HTTP app: bearer auth on /mcp only, public health check, Host-header (DNS rebinding) checks, a real MCP
initialize over Streamable HTTP."""

from __future__ import annotations

import contextlib

import httpx2

from conftest import BEARER
from kite_connect_mcp.server import build_http_app, build_server

INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}},
}
MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


@contextlib.asynccontextmanager
async def http_client(runtime_factory, **overrides):
    rt = runtime_factory(KITE_MCP_TRANSPORT="http", KITE_MCP_BEARER_TOKEN=BEARER, **overrides)
    server, _ = build_server(rt)
    app = build_http_app(server, rt)
    starlette_app = app.app.app  # RuntimeLifespan(BearerAuth(Starlette)): run the MCP session manager's lifespan
    async with starlette_app.router.lifespan_context(starlette_app):
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://127.0.0.1:8765") as client:
            yield client


async def test_health_is_public(runtime_factory):
    async with http_client(runtime_factory) as client:
        response = await client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.headers["cache-control"] == "no-store"


async def test_mcp_needs_the_bearer_token(runtime_factory):
    async with http_client(runtime_factory) as client:
        missing = await client.post("/mcp", json=INIT, headers=MCP_HEADERS)
        wrong = await client.post("/mcp", json=INIT, headers={**MCP_HEADERS, "Authorization": "Bearer " + "x" * 40})
        basic = await client.post("/mcp", json=INIT, headers={**MCP_HEADERS, "Authorization": f"Basic {BEARER}"})
    for response in (missing, wrong, basic):
        assert response.status_code == 401
        assert response.json() == {"error": "unauthorized"}


async def test_mcp_initialize_with_the_bearer_token(runtime_factory):
    async with http_client(runtime_factory) as client:
        response = await client.post("/mcp", json=INIT, headers={**MCP_HEADERS, "Authorization": f"Bearer {BEARER}"})
    assert response.status_code == 200
    assert "kite-connect-mcp" in response.text


async def test_unknown_host_header_is_refused(runtime_factory):
    async with http_client(runtime_factory) as client:
        response = await client.post(
            "/mcp",
            json=INIT,
            headers={**MCP_HEADERS, "Authorization": f"Bearer {BEARER}", "Host": "evil.example.net"},
        )
    assert response.status_code in (400, 403, 421)


async def test_public_url_host_is_accepted(runtime_factory):
    async with http_client(runtime_factory, KITE_MCP_PUBLIC_URL="https://kite.example.com") as client:
        response = await client.post(
            "/mcp",
            json=INIT,
            headers={**MCP_HEADERS, "Authorization": f"Bearer {BEARER}", "Host": "kite.example.com"},
        )
    assert response.status_code == 200
