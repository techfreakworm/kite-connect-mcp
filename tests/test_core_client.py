"""The HTTP client's door, error mapping, retries and redaction; the rate limiter; the token store."""

from __future__ import annotations

import datetime as dt
import gzip
import json
from pathlib import Path

import httpx2
import pytest

from conftest import ACCESS_TOKEN, API_KEY, API_SECRET, FIXED_NOW, write_token_file
from kite_connect_mcp.auth.tokenstore import TokenFile, TokenProvider, TokenRecord, parse_token_file
from kite_connect_mcp.client.http import _TICKET_ISSUER, ENDPOINTS, WriteTicket, checksum, find_endpoint, payload_digest
from kite_connect_mcp.client.ratelimit import RateLimiter, SlidingWindowLimiter, Window
from kite_connect_mcp.config import Settings
from kite_connect_mcp.errors import (
    AuthorisationRequired,
    ConfigError,
    ForbiddenCall,
    InputError,
    MarginError,
    NetworkError,
    NotLoggedIn,
    OutcomeUnknown,
    RateLimited,
    TokenError,
)
from kite_connect_mcp.timeutil import IST, next_token_reset

# --- the door -------------------------------------------------------------------------------------------------------


async def test_reads_carry_kite_headers(runtime_factory, fake_kite):
    fake_kite.on("GET", "/user/profile", {"user_id": "AB1234"})
    rt = runtime_factory()
    assert await rt.client.get("/user/profile") == {"user_id": "AB1234"}
    request = fake_kite.calls("GET", "/user/profile")[0]
    assert request.headers["X-Kite-Version"] == "3"
    assert request.headers["Authorization"] == f"token {API_KEY}:{ACCESS_TOKEN}"
    assert request.headers["User-Agent"].startswith("kite-connect-mcp/")


async def test_unknown_endpoint_is_refused_before_io(runtime_factory, fake_kite):
    rt = runtime_factory()
    with pytest.raises(ForbiddenCall):
        await rt.client.get("/user/secret-stuff")
    assert fake_kite.requests == []


async def test_write_without_ticket_is_refused(runtime_factory, fake_kite):
    rt = runtime_factory()
    with pytest.raises(ForbiddenCall):
        await rt.client.request("POST", "/orders/regular", data={"tradingsymbol": "INFY"})
    assert fake_kite.requests == []


async def test_write_ticket_must_match_and_is_single_use(runtime_factory, fake_kite):
    fake_kite.on("POST", "/orders/regular", {"order_id": "1"})
    rt = runtime_factory()
    data = {"tradingsymbol": "INFY", "quantity": 1}
    ticket = WriteTicket(
        "POST", "/orders/regular", payload_digest("POST", "/orders/regular", None, data, None), issuer=_TICKET_ISSUER
    )
    with pytest.raises(ForbiddenCall):  # a different payload
        await rt.client.write("POST", "/orders/regular", ticket, data={"tradingsymbol": "TCS", "quantity": 1})
    assert await rt.client.write("POST", "/orders/regular", ticket, data=data) == {"order_id": "1"}
    with pytest.raises(ForbiddenCall):  # used up
        await rt.client.write("POST", "/orders/regular", ticket, data=data)
    assert len(fake_kite.writes()) == 1


async def test_forged_ticket_is_refused(runtime_factory, fake_kite):
    rt = runtime_factory()
    data = {"tradingsymbol": "INFY"}
    forged = WriteTicket(
        "POST", "/orders/regular", payload_digest("POST", "/orders/regular", None, data, None), issuer=object()
    )
    with pytest.raises(ForbiddenCall):
        await rt.client.write("POST", "/orders/regular", forged, data=data)
    assert fake_kite.requests == []


async def test_read_only_refuses_writes_even_with_ticket(runtime_factory, fake_kite):
    rt = runtime_factory(KITE_MCP_READ_ONLY="true")
    data = {"x": 1}
    ticket = WriteTicket(
        "POST", "/gtt/triggers", payload_digest("POST", "/gtt/triggers", None, data, None), issuer=_TICKET_ISSUER
    )
    with pytest.raises(ForbiddenCall):
        await rt.client.write("POST", "/gtt/triggers", ticket, data=data)
    assert fake_kite.requests == []


