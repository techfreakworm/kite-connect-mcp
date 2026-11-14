"""The login's session events: `login` once a new token is stored, `login_refused` once another account's login is
refused, `login_failed` once per link when a real login on it did not complete. The callback's noise never becomes an
event, and no event holds a user id, a token, a state, a link or Kite's own text."""

from __future__ import annotations

import datetime as dt
import json
import logging

import pytest

from conftest import ACCESS_TOKEN, BEARER
from events_helpers import PY, Clock, write_consumers
from kite_connect_mcp.auth.loginstate import digest_of
from kite_connect_mcp.auth.tokenstore import TokenFile
from kite_connect_mcp.events.model import LOGIN_FAILURES, login_event, login_failed_event, login_refused_event
from kite_connect_mcp.server import build_runtime
from kite_connect_mcp.timeutil import IST
from kite_login import NEW_TOKEN, REQUEST_TOKEN, USER_ID, KiteSession, callback_params, state_of

EVENTS = {"KITE_MCP_EVENTS": "true", "KITE_MCP_TRANSPORT": "http", "KITE_MCP_BEARER_TOKEN": BEARER}
SESSION_CONSUMER = {"name": "notify", "command": [PY, "-c", "pass"], "match": {"kinds": ["session"]}}
WRONG_REQUEST_TOKEN = "WrongRequestToken000000001"  # KiteSession refuses it, as Kite refuses a used or stale one
KITES_WORDS = "Token is invalid"  # the start of KiteSession's refusal message: never in an event


@pytest.fixture
def events_rt(runtime_factory, tmp_path):
    def make(**env):
        consumers = write_consumers(tmp_path, [SESSION_CONSUMER])
        return runtime_factory(**EVENTS, KITE_MCP_EVENTS_CONSUMERS_FILE=str(consumers), **env)

    return make


def no_secret_in(event, *secrets):
    text = json.dumps(event)
    for secret in (NEW_TOKEN, ACCESS_TOKEN, REQUEST_TOKEN, USER_ID, "ZZ9999", "https://", KITES_WORDS, *secrets):
        assert secret not in text, secret


async def _no_sleep(_: float) -> None:
    return None


async def test_a_login_through_the_callback_raises_one_login_event(events_rt, fake_kite):
    KiteSession(fake_kite)
    rt = events_rt()
    url, _ = await rt.login.issue_link()
    state = state_of(url)
    assert (await rt.login.handle_callback(callback_params(state))).ok
    (event,) = rt.events.store.events()
    assert (event["kind"], event["session"], event["source"]) == ("session", "login", "service")
    assert event["logged_in_at"] == "2026-11-04T10:00:00+05:30" and event["expires_at"] == "2026-11-05T06:00:00+05:30"
    assert event["text"] == "Kite login completed: the session is valid until 06:00 IST tomorrow."
    no_secret_in(event, state)
    assert rt.events.store.summary(["notify"])["consumers"]["notify"]["pending"] == 1
    assert [r for r in fake_kite.requests if r.url.path.startswith("/orders")] == []


async def test_a_pasted_request_token_raises_it_too(events_rt, fake_kite):
    KiteSession(fake_kite)
    rt = events_rt()
    assert (await rt.login.complete(REQUEST_TOKEN)).ok
    assert [e["session"] for e in rt.events.store.events()] == ["login"]


@pytest.mark.parametrize(("logout_error", "ended"), [(None, True), ((500, "GeneralException"), False)])
async def test_another_accounts_login_raises_login_refused(events_rt, fake_kite, logout_error, ended):
    kite = KiteSession(fake_kite, user_id="ZZ9999")
    kite.logout_error = logout_error
    rt = events_rt(KITE_MCP_ALLOWED_USER_ID=USER_ID)
    state = state_of((await rt.login.issue_link())[0])
    assert (await rt.login.handle_callback(callback_params(state))).reason == "account_mismatch"
    (event,) = rt.events.store.events()
    assert event["session"] == "login_refused" and event["session_ended"] is ended
    assert ("lapses at 06:00 IST" in event["text"]) is not ended
    no_secret_in(event, state)


