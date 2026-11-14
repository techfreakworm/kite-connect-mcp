"""The account pin by hash: one formula, one check (constant time) for the login and the postbacks, and a digest
prefix instead of the user id wherever the server names the account while the hash pin is set."""

from __future__ import annotations

import hashlib
import json

import pytest
from mcp import Client

import kite_connect_mcp.server as server_module
from conftest import ACCESS_TOKEN, FIXED_NOW, tool_json
from kite_connect_mcp import cli
from kite_connect_mcp.auth import account
from kite_connect_mcp.auth.account import AccountPin, user_id_digest
from kite_connect_mcp.auth.login import cli_login
from kite_connect_mcp.config import unknown_variables
from kite_connect_mcp.errors import ConfigError
from kite_login import NEW_TOKEN, REQUEST_TOKEN, USER_ID, KiteSession, callback_params, state_of

AB1234_SHA256 = "f08b6edab3b27aa05f3ae28397a6c1e21fe023484e0390ed6dd69a991cf190ae"  # sha256(b"AB1234")
HASH_PIN = {"KITE_MCP_ALLOWED_USER_ID_SHA256": AB1234_SHA256}


def test_the_digest_is_sha256_of_the_trimmed_upper_case_id_in_utf8():
    assert user_id_digest("AB1234") == AB1234_SHA256 == hashlib.sha256(b"AB1234").hexdigest()
    assert user_id_digest(" ab1234\n") == AB1234_SHA256


def test_the_setting_is_known_kept_in_lower_case_and_hidden(settings_factory):
    assert unknown_variables({"KITE_MCP_ALLOWED_USER_ID_SHA256": "x"}) == []
    s = settings_factory(KITE_MCP_ALLOWED_USER_ID_SHA256=AB1234_SHA256.upper())
    assert s.allowed_user_id_sha256 == AB1234_SHA256
    assert s.redacted()["allowed_user_id_sha256"] == "set" and AB1234_SHA256 not in repr(s)


@pytest.mark.parametrize("raw", ["AB1234", AB1234_SHA256[:63], AB1234_SHA256 + "0", "g" * 64])
def test_a_value_that_is_not_a_digest_is_refused_and_never_echoed(settings_factory, raw):
    with pytest.raises(ConfigError, match="KITE_MCP_ALLOWED_USER_ID_SHA256") as info:
        settings_factory(KITE_MCP_ALLOWED_USER_ID_SHA256=raw)
    assert raw not in str(info.value)  # a plain id put there by mistake must not reach a log


def test_both_pins_must_name_the_same_account(settings_factory):
    pin = AccountPin.from_settings(settings_factory(KITE_MCP_ALLOWED_USER_ID="ab1234", **HASH_PIN))
    assert pin.allows("AB1234")
    # with both set the hash pin wins: the account is still named only by its digest prefix
    assert pin.by_hash and pin.setting == "KITE_MCP_ALLOWED_USER_ID_SHA256"
    assert pin.label("AB1234") == {"user_id_sha256_prefix": AB1234_SHA256[:12]}
    assert pin.describe("AB1234") == f"sha256:{AB1234_SHA256[:12]}"
    with pytest.raises(ConfigError, match="different accounts") as info:
        settings_factory(KITE_MCP_ALLOWED_USER_ID="ZZ9999", **HASH_PIN)
    assert "ZZ9999" not in str(info.value)


@pytest.mark.parametrize(
    ("env", "by_hash"),
    [({"KITE_MCP_ALLOWED_USER_ID": "ab1234"}, False), (HASH_PIN, True)],
)
def test_one_check_for_either_pin(settings_factory, env, by_hash):
    pin = AccountPin.from_settings(settings_factory(**env))
    assert pin.pinned and pin.by_hash is by_hash
    assert pin.allows("AB1234") and pin.allows(" ab1234 ")
    for other in ("ZZ9999", "", "  ", None, 5, ["AB1234"]):
        assert not pin.allows(other), other


@pytest.mark.parametrize("env", [{"KITE_MCP_ALLOWED_USER_ID": "AB1234"}, HASH_PIN])
def test_the_pin_keeps_its_digest_out_of_its_repr(settings_factory, env):
    pin = AccountPin.from_settings(settings_factory(**env))
    assert pin.digest == AB1234_SHA256  # the check still has it
    assert AB1234_SHA256 not in repr(pin) and AB1234_SHA256[:12] not in str(pin) and USER_ID not in repr(pin)


def test_without_a_pin_any_account_is_allowed(settings_factory):
    pin = AccountPin.from_settings(settings_factory())
    assert not pin.pinned and pin.allows("ZZ9999") and pin.allows(None)
    assert pin.label("AB1234") == {"user_id": "AB1234"} and pin.describe("AB1234") == "AB1234"


def test_the_check_compares_digests_in_constant_time(settings_factory, monkeypatch):
    seen = []
    real = account.hmac.compare_digest
    monkeypatch.setattr(account.hmac, "compare_digest", lambda a, b: seen.append((a, b)) or real(a, b))
    pin = AccountPin.from_settings(settings_factory(KITE_MCP_ALLOWED_USER_ID="AB1234"))
    assert pin.allows("AB1234") and not pin.allows("ZZ9999")
    assert seen == [(AB1234_SHA256, AB1234_SHA256), (user_id_digest("ZZ9999"), AB1234_SHA256)]


def test_with_the_hash_pin_an_account_is_named_by_a_digest_prefix(settings_factory):
    pin = AccountPin.from_settings(settings_factory(**HASH_PIN))
    assert pin.label("ab1234") == {"user_id_sha256_prefix": AB1234_SHA256[:12]}
    for odd in (None, 5, "", "  ", ["AB1234"]):  # Kite's answer passed on as it came: no prefix, and no error
        assert pin.label(odd) == {"user_id_sha256_prefix": None}, odd
    assert pin.describe("AB1234") == f"sha256:{AB1234_SHA256[:12]}"