async def test_calc_posts_are_allowed_in_read_only(runtime_factory, fake_kite):
    fake_kite.on("POST", "/margins/orders", [{"total": 10}])
    rt = runtime_factory(KITE_MCP_READ_ONLY="true")
    assert await rt.client.calc("/margins/orders", [{"exchange": "NSE"}]) == [{"total": 10}]


async def test_external_mode_never_sends_session_calls(runtime_factory, fake_kite):
    rt = runtime_factory(KITE_MCP_AUTH_MODE="external")
    with pytest.raises(ForbiddenCall):
        await rt.client.request("POST", "/session/token", data={"api_key": API_KEY})
    with pytest.raises(ForbiddenCall):
        await rt.client.request("DELETE", "/session/token", params={"api_key": API_KEY}, allow_session_call=True)
    assert fake_kite.requests == []


async def test_logout_only_from_the_logout_flow(runtime_factory, fake_kite):
    rt = runtime_factory()
    with pytest.raises(ForbiddenCall):
        await rt.client.request("DELETE", "/session/token", params={"api_key": API_KEY})
    assert fake_kite.requests == []


def test_every_write_endpoint_is_classified():
    for endpoint in ENDPOINTS:
        if endpoint.method in ("PUT", "DELETE") or (
            endpoint.method == "POST" and endpoint.kind not in ("calc", "auth")
        ):
            assert endpoint.kind in ("write", "auth"), endpoint
    assert find_endpoint("POST", "/orders/regular").group == "order"
    assert find_endpoint("GET", "/quote/ltp").group == "quote"
    assert find_endpoint("GET", "/instruments/historical/5633/minute").group == "historical"
    with pytest.raises(ForbiddenCall):
        find_endpoint("POST", "/orders/bracket")  # BO was discontinued; not an allowed variety


# --- answers and errors ------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "error_type", "cls"),
    [
        (403, "TokenException", TokenError),
        (400, "InputException", InputError),
        (400, "MarginException", MarginError),
        (428, "OrderException", AuthorisationRequired),
    ],
)
async def test_error_mapping(runtime_factory, fake_kite, status, error_type, cls):
    fake_kite.on("GET", "/portfolio/holdings", status=status, error_type=error_type, message="nope")
    rt = runtime_factory()
    with pytest.raises(cls) as info:
        await rt.client.get("/portfolio/holdings")
    assert info.value.status == status
    assert "nope" in str(info.value)


async def test_token_exception_marks_the_token_rejected(runtime_factory, fake_kite):
    fake_kite.on("GET", "/user/profile", status=403, error_type="TokenException", message="Incorrect api_key")
    rt = runtime_factory()
    with pytest.raises(TokenError):
        await rt.client.get("/user/profile")
    assert rt.tokens.current() is None
    assert "rejected" in rt.tokens.status()["problem"]
    with pytest.raises(NotLoggedIn):
        await rt.client.get("/user/profile")
    assert len(fake_kite.calls("GET", "/user/profile")) == 1


async def test_credentials_are_scrubbed_from_errors(runtime_factory, fake_kite):
    fake_kite.on(
        "GET",
        "/user/profile",
        status=400,
        error_type="InputException",
        message=f"bad key {API_KEY} token {ACCESS_TOKEN} secret {API_SECRET}",
    )
    rt = runtime_factory()
    with pytest.raises(InputError) as info:
        await rt.client.get("/user/profile")
    text = str(info.value)
    for secret in (API_KEY, ACCESS_TOKEN, API_SECRET):
        assert secret not in text


async def test_reads_retry_on_429_then_succeed(runtime_factory, fake_kite):
    answers = iter(
        [
            httpx2.Response(
                429, json={"status": "error", "message": "Too many requests", "error_type": "NetworkException"}
            ),
            httpx2.Response(200, json={"status": "success", "data": {"ok": 1}}),
        ]
    )
    fake_kite.on("GET", "/portfolio/positions", handler=lambda _: next(answers))
    rt = runtime_factory()
    assert await rt.client.get("/portfolio/positions") == {"ok": 1}
    assert len(fake_kite.calls("GET", "/portfolio/positions")) == 2


