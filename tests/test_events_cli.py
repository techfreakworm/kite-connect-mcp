"""`kite-connect-mcp events status|tail|pause|resume|retry|selftest`."""

from __future__ import annotations

import asyncio
import json
import os
import socket
from pathlib import Path

import httpx2
import pytest

import kite_connect_mcp.server as server_module
from conftest import API_SECRET, BEARER, FIXED_NOW
from events_helpers import ORDER_ID, PY, filled_row, make_service, order_row, write_consumers
from kite_connect_mcp import cli
from kite_connect_mcp.auth.login import cli_login, cli_logout
from kite_connect_mcp.errors import ConfigError, KiteMCPError
from kite_connect_mcp.events import selftest
from kite_connect_mcp.events.model import is_selftest_id
from kite_connect_mcp.events.store import EventStore
from kite_connect_mcp.postback import verify_checksum
from kite_connect_mcp.server import build_http_app, build_server
from kite_connect_mcp.tools._base import Runtime

NOTIFY = {"name": "notify", "command": [PY, "-c", "pass"]}
SERVICE = {"KITE_MCP_EVENTS": "true", "KITE_MCP_TRANSPORT": "http", "KITE_MCP_BEARER_TOKEN": BEARER}
HTTP_NO_EVENTS = {"KITE_MCP_TRANSPORT": "http", "KITE_MCP_BEARER_TOKEN": BEARER, "KITE_MCP_POSTBACK_PATH": "/postback"}


@pytest.fixture
def env(runtime_factory, tmp_path, monkeypatch, base_env):
    svc, rt = make_service(runtime_factory, tmp_path, consumers=[NOTIFY])
    svc.ingest_order(order_row(), source="poll")
    svc.ingest_order(filled_row(), source="poll")
    for key, value in {**base_env, "KITE_MCP_EVENTS_DB": str(rt.settings.events_db)}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("KITE_MCP_EVENTS_CONSUMERS_FILE", str(rt.settings.events_consumers_file))
    yield svc
    svc.store.close()


def run(capsys, *args):
    code = cli.main(["events", *args])
    return code, capsys.readouterr()


def test_status(env, capsys):
    code, out = run(capsys, "status")
    data = json.loads(out.out)
    assert code == 0 and data["last_seq"] == 2 and data["consumers"]["notify"]["pending"] == 2


def test_tail(env, capsys):
    code, out = run(capsys, "tail", "--limit", "1")
    (line,) = out.out.splitlines()
    assert code == 0 and json.loads(line)["change"] == "filled"
    code, out = run(capsys, "tail", "--order", ORDER_ID)
    assert len(out.out.splitlines()) == 2


def test_pause_resume_skip_and_retry(env, capsys):
    assert run(capsys, "pause", "notify")[0] == 0
    assert env.store.is_paused("notify")
    code, out = run(capsys, "resume", "notify", "--skip-backlog")
    assert code == 0 and "2 waiting deliveries skipped" in out.out and not env.store.is_paused("notify")
    assert env.store.summary(["notify"])["consumers"]["notify"]["skipped"] == 2
    # both deliveries are skipped now, and only a pending one can fail: a new event's delivery fails
    seq = env.ingest_order(order_row(order_id="250101000000002"), source="poll")
    assert env.store.finish_delivery("notify", seq, "failed", env.clock(), "exit 1")
    code, out = run(capsys, "retry", "notify")
    assert code == 0 and out.out.strip() == "1 failed delivery of notify queued again"
    assert env.store.summary(["notify"])["consumers"]["notify"]["pending"] == 1
    code, out = run(capsys, "retry", "notify")
    assert code == 0 and out.out.strip() == "0 failed deliveries of notify queued again"


def test_an_unknown_consumer_is_an_error(env, capsys):
    code, out = run(capsys, "pause", "nobody")
    assert code == 2 and "nobody" in out.err