async def test_a_failed_exchange_on_a_valid_link_raises_login_failed_once_per_link(events_rt, fake_kite):
    KiteSession(fake_kite)
    rt = events_rt()
    state = state_of((await rt.login.issue_link())[0])
    for _ in range(2):  # a failure releases the link; a second failure on it is the same event
        result = await rt.login.handle_callback(callback_params(state, WRONG_REQUEST_TOKEN))
        assert result.reason == "exchange_failed"
    (event,) = rt.events.store.events()
    assert (event["session"], event["reason"]) == ("login_failed", "exchange_failed")
    assert event["text"] == (
        "A Kite login did not complete: Kite did not accept the login. Tap the link again or ask for a new one."
    )
    seeded = login_failed_event(seed=digest_of(state), reason="exchange_failed", received_at=dt.datetime.now(IST))
    assert event["event_id"] == seeded["event_id"]  # seeded with the digest of the link's state
    no_secret_in(event, state, WRONG_REQUEST_TOKEN, digest_of(state))
    assert rt.events.store.summary(["notify"])["consumers"]["notify"]["pending"] == 1


async def test_a_token_that_cannot_be_stored_raises_login_failed(events_rt, fake_kite, monkeypatch):
    KiteSession(fake_kite)
    rt = events_rt()
    state = state_of((await rt.login.issue_link())[0])

    def refuse(self, record):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(TokenFile, "write", refuse)
    assert (await rt.login.handle_callback(callback_params(state))).reason == "token_not_stored"
    (event,) = rt.events.store.events()
    assert (event["session"], event["reason"]) == ("login_failed", "token_not_stored")
    assert event["text"] == "A Kite login did not complete: the token could not be stored."
    no_secret_in(event, state, "Permission denied")


async def test_a_pasted_request_token_that_fails_raises_nothing(events_rt, fake_kite):
    KiteSession(fake_kite)
    rt = events_rt()
    assert (await rt.login.complete(WRONG_REQUEST_TOKEN)).reason == "exchange_failed"  # no state: no link of ours
    assert rt.events.store.events() == []


async def test_the_callbacks_noise_never_becomes_an_event(events_rt, fake_kite):
    KiteSession(fake_kite)
    rt = events_rt()
    state = state_of((await rt.login.issue_link())[0])
    await rt.login.handle_callback({"status": "success"})  # malformed
    await rt.login.handle_callback(callback_params(state, "not a token!"))  # a request token of the wrong shape
    await rt.login.handle_callback(callback_params("UnknownState00000000000000001"))  # a state never issued
    rt.login.record_refusal("rate_limited", source="callback", client="203.0.113.7")
    rt.login.record_refusal("error", source="callback", client="203.0.113.7", error="RuntimeError")
    assert rt.events.store.events() == []
    assert (await rt.login.handle_callback(callback_params(state))).ok
    assert (await rt.login.handle_callback(callback_params(state))).reason == "bad_state"  # a replay
    replay = await rt.login.handle_callback(callback_params(state, WRONG_REQUEST_TOKEN))  # a used link: no exchange
    assert replay.reason == "bad_state"
    assert [e["session"] for e in rt.events.store.events()] == ["login"]


async def test_an_expired_link_raises_nothing(settings_factory, fake_kite, tmp_path):
    kite = KiteSession(fake_kite)
    clock = Clock()
    consumers = write_consumers(tmp_path, [SESSION_CONSUMER])
    settings = settings_factory(**EVENTS, KITE_MCP_EVENTS_CONSUMERS_FILE=str(consumers))
    rt = build_runtime(settings, transport=fake_kite.transport(), clock=clock, sleep=_no_sleep)
    try:
        url, expires_at = await rt.login.issue_link()
        clock.now = expires_at + dt.timedelta(seconds=1)
        assert (await rt.login.handle_callback(callback_params(state_of(url)))).reason == "bad_state"
        assert kite.exchanges == []  # an expired link never reaches Kite
        assert rt.events.store.events() == []
    finally:
        await rt.aclose()