async def test_writes_are_never_retried(runtime_factory, fake_kite):
    fake_kite.on("POST", "/orders/regular", status=429, error_type="NetworkException", message="Too many requests")
    rt = runtime_factory()
    data = {"tradingsymbol": "INFY"}
    ticket = WriteTicket(
        "POST", "/orders/regular", payload_digest("POST", "/orders/regular", None, data, None), issuer=_TICKET_ISSUER
    )
    with pytest.raises(RateLimited):
        await rt.client.write("POST", "/orders/regular", ticket, data=data)
    assert len(fake_kite.writes()) == 1


async def test_write_timeout_is_outcome_unknown(runtime_factory, fake_kite):
    def boom(request):
        raise httpx2.ReadTimeout("slow", request=request)

    fake_kite.on("POST", "/orders/regular", handler=boom)
    rt = runtime_factory()
    data = {"tradingsymbol": "INFY"}
    ticket = WriteTicket(
        "POST", "/orders/regular", payload_digest("POST", "/orders/regular", None, data, None), issuer=_TICKET_ISSUER
    )
    with pytest.raises(OutcomeUnknown) as info:
        await rt.client.write("POST", "/orders/regular", ticket, data=data)
    assert "get_orders" in str(info.value) and "before trying again" in str(info.value)


async def test_write_connect_error_is_not_sent(runtime_factory, fake_kite):
    def refuse(request):
        raise httpx2.ConnectError("refused", request=request)

    fake_kite.on("POST", "/orders/regular", handler=refuse)
    rt = runtime_factory()
    data = {"tradingsymbol": "INFY"}
    ticket = WriteTicket(
        "POST", "/orders/regular", payload_digest("POST", "/orders/regular", None, data, None), issuer=_TICKET_ISSUER
    )
    with pytest.raises(NetworkError):
        await rt.client.write("POST", "/orders/regular", ticket, data=data)


async def test_csv_dump_gzip_and_plain(runtime_factory, fake_kite):
    csv_text = "instrument_token,tradingsymbol\n408065,INFY\n"
    fake_kite.on("GET", "/instruments", body=gzip.compress(csv_text.encode()), content_type="text/csv")
    fake_kite.on("GET", "/instruments/NSE", body=csv_text.encode(), content_type="text/csv")
    rt = runtime_factory()
    assert await rt.client.get_csv("/instruments") == csv_text
    assert await rt.client.get_csv("/instruments/NSE") == csv_text


async def test_instruments_dump_works_without_a_token(runtime_factory, fake_kite, tmp_path):
    fake_kite.on("GET", "/instruments", body=b"instrument_token\n1\n", content_type="text/csv")
    rt = runtime_factory(KITE_TOKEN_FILE=str(tmp_path / "missing.json"))
    assert (await rt.client.get_csv("/instruments")).startswith("instrument_token")
    assert "Authorization" not in fake_kite.requests[0].headers
    with pytest.raises(NotLoggedIn):
        await rt.client.get("/user/profile")


def test_checksum_matches_kite_spec():
    import hashlib

    assert checksum("k", "r", "s") == hashlib.sha256(b"krs").hexdigest()
    with pytest.raises(ConfigError):
        checksum("k", "", "s")


# --- the rate limiter ----------------------------------------------------------------------------------------------


async def test_sliding_window_never_exceeds_limit():
    now = [0.0]
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)
        now[0] += seconds

    limiter = SlidingWindowLimiter((Window(3, 1.1),), clock=lambda: now[0], sleep=sleep)
    stamps = []
    for _ in range(10):
        await limiter.acquire()
        stamps.append(now[0])
    for i in range(len(stamps)):
        in_window = [t for t in stamps if stamps[i] <= t < stamps[i] + 1.1]
        assert len(in_window) <= 3