def test_a_paused_consumer_that_is_no_longer_configured_can_be_resumed(env, capsys):
    env.store.pause("retired", env.clock())  # paused once, then removed from the consumers file
    code, out = run(capsys, "resume", "retired")
    assert code == 0 and "resumed retired" in out.out and not env.store.is_paused("retired")


def test_retry_refuses_a_consumer_no_longer_configured_unless_it_is_paused(env, capsys, tmp_path):
    assert env.store.finish_delivery("notify", 1, "failed", env.clock(), "exit 1")
    write_consumers(tmp_path, [{"name": "other", "command": [PY, "-c", "pass"]}])  # notify is removed from the file
    code, out = run(capsys, "retry", "notify")
    assert code == 2 and "no longer in the consumers file" in out.err
    assert env.store.summary()["consumers"]["notify"]["failed"] == 1  # nothing was queued
    assert run(capsys, "pause", "notify")[0] == 0  # known from its deliveries
    code, out = run(capsys, "retry", "notify")  # paused: its deliveries wait for `resume`
    assert code == 0 and out.out.strip() == "1 failed delivery of notify queued again"


def test_a_name_neither_configured_nor_in_the_store_never_opens_it_read_write(env, capsys, monkeypatch):
    opened = []
    real_init = EventStore.__init__

    def spy(self, path, *, readonly=False, **kw):
        opened.append(readonly)
        real_init(self, path, readonly=readonly, **kw)

    monkeypatch.setattr(EventStore, "__init__", spy)
    for action in ("pause", "resume", "retry"):
        code, out = run(capsys, action, "nobody")
        assert code == 2 and "unknown consumer 'nobody' (known: notify)" in out.err
    assert opened == [True, True, True]


def test_a_broken_consumers_file_falls_back_to_the_names_in_the_store(env, capsys, tmp_path):
    (tmp_path / "consumers.json").write_text("{")
    code, out = run(capsys, "pause", "notify")
    assert code == 0 and env.store.is_paused("notify")
    assert "warning" in out.err and "not valid JSON" in out.err and "the consumers the event store knows" in out.err
    code, out = run(capsys, "status")
    data = json.loads(out.out)
    assert code == 0 and "notify" in data["consumers"] and "configured_consumers" not in data
    assert "not valid JSON" in out.err
    code, out = run(capsys, "pause", "nobody")
    assert code == 2 and "unknown consumer 'nobody'" in out.err


def test_status_and_tail_do_not_change_the_store(env, capsys):
    before = env.store.summary(["notify"])
    run(capsys, "status")
    run(capsys, "tail")
    assert env.store.summary(["notify"]) == before


def test_without_a_store_it_says_so(capsys, monkeypatch, base_env, tmp_path):
    for key, value in base_env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("KITE_MCP_EVENTS_DB", raising=False)
    code, out = run(capsys, "status")
    assert code == 2 and "event store" in out.err


def test_a_store_that_is_not_there_yet_is_not_created(capsys, monkeypatch, base_env, tmp_path):
    for key, value in base_env.items():
        monkeypatch.setenv(key, value)
    missing = tmp_path / "events.sqlite3"
    monkeypatch.setenv("KITE_MCP_EVENTS_DB", str(missing))
    for configured in (False, True):  # a configured name opens the store read-write at once: it must not create it
        if configured:
            monkeypatch.setenv("KITE_MCP_EVENTS_CONSUMERS_FILE", str(write_consumers(tmp_path, [NOTIFY])))
        for args in (("status",), ("tail",), ("pause", "notify"), ("resume", "notify"), ("retry", "notify")):
            code, out = run(capsys, *args)
            assert code == 2 and "no event store at" in out.err, args
    assert not missing.exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root can enter any folder")