async def test_a_hash_pinned_login_audits_only_the_digest_prefix(runtime_factory, fake_kite):
    KiteSession(fake_kite)
    rt = runtime_factory(**HASH_PIN)
    url, _ = await rt.login.issue_link()
    assert (await rt.login.handle_callback(callback_params(state_of(url)))).ok
    (ok,) = [e for e in rt.audit.read() if e["event"] == "login_ok"]
    assert ok["user_id_sha256_prefix"] == AB1234_SHA256[:12] and "user_id" not in ok
    assert USER_ID not in json.dumps(rt.audit.read())


async def test_a_hash_pinned_login_by_another_account_is_ended_and_refused(runtime_factory, fake_kite):
    kite = KiteSession(fake_kite, user_id="ZZ9999")
    rt = runtime_factory(**HASH_PIN)
    before = rt.settings.token_file.read_text()
    result = await rt.login.complete(REQUEST_TOKEN)
    assert (result.ok, result.reason) == (False, "account_mismatch")
    assert "KITE_MCP_ALLOWED_USER_ID_SHA256" in result.message and "ZZ9999" not in result.message
    assert kite.logouts and kite.logouts[0]["access_token"] == NEW_TOKEN  # that session's own token only
    assert rt.settings.token_file.read_text() == before and rt.tokens.current().access_token == ACCESS_TOKEN
    audit = rt.audit.read()
    assert [e["reason"] for e in audit if e["event"] == "login_refused"] == ["account_mismatch"]
    assert "ZZ9999" not in json.dumps(audit)  # the other account is not named either


def test_the_postback_check_uses_the_hash_pin(service_factory, tmp_path):
    svc, _ = service_factory(tmp_path, **HASH_PIN)
    assert svc.user_allowed("AB1234") and svc.user_allowed(" ab1234 ")
    assert not svc.user_allowed("ZZ9999") and not svc.user_allowed(None)


async def test_tool_results_name_the_account_by_its_digest_prefix(mcp_factory, fake_kite):
    KiteSession(fake_kite)
    fake_kite.on("GET", "/user/profile", {"user_id": USER_ID, "user_name": "Test Person"})
    server, _, _ = mcp_factory(**HASH_PIN)
    async with Client(server) as client:
        done = tool_json(await client.call_tool("complete_login", {"request_token": REQUEST_TOKEN}))
        verify = tool_json(await client.call_tool("session_status", {"verify": True}))["verify"]
    assert done["user_id_sha256_prefix"] == AB1234_SHA256[:12] and "user_id" not in done
    assert verify == {"ok": True, "user_id_sha256_prefix": AB1234_SHA256[:12]}


async def test_the_login_command_names_the_account_by_its_digest_prefix(settings_factory, fake_kite, capsys):
    KiteSession(fake_kite)
    settings = settings_factory(**HASH_PIN)
    assert await cli_login(settings, request_token=REQUEST_TOKEN, listen=False, transport=fake_kite.transport()) == 0
    out, err = capsys.readouterr()
    assert f"Logged in as sha256:{AB1234_SHA256[:12]}" in out and USER_ID not in out + err


def test_status_verify_names_the_account_by_its_digest_prefix(cli_env, capsys, monkeypatch, fake_kite):
    real_build = server_module.build_runtime
    monkeypatch.setattr(
        server_module,
        "build_runtime",
        lambda settings, **kw: real_build(settings, transport=fake_kite.transport(), clock=lambda: FIXED_NOW, **kw),
    )
    monkeypatch.setenv("KITE_MCP_ALLOWED_USER_ID_SHA256", AB1234_SHA256)
    fake_kite.on("GET", "/user/profile", {"user_id": USER_ID})
    assert cli.main(["status", "--verify"]) == 0
    out, err = capsys.readouterr()
    assert json.loads(out)["live_check"] == {"verified": True, "user_id_sha256_prefix": AB1234_SHA256[:12]}
    assert USER_ID not in out + err


@pytest.mark.parametrize("profile", [[USER_ID], "AB1234", {"user_id": 5}])
def test_status_verify_survives_a_profile_that_is_not_the_expected_object(
    cli_env, capsys, monkeypatch, fake_kite, profile
):
    real_build = server_module.build_runtime
    monkeypatch.setattr(
        server_module,
        "build_runtime",
        lambda settings, **kw: real_build(settings, transport=fake_kite.transport(), clock=lambda: FIXED_NOW, **kw),
    )
    monkeypatch.setenv("KITE_MCP_ALLOWED_USER_ID_SHA256", AB1234_SHA256)
    fake_kite.on("GET", "/user/profile", profile)
    assert cli.main(["status", "--verify"]) == 0
    out, _ = capsys.readouterr()
    assert json.loads(out)["live_check"] == {"verified": True, "user_id_sha256_prefix": None}


async def test_the_login_tool_notes_a_missing_pin_by_either_setting(mcp_factory):
    open_server, _, _ = mcp_factory()
    hashed, _, _ = mcp_factory(**HASH_PIN)
    async with Client(open_server) as client:
        notes = tool_json(await client.call_tool("login", {"force": True}))["notes"]
    async with Client(hashed) as client:
        pinned = tool_json(await client.call_tool("login", {"force": True}))
    (note,) = [n for n in notes if "pin" in n]
    assert "KITE_MCP_ALLOWED_USER_ID or KITE_MCP_ALLOWED_USER_ID_SHA256" in note and "any Kite account" in note
    assert "notes" not in pinned and "login_url" in pinned
