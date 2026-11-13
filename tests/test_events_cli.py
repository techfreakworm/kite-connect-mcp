"""`kite-connect-mcp events status|tail|pause|resume|retry|selftest`."""

from __future__ import annotations

import json
from pathlib import Path

import httpx2
import pytest

import kite_connect_mcp.server as server_module
from conftest import API_SECRET, BEARER, FIXED_NOW
from events_helpers import ORDER_ID, PY, filled_row, make_service, order_row
from kite_connect_mcp import cli
from kite_connect_mcp.errors import ConfigError, KiteMCPError
from kite_connect_mcp.events import selftest
from kite_connect_mcp.events.model import is_selftest_id
from kite_connect_mcp.events.store import EventStore
from kite_connect_mcp.postback import verify_checksum
from kite_connect_mcp.server import build_http_app, build_server
from kite_connect_mcp.tools._base import Runtime

NOTIFY = {"name": "notify", "command": [PY, "-c", "pass"]}
SERVICE = {"KITE_MCP_EVENTS": "true", "KITE_MCP_TRANSPORT": "http", "KITE_MCP_BEARER_TOKEN": BEARER}


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
    assert code == 0 and "2" in out.out and not env.store.is_paused("notify")
    assert env.store.summary(["notify"])["consumers"]["notify"]["skipped"] == 2
    # both deliveries are skipped now, and only a pending one can fail: a new event's delivery fails
    seq = env.ingest_order(order_row(order_id="250101000000002"), source="poll")
    assert env.store.finish_delivery("notify", seq, "failed", env.clock(), "exit 1")
    code, out = run(capsys, "retry", "notify")
    assert code == 0 and "1" in out.out
    assert env.store.summary(["notify"])["consumers"]["notify"]["pending"] == 1


def test_an_unknown_consumer_is_an_error(env, capsys):
    code, out = run(capsys, "pause", "nobody")
    assert code == 2 and "nobody" in out.err


def test_a_paused_consumer_that_is_no_longer_configured_can_be_resumed(env, capsys):
    env.store.pause("retired", env.clock())  # paused once, then removed from the consumers file
    code, out = run(capsys, "resume", "retired")
    assert code == 0 and "resumed retired" in out.out and not env.store.is_paused("retired")


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
    for args in (("status",), ("pause", "notify")):
        code, out = run(capsys, *args)
        assert code == 2 and "no event store at" in out.err
    assert not missing.exists()


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
    monkeypatch.setenv("KITE_MCP_POSTBACK_PATH", "/postback")
    code, _ = run(capsys, "selftest")
    assert code == 0 and seen["url"] == "http://127.0.0.1:8765/postback"


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


@pytest.mark.parametrize(
    ("host", "url"),
    [
        ("127.0.0.1", "http://127.0.0.1:8765/postback"),
        ("0.0.0.0", "http://127.0.0.1:8765/postback"),
        ("::", "http://127.0.0.1:8765/postback"),
        ("::1", "http://[::1]:8765/postback"),
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