def test_a_store_in_a_folder_it_cannot_enter_is_a_clean_error(capsys, monkeypatch, base_env, tmp_path):
    for key, value in base_env.items():
        monkeypatch.setenv(key, value)
    folder = tmp_path / "locked"
    EventStore(folder / "events.sqlite3").close()
    monkeypatch.setenv("KITE_MCP_EVENTS_DB", str(folder / "events.sqlite3"))
    monkeypatch.setenv("KITE_MCP_EVENTS_CONSUMERS_FILE", str(write_consumers(tmp_path, [NOTIFY])))
    folder.chmod(0)
    try:
        for args in (("status",), ("tail",), ("pause", "notify"), ("pause", "nobody")):
            code, out = run(capsys, *args)
            assert code == 2 and "cannot open the event store" in out.err and "Traceback" not in out.err, args
    finally:
        folder.chmod(0o700)


@pytest.mark.parametrize("limit", ["0", "-1", "1001", "many"])
def test_tail_takes_a_limit_from_1_to_1000(env, capsys, limit):
    with pytest.raises(SystemExit) as exit_:
        cli.main(["events", "tail", "--limit", limit])
    assert exit_.value.code == 2 and "--limit" in capsys.readouterr().err


def test_the_selftest_payload_is_signed_and_marked():
    payload = selftest.build_payload(API_SECRET)
    assert payload["order_id"].startswith("selftest-")
    assert is_selftest_id(payload["order_id"])  # the shape the postback route takes a selftest for
    assert verify_checksum(payload, API_SECRET)
    assert API_SECRET not in json.dumps(payload)


async def test_the_selftest_goes_through_the_real_route(runtime_factory):
    rt = runtime_factory(
        KITE_MCP_EVENTS="true",
        KITE_MCP_TRANSPORT="http",
        KITE_MCP_BEARER_TOKEN=BEARER,
        KITE_MCP_POSTBACK_PATH="/postback",
    )
    server, _ = build_server(rt)
    app = build_http_app(server, rt)
    status = await selftest.send(
        "http://testserver/postback", selftest.build_payload(API_SECRET), transport=httpx2.ASGITransport(app=app)
    )
    assert status == 200
    (event,) = rt.events.store.events()
    assert event["kind"] == "selftest"
    rt.events.store.close()


def test_the_selftest_command(capsys, monkeypatch, base_env):
    for key, value in base_env.items():
        monkeypatch.setenv(key, value)
    seen = {}

    async def fake_send(url, payload, **_):
        seen["url"], seen["payload"] = url, payload
        return 200

    monkeypatch.setattr(selftest, "send", fake_send)
    code, out = run(capsys, "selftest", "--url", "https://kite.example.com/postback")
    assert code == 0 and "200" in out.out
    assert seen["url"] == "https://kite.example.com/postback" and verify_checksum(seen["payload"], API_SECRET)
    assert API_SECRET not in out.out + out.err
    for key, value in {**SERVICE, "KITE_MCP_POSTBACK_PATH": "/postback"}.items():
        monkeypatch.setenv(key, value)
    code, _ = run(capsys, "selftest")
    assert code == 0 and seen["url"] == "http://127.0.0.1:8765/postback"


def test_without_the_event_service_the_selftest_needs_a_url(capsys, monkeypatch, base_env):
    """With the events off, this server's route hands a postback to the ticker's buffer: there is nothing to test."""
    for key, value in {**base_env, **HTTP_NO_EVENTS}.items():
        monkeypatch.setenv(key, value)
    sent = []

    async def fake_send(url, payload, **_):
        sent.append(url)
        return 200

    monkeypatch.setattr(selftest, "send", fake_send)
    code, out = run(capsys, "selftest")
    assert code == 2 and "KITE_MCP_EVENTS" in out.err and "--url" in out.err
    assert sent == []


async def test_a_selftest_to_a_server_without_the_event_service_is_refused(runtime_factory):
    rt = runtime_factory(**HTTP_NO_EVENTS)
    server, _ = build_server(rt)
    app = build_http_app(server, rt)
    status = await selftest.send(
        "http://testserver/postback", selftest.build_payload(API_SECRET), transport=httpx2.ASGITransport(app=app)
    )
    assert status == 404
    assert rt.ticker.events.recent(500)[0] == []  # no made-up order where agents read order updates


