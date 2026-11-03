"""Test fixtures. No test ever reaches the network: a guard below refuses any non-loopback connection, and every
Kite call goes to FakeKite, an in-memory stand-in for api.kite.trade.

    fake_kite           routes (method, path) -> canned answers; records every request it sees
    settings_factory    Settings from a test environment (temporary state/cache dirs, a valid token file)
    runtime_factory     a Runtime wired to fake_kite, with a fixed clock and no real sleeping
    mcp_factory         (server, registrar, runtime) for in-process MCP tests via `mcp.Client(server)`
"""

from __future__ import annotations

import datetime as dt
import json
import socket
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx2
import pytest

from kite_connect_mcp.auth.tokenstore import TokenRecord
from kite_connect_mcp.config import Settings
from kite_connect_mcp.timeutil import IST

FIXED_NOW = dt.datetime(2026, 11, 4, 10, 0, 0, tzinfo=IST)  # a Wednesday, during market hours
API_KEY = "testapikey0001"
API_SECRET = "testapisecret000000000000000001"
ACCESS_TOKEN = "testaccesstoken00000000000000001"
BEARER = "b" * 40


# --- no network ---------------------------------------------------------------------------------------------------

_real_connect = socket.socket.connect


def _guarded_connect(self: socket.socket, address: Any) -> Any:
    host = address[0] if isinstance(address, tuple) else address
    if isinstance(host, str) and (
        host.startswith("127.") or host in ("localhost", "::1") or self.family == socket.AF_UNIX
    ):
        return _real_connect(self, address)
    raise RuntimeError(f"tests must not reach the network (tried {host!r})")


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket.socket, "connect", _guarded_connect)


# --- the fake Kite API ----------------------------------------------------------------------------------------------


class FakeKite:
    """An in-memory api.kite.trade. Unrouted requests get a 404 InputException, like Kite does for unknown routes."""

    CALC_PATHS = ("/margins/orders", "/margins/basket", "/charges/orders")

    def __init__(self) -> None:
        self.requests: list[httpx2.Request] = []
        self.routes: dict[tuple[str, str], Callable[[httpx2.Request], httpx2.Response]] = {}

    def on(
        self,
        method: str,
        path: str,
        data: Any = None,
        *,
        status: int = 200,
        error_type: str | None = None,
        message: str = "error",
        body: bytes | None = None,
        content_type: str = "application/json",
        handler: Callable[[httpx2.Request], httpx2.Response] | None = None,
    ) -> FakeKite:
        if handler is None:
            if body is not None:

                def handler(_: httpx2.Request, body: bytes = body) -> httpx2.Response:
                    return httpx2.Response(status, content=body, headers={"content-type": content_type})

            elif error_type is not None or status >= 400:
                payload = {
                    "status": "error",
                    "message": message,
                    "error_type": error_type or "GeneralException",
                    "data": None,
                }

                def handler(_: httpx2.Request, payload: dict = payload) -> httpx2.Response:
                    return httpx2.Response(status, json=payload)

            else:

                def handler(_: httpx2.Request, data: Any = data) -> httpx2.Response:
                    return httpx2.Response(status, json={"status": "success", "data": data})

        self.routes[(method.upper(), path)] = handler
        return self

    def _handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        handler = self.routes.get((request.method, request.url.path))
        if handler is None:
            return httpx2.Response(
                404,
                json={"status": "error", "message": "Route not found", "error_type": "InputException", "data": None},
            )
        return handler(request)

    def transport(self) -> httpx2.MockTransport:
        return httpx2.MockTransport(self._handle)

    def calls(self, method: str | None = None, path: str | None = None) -> list[httpx2.Request]:
        return [
            r for r in self.requests if (method is None or r.method == method) and (path is None or r.url.path == path)
        ]

    def writes(self) -> list[httpx2.Request]:
        """Every request that could change the account (anything but GET and the computing POSTs)."""
        return [r for r in self.requests if r.method != "GET" and r.url.path not in self.CALC_PATHS]

    @staticmethod
    def form(request: httpx2.Request) -> list[tuple[str, str]]:
        from urllib.parse import parse_qsl

        return parse_qsl(request.content.decode(), keep_blank_values=True)

    @staticmethod
    def json_body(request: httpx2.Request) -> Any:
        return json.loads(request.content)


@pytest.fixture
def fake_kite() -> FakeKite:
    return FakeKite()


# --- settings and runtime ---------------------------------------------------------------------------------------------


def write_token_file(path: Path, *, token: str = ACCESS_TOKEN, logged_in_at: dt.datetime | None = None) -> Path:
    record = TokenRecord.new(token, logged_in_at or (FIXED_NOW - dt.timedelta(hours=3)))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(record.to_json(), encoding="utf-8")
    path.chmod(0o600)
    return path


@pytest.fixture
def base_env(tmp_path: Path) -> dict[str, str]:
    state = tmp_path / "state"
    cache = tmp_path / "cache"
    token_file = write_token_file(state / "access_token.json")
    return {
        "KITE_API_KEY": API_KEY,
        "KITE_API_SECRET": API_SECRET,
        "KITE_MCP_STATE_DIR": str(state),
        "KITE_MCP_CACHE_DIR": str(cache),
        "KITE_TOKEN_FILE": str(token_file),
        "HOME": str(tmp_path / "home"),
    }


@pytest.fixture
def settings_factory(base_env: dict[str, str]) -> Callable[..., Settings]:
    def make(**overrides: str | None) -> Settings:
        env = dict(base_env)
        for key, value in overrides.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
        return Settings.from_env(env)

    return make


async def _no_sleep(_: float) -> None:
    return None


@pytest.fixture
def runtime_factory(fake_kite: FakeKite, settings_factory: Callable[..., Settings]):
    from kite_connect_mcp.server import build_runtime

    created = []

    def make(settings: Settings | None = None, **overrides: str | None):
        rt = build_runtime(
            settings or settings_factory(**overrides),
            transport=fake_kite.transport(),
            clock=lambda: FIXED_NOW,
            sleep=_no_sleep,
        )
        created.append(rt)
        return rt

    yield make


@pytest.fixture
def mcp_factory(runtime_factory):
    from kite_connect_mcp.server import build_server

    def make(settings: Settings | None = None, **overrides: str | None):
        rt = runtime_factory(settings, **overrides)
        server, reg = build_server(rt)
        return server, reg, rt

    return make


def tool_text(result: Any) -> str:
    return "".join(getattr(block, "text", "") for block in result.content)


def tool_json(result: Any) -> Any:
    assert not result.is_error, tool_text(result)
    return json.loads(tool_text(result))