async def test_quote_group_is_one_per_window():
    now = [0.0]

    async def sleep(seconds: float) -> None:
        now[0] += seconds

    limiter = RateLimiter(clock=lambda: now[0], sleep=sleep)
    await limiter.acquire("quote")
    waited = await limiter.acquire("quote")
    assert waited == pytest.approx(1.1)
    assert await limiter.acquire("other") == 0  # groups are independent


# --- the token store ----------------------------------------------------------------------------------------------


FAKE_TOKEN = "fake-token-0001"  # gitleaks:allow (test value)


def test_reads_the_json_token_file_format(tmp_path):
    text = json.dumps(
        {
            "access_token": FAKE_TOKEN,
            "login_date": "2026-11-04",
            "logged_in_at": "2026-11-04T06:05:00+05:30",
            "expires_at": "2026-11-05T06:00:00+05:30",
        }
    )
    record = parse_token_file(text, FIXED_NOW)
    assert record is not None
    assert record.access_token == FAKE_TOKEN
    assert record.expires_at == dt.datetime(2026, 11, 5, 6, 0, tzinfo=IST)
    assert record.valid_at(FIXED_NOW)
    assert FAKE_TOKEN not in repr(record)


def test_bare_token_file_expires_at_next_reset(tmp_path):
    record = parse_token_file("abcDEF123456\n", FIXED_NOW)
    assert record is not None and record.expires_at == next_token_reset(FIXED_NOW)


@pytest.mark.parametrize("text", ["", "{}", '{"access_token": "has space"}', "not json {", '{"access_token": 5}'])
def test_garbage_token_files_are_ignored(text):
    assert parse_token_file(text, FIXED_NOW) is None


def test_next_token_reset():
    assert next_token_reset(dt.datetime(2026, 11, 4, 5, 59, tzinfo=IST)) == dt.datetime(2026, 11, 4, 6, 0, tzinfo=IST)
    assert next_token_reset(dt.datetime(2026, 11, 4, 6, 0, tzinfo=IST)) == dt.datetime(2026, 11, 5, 6, 0, tzinfo=IST)


def test_external_token_file_is_never_written_or_removed(tmp_path):
    path = write_token_file(tmp_path / "theirs.json")
    before = path.read_bytes()
    store = TokenFile(path, writable=False)
    with pytest.raises(ConfigError):
        store.write(TokenRecord.new("othertoken123", FIXED_NOW))
    with pytest.raises(ConfigError):
        store.remove()
    assert path.read_bytes() == before


def test_token_file_is_reread_when_it_changes(tmp_path):
    path = write_token_file(tmp_path / "t.json", token="firsttoken0001")
    provider = TokenProvider(token_file=TokenFile(path, writable=False), clock=lambda: FIXED_NOW)
    assert provider.current().access_token == "firsttoken0001"
    provider.reject("firsttoken0001")
    assert provider.current() is None
    write_token_file(path, token="secondtoken002")
    assert provider.current().access_token == "secondtoken002"


def test_expired_token_is_not_used(tmp_path):
    path = write_token_file(tmp_path / "t.json", logged_in_at=FIXED_NOW - dt.timedelta(days=2))
    provider = TokenProvider(token_file=TokenFile(path, writable=False), clock=lambda: FIXED_NOW)
    assert provider.current() is None
    assert "expired" in provider.status()["problem"]


def test_managed_write_is_private_and_atomic(tmp_path):
    path = tmp_path / "dir" / "token.json"
    TokenFile(path, writable=True).write(TokenRecord.new("mytoken12345", FIXED_NOW))
    assert path.stat().st_mode & 0o777 == 0o600
    assert (path.parent.stat().st_mode & 0o777) == 0o700
    assert list(path.parent.glob(".*.tmp")) == []


# --- settings ---------------------------------------------------------------------------------------------------------


def test_defaults_are_safe(settings_factory):
    s = settings_factory()
    assert s.trading_mode == "dry_run" and not s.live_trading
    assert s.read_only is False
    assert s.live_confirmation == "token"
    assert s.host == "127.0.0.1"
    assert API_KEY not in repr(s) and API_SECRET not in repr(s)
    assert s.redacted()["api_secret"] == "set"