@pytest.mark.parametrize(
    ("status", "hint"),
    [
        (404, "the event service is off there"),
        (403, "KITE_API_SECRET"),
        (503, "event store cannot write"),
        (308, "a redirect"),
        (500, "(expected 200)"),
    ],
)
def test_a_failed_selftest_says_what_its_answer_likely_means(capsys, monkeypatch, base_env, status, hint):
    for key, value in base_env.items():
        monkeypatch.setenv(key, value)

    async def answer(url, payload, **_):
        return status

    monkeypatch.setattr(selftest, "send", answer)
    code, out = run(capsys, "selftest", "--url", "https://kite.example.com/postback")
    assert code == 1 and f"HTTP {status}" in out.out and hint in out.out


def test_the_selftest_hides_a_password_in_the_url(capsys, monkeypatch, base_env):
    for key, value in base_env.items():
        monkeypatch.setenv(key, value)

    async def ok(url, payload, **_):
        return 200

    monkeypatch.setattr(selftest, "send", ok)
    code, out = run(capsys, "selftest", "--url", "https://someone:pw-ABCD@kite.example.com/postback")
    assert code == 0 and "https://[redacted]@kite.example.com/postback" in out.out
    assert "pw-ABCD" not in out.out + out.err and "someone" not in out.out + out.err


@pytest.mark.parametrize(
    ("url", "shown"),
    [
        ("https://kite.example.com/postback", "https://kite.example.com/postback"),
        ("https://a:b@kite.example.com/postback", "https://[redacted]@kite.example.com/postback"),
        ("http://a@127.0.0.1:8765/postback", "http://[redacted]@127.0.0.1:8765/postback"),
        ("a:b@kite.example.com/postback", "[redacted]@kite.example.com/postback"),
        ("https://kite.example.com/a@b", "https://kite.example.com/a@b"),
        ("https://kite.example.com/?to=a@b", "https://kite.example.com/?to=a@b"),
    ],
)
def test_a_url_is_shown_without_its_user_and_password(url, shown):
    assert selftest.shown_url(url) == shown


async def test_an_unreachable_url_is_reported_without_its_password():
    with pytest.raises(KiteMCPError) as error:
        await selftest.send(
            "https://someone:pw-ABCD@kite.example.com/postback",
            selftest.build_payload(API_SECRET),
            transport=httpx2.MockTransport(_unreachable),
        )
    assert "pw-ABCD" not in str(error.value) and "[redacted]@kite.example.com" in str(error.value)


def test_the_selftest_fails_on_a_refusal(capsys, monkeypatch, base_env):
    for key, value in base_env.items():
        monkeypatch.setenv(key, value)

    async def refused(url, payload, **_):
        return 403

    monkeypatch.setattr(selftest, "send", refused)
    code, out = run(capsys, "selftest", "--url", "https://kite.example.com/postback")
    assert code == 1 and "403" in out.out


def _unreachable(request: httpx2.Request) -> httpx2.Response:
    raise httpx2.ConnectError("connection refused", request=request)


async def test_the_selftest_reports_a_url_it_cannot_reach():
    with pytest.raises(KiteMCPError, match="connection refused"):
        await selftest.send(
            "https://kite.example.com/postback",
            selftest.build_payload(API_SECRET),
            transport=httpx2.MockTransport(_unreachable),
        )
    with pytest.raises(KiteMCPError, match="cannot post"):
        await selftest.send("kite.example.com/postback", selftest.build_payload(API_SECRET))  # no scheme


def test_the_selftest_command_fails_on_a_url_it_cannot_reach(capsys, monkeypatch, base_env):
    for key, value in base_env.items():
        monkeypatch.setenv(key, value)
    real_send = selftest.send

    async def unreachable(url, payload, **_):
        return await real_send(url, payload, transport=httpx2.MockTransport(_unreachable))

    monkeypatch.setattr(selftest, "send", unreachable)
    code, out = run(capsys, "selftest", "--url", "https://kite.example.com/postback")
    assert code == 1 and "connection refused" in out.err and "Traceback" not in out.err
    assert API_SECRET not in out.out + out.err