async def test_a_store_that_cannot_write_does_not_undo_the_login(events_rt, fake_kite, caplog):
    KiteSession(fake_kite)
    rt = events_rt()
    rt.events.store.close()
    with caplog.at_level(logging.WARNING):
        assert (await rt.login.complete(REQUEST_TOKEN)).ok
    assert rt.tokens.current().access_token == NEW_TOKEN
    assert "could not record the login session event" in caplog.text and NEW_TOKEN not in caplog.text


@pytest.mark.parametrize("what", ["login_refused", "login_failed"])
async def test_a_store_that_cannot_write_does_not_change_a_refusal_or_a_failure(events_rt, fake_kite, caplog, what):
    kite = KiteSession(fake_kite, user_id="ZZ9999" if what == "login_refused" else USER_ID)
    rt = events_rt(KITE_MCP_ALLOWED_USER_ID=USER_ID)
    state = state_of((await rt.login.issue_link())[0])
    rt.events.store.close()
    request_token = REQUEST_TOKEN if what == "login_refused" else WRONG_REQUEST_TOKEN
    with caplog.at_level(logging.WARNING):
        result = await rt.login.handle_callback(callback_params(state, request_token))
    if what == "login_refused":
        assert (result.ok, result.reason) == (False, "account_mismatch")
        assert "that new session was ended and nothing was stored" in result.message
        assert len(kite.logouts) == 1
    else:
        assert (result.ok, result.reason) == (False, "exchange_failed")
        assert (await rt.login.handle_callback(callback_params(state))).ok  # the link was released all the same
    assert rt.tokens.current().access_token == (ACCESS_TOKEN if what == "login_refused" else NEW_TOKEN)
    assert f"could not record the {what} session event" in caplog.text
    for secret in (NEW_TOKEN, REQUEST_TOKEN, WRONG_REQUEST_TOKEN, state, digest_of(state)):
        assert secret not in caplog.text, secret


async def test_without_the_events_a_login_works_as_before(runtime_factory, fake_kite):
    KiteSession(fake_kite)
    rt = runtime_factory()
    assert rt.login.session_notes is None and (await rt.login.complete(REQUEST_TOKEN)).ok


@pytest.mark.parametrize(
    ("logged_in", "expires", "words"),
    [
        (
            dt.datetime(2026, 11, 16, 7, 35, tzinfo=IST),
            dt.datetime(2026, 11, 17, 6, 0, tzinfo=IST),
            "06:00 IST tomorrow",
        ),
        (dt.datetime(2026, 11, 16, 5, 30, tzinfo=IST), dt.datetime(2026, 11, 16, 6, 0, tzinfo=IST), "06:00 IST today"),
    ],
)
def test_the_session_event_texts(logged_in, expires, words):
    body = login_event(seed="s", logged_in_at=logged_in, expires_at=expires, received_at=logged_in)
    assert body["text"] == f"Kite login completed: the session is valid until {words}."
    assert (
        login_event(seed="s", logged_in_at=logged_in, expires_at=expires, received_at=expires)["event_id"]
        == (body["event_id"])
    )  # the seed alone makes the id
    refused = login_refused_event(seed="t", session_ended=True, received_at=logged_in)
    assert refused["session"] == "login_refused" and refused["event_id"] != body["event_id"]
    assert LOGIN_FAILURES == ("exchange_failed", "token_not_stored")
    failed = login_failed_event(seed="s", reason="token_not_stored", received_at=logged_in)
    assert failed["session"] == "login_failed" and failed["event_id"] not in (body["event_id"], refused["event_id"])
    with pytest.raises(ValueError):
        login_failed_event(seed="s", reason="Kite said no", received_at=logged_in)  # a fixed vocabulary only