def test_secret_files_must_be_private(base_env, tmp_path):
    secret = tmp_path / "secret"
    secret.write_text("abc123\n")
    secret.chmod(0o644)
    env = {**base_env, "KITE_API_SECRET_FILE": str(secret)}
    env.pop("KITE_API_SECRET")
    with pytest.raises(ConfigError, match="chmod 600"):
        Settings.from_env(env)
    secret.chmod(0o600)
    assert Settings.from_env(env).api_secret == "abc123"


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"KITE_MCP_TRADING_MODE": "yolo"}, "KITE_MCP_TRADING_MODE"),
        ({"KITE_MCP_TRANSPORT": "http"}, "BEARER"),
        ({"KITE_MCP_LIVE_CONFIRMATION": "allowlist"}, "ALLOWED_INSTRUMENTS"),
        ({"KITE_MCP_MAX_ORDER_VALUE": "-5"}, "MAX_ORDER_VALUE"),
        ({"KITE_MCP_TOOLSETS": "orders,nonsense"}, "nonsense"),
        ({"KITE_MCP_BEARER_TOKEN": "short"}, "32 characters"),
    ],
)
def test_bad_settings_are_reported(settings_factory, overrides, message):
    with pytest.raises(ConfigError, match=message):
        settings_factory(**overrides)


def test_env_file_loader(tmp_path):
    from kite_connect_mcp.config import load_env_file

    path = tmp_path / ".env"
    path.write_text('# c\nexport A=1\nB="two words"\nC=3 # trailing\n')
    env = load_env_file(path, {"A": "keep"})
    assert env == {"A": "keep", "B": "two words", "C": "3"}


def test_token_paths(settings_factory, tmp_path: Path):
    s = settings_factory(KITE_TOKEN_FILE=None)
    assert s.token_file == s.state_dir / "access_token.json"


def test_an_empty_boolean_keeps_its_default(settings_factory):
    assert settings_factory(KITE_MCP_TICKER_ENABLED="").ticker_enabled is True
    assert settings_factory(KITE_MCP_READ_ONLY="  ").read_only is False


def test_public_url_needs_a_scheme(settings_factory):
    with pytest.raises(ConfigError, match="KITE_MCP_PUBLIC_URL"):
        settings_factory(KITE_MCP_PUBLIC_URL="kite.example.com")
    assert settings_factory(KITE_MCP_PUBLIC_URL="https://kite.example.com").public_url == "https://kite.example.com"


def test_unknown_variables_are_reported(base_env):
    from kite_connect_mcp.config import unknown_variables

    env = {**base_env, "KITE_MCP_TRADNG_MODE": "live", "KITE_API_SECRET_FILE": "/x", "OTHER": "1"}
    assert unknown_variables(env) == ["KITE_MCP_TRADNG_MODE"]


def test_container_secret_mounts_may_be_world_readable(base_env, tmp_path, monkeypatch):
    import kite_connect_mcp.config as config

    secret = tmp_path / "api_secret"
    secret.write_text("abc123\n")
    secret.chmod(0o444)
    env = {**base_env, "KITE_API_SECRET_FILE": str(secret)}
    env.pop("KITE_API_SECRET")
    with pytest.raises(ConfigError, match="chmod 600"):
        Settings.from_env(env)
    monkeypatch.setattr(config, "_container_secret", lambda file: True)
    assert Settings.from_env(env).api_secret == "abc123"


async def test_limiter_never_stalls_on_float_rounding():
    """Virtual time that only moves by the requested sleep: 2.2 - 1.1 is not exactly 1.1 in floating point."""
    now = [0.0]

    async def sleep(seconds: float) -> None:
        now[0] += seconds  # exactly what was asked, nothing more

    limiter = SlidingWindowLimiter((Window(10, 1.1), Window(400, 66.0)), clock=lambda: now[0], sleep=sleep)
    for _ in range(100):
        await limiter.acquire()
    assert now[0] >= 9 * 1.1 - 1e-6