def test_the_selftest_needs_the_api_secret(capsys, monkeypatch, base_env):
    for key, value in base_env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("KITE_API_SECRET")
    code, out = run(capsys, "selftest", "--url", "https://kite.example.com/postback")
    assert code == 2 and "KITE_API_SECRET" in out.err
    assert "it is needed to sign the selftest" in out.err and "log in" not in out.err


class Listener:
    """A tiny HTTP/1.1 server on loopback that answers 200 and keeps each request's first line: posted to directly it
    reads "POST /postback HTTP/1.1"; through a proxy, "POST http://host/postback HTTP/1.1"."""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.server: asyncio.Server | None = None

    async def __aenter__(self) -> Listener:
        self.server = await asyncio.start_server(self._answer, "127.0.0.1", 0)
        return self

    async def __aexit__(self, *exc: object) -> None:
        assert self.server is not None
        self.server.close()
        await self.server.wait_closed()

    @property
    def port(self) -> int:
        assert self.server is not None
        return self.server.sockets[0].getsockname()[1]

    async def _answer(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = (await reader.readuntil(b"\r\n\r\n")).decode("latin-1")
        self.lines.append(head.split("\r\n", 1)[0])
        length = next(
            (int(h.split(":", 1)[1]) for h in head.split("\r\n") if h.lower().startswith("content-length:")), 0
        )
        await reader.readexactly(length)
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()


def _closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]  # nothing listens there once the socket is closed


@pytest.fixture
def proxy_env(monkeypatch):
    """No proxy settings but the ones a test sets (urllib reads both cases; the lower-case one wins)."""
    for name in ("http_proxy", "https_proxy", "all_proxy", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.upper(), raising=False)
    return monkeypatch


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost"])
async def test_a_loopback_selftest_goes_past_the_proxy_settings(proxy_env, host):
    """The default URL is this server on loopback: a proxy in the environment (with no NO_PROXY for loopback) must not
    take the request, as it cannot reach this host's loopback."""
    proxy_env.setenv("HTTP_PROXY", f"http://127.0.0.1:{_closed_port()}")
    proxy_env.setenv("ALL_PROXY", f"http://127.0.0.1:{_closed_port()}")
    async with Listener() as server:
        status = await selftest.send(f"http://{host}:{server.port}/postback", selftest.build_payload(API_SECRET))
    assert status == 200 and server.lines == ["POST /postback HTTP/1.1"]


async def test_another_selftest_url_still_goes_through_the_proxy(proxy_env):
    async with Listener() as proxy:
        proxy_env.setenv("HTTP_PROXY", f"http://127.0.0.1:{proxy.port}")
        status = await selftest.send("http://kite.example.com/postback", selftest.build_payload(API_SECRET))
    assert status == 200 and proxy.lines == ["POST http://kite.example.com/postback HTTP/1.1"]


@pytest.mark.parametrize(
    ("url", "loopback"),
    [
        ("http://127.0.0.1:8765/postback", True),
        ("http://127.1.2.3:8765/postback", True),
        ("http://localhost:8765/postback", True),
        ("http://LOCALHOST/postback", True),
        ("http://[::1]:8765/postback", True),
        ("http://a:b@127.0.0.1/postback", True),
        ("https://kite.example.com/postback", False),
        ("http://10.0.0.1/postback", False),
        ("http://localhost.example.com/postback", False),
        ("kite.example.com/postback", False),
        ("http://[::1/postback", False),  # not a URL: httpx2 says so
    ],
)
def test_which_urls_are_loopback(url, loopback):
    assert selftest.is_loopback(url) is loopback


@pytest.mark.parametrize(
    ("host", "url"),
    [
        ("127.0.0.1", "http://127.0.0.1:8765/postback"),
        ("0.0.0.0", "http://127.0.0.1:8765/postback"),
        ("::", "http://127.0.0.1:8765/postback"),
        ("::1", "http://[::1]:8765/postback"),
        ("[::1]", "http://[::1]:8765/postback"),
        ("[::]", "http://127.0.0.1:8765/postback"),
        ("localhost", "http://localhost:8765/postback"),
    ],
)
def test_the_default_selftest_url(settings_factory, host, url):
    assert selftest.default_url(settings_factory(KITE_MCP_HOST=host, KITE_MCP_POSTBACK_PATH="/postback")) == url


def test_the_default_selftest_url_needs_the_postback_path(settings_factory):
    with pytest.raises(ConfigError, match="--url"):
        selftest.default_url(settings_factory())


# --- `status` and `tools` read; they never build the event service ------------------------------------------------


@pytest.fixture
def no_service(monkeypatch, base_env):
    """KITE_MCP_EVENTS=true in the environment, and an EventService that fails the test if anything builds one."""
    for key, value in {**base_env, **SERVICE}.items():
        monkeypatch.setenv(key, value)

    def refuse(*_, **__):
        raise AssertionError("the event service was built")

    monkeypatch.setattr("kite_connect_mcp.events.service.EventService", refuse)
    return base_env


def _store_path(base_env):
    return Path(base_env["KITE_MCP_STATE_DIR"]) / "events.sqlite3"


def _spy_aclose(monkeypatch):
    calls = []
    real = Runtime.aclose

    async def aclose(self):
        calls.append(self)
        await real(self)

    monkeypatch.setattr(Runtime, "aclose", aclose)
    return calls


def test_status_does_not_build_the_event_service_and_closes_what_it_opened(no_service, capsys, monkeypatch):
    closed = _spy_aclose(monkeypatch)
    assert cli.main(["status"]) == 0
    assert json.loads(capsys.readouterr().out)["auth_mode"] == "managed"
    assert len(closed) == 1 and not _store_path(no_service).exists()


def test_status_verify_closes_the_client_in_its_own_loop(no_service, capsys, monkeypatch, fake_kite):
    real_build = server_module.build_runtime
    monkeypatch.setattr(
        server_module,
        "build_runtime",
        lambda settings, **kw: real_build(settings, transport=fake_kite.transport(), clock=lambda: FIXED_NOW, **kw),
    )
    fake_kite.on("GET", "/user/profile", {"user_id": "AB1234"})
    closed = _spy_aclose(monkeypatch)
    assert cli.main(["status", "--verify"]) == 0
    assert json.loads(capsys.readouterr().out)["live_check"] == {"verified": True, "user_id": "AB1234"}
    assert len(closed) == 1 and closed[0].client._http.is_closed
    assert fake_kite.writes() == []


async def test_login_and_logout_do_not_build_the_event_service(no_service, settings_factory, fake_kite, capsys):
    settings = settings_factory(**SERVICE)
    assert await cli_login(settings, request_token=None, listen=False, transport=fake_kite.transport()) == 0
    settings.token_file.unlink()  # nothing to end at Kite: the logout makes no call
    assert await cli_logout(settings, yes=True, transport=fake_kite.transport()) == 0
    assert "There was no stored token" in capsys.readouterr().out
    assert not _store_path(no_service).exists()
    assert fake_kite.requests == []


def test_tools_does_not_build_the_event_service(no_service, capsys):
    assert cli.main(["tools"]) == 0
    out = capsys.readouterr().out
    assert "get_order_events" in out and "(hidden: no event store at" in out  # no store yet: hidden, and not created
    assert not _store_path(no_service).exists()


def test_tools_lists_the_events_tools_from_an_existing_store_and_closes_it(no_service, capsys, monkeypatch):
    EventStore(_store_path(no_service)).close()
    closed = []
    real_close = EventStore.close
    monkeypatch.setattr(EventStore, "close", lambda self: (closed.append(self.readonly), real_close(self)))
    assert cli.main(["tools"]) == 0
    listed = {line.split()[0]: line for line in capsys.readouterr().out.splitlines()}
    assert "hidden" not in listed["get_order_events"] and "hidden" not in listed["get_event_status"]
    assert closed == [True]  # the read-only store the events tools opened
