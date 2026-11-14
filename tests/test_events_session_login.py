"""The login's session events: `login` once a new token is stored, `login_refused` once another account's login is
refused, `login_failed` once per link when a real login on it did not complete. The callback's noise never becomes an
event, and no event holds a user id, a token, a state, a link or Kite's own text.

What the service sees by itself: a token stored beside it (the poll raises `login`, in managed mode), and Kite's
rejection of the token in use met first by any call (`token_rejected`, once per token; never for a logout)."""

from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
import json
import logging
import threading

import httpx2
import pytest

from conftest import ACCESS_TOKEN, BEARER, FIXED_NOW, write_token_file
from events_helpers import PY, Clock, write_consumers
from kite_connect_mcp.auth.callback import CALLBACK_RATE_LIMIT
from kite_connect_mcp.auth.loginstate import StateStore, digest_of
from kite_connect_mcp.auth.tokenstore import TokenFile, TokenRecord
from kite_connect_mcp.errors import TokenError
from kite_connect_mcp.events.model import (
    LOGIN_FAILURES,
    SESSION_TEXT,
    SESSIONS,
    _session,
    login_event,
    login_failed_event,
    login_refused_event,
    session_event,
)
from kite_connect_mcp.events.store import StoreError
from kite_connect_mcp.server import build_http_app, build_runtime
from kite_connect_mcp.timeutil import IST, next_token_reset
from kite_login import NEW_TOKEN, REQUEST_TOKEN, USER_ID, KiteSession, callback_params, state_of

EVENTS = {"KITE_MCP_EVENTS": "true", "KITE_MCP_TRANSPORT": "http", "KITE_MCP_BEARER_TOKEN": BEARER}
SESSION_CONSUMER = {"name": "notify", "command": [PY, "-c", "pass"], "match": {"kinds": ["session"]}}
WRONG_REQUEST_TOKEN = "WrongRequestToken000000001"  # KiteSession refuses it, as Kite refuses a used or stale one
KITES_WORDS = "Token is invalid"  # the start of KiteSession's refusal message: never in an event
EXCHANGE_FAILED_TEXT = (
    "A Kite login did not complete: Kite did not accept the login. Tap the link again or ask for a new one."
)


@pytest.fixture
def events_rt(runtime_factory, tmp_path):
    def make(**env):
        consumers = write_consumers(tmp_path, [SESSION_CONSUMER])
        return runtime_factory(**EVENTS, KITE_MCP_EVENTS_CONSUMERS_FILE=str(consumers), **env)

    return make


@pytest.fixture
def events_app(mcp_factory, tmp_path):
    """The HTTP app with the events on, for driving the callback route itself."""
    consumers = write_consumers(tmp_path, [SESSION_CONSUMER])
    server, _, rt = mcp_factory(**EVENTS, KITE_MCP_EVENTS_CONSUMERS_FILE=str(consumers))
    return build_http_app(server, rt), rt


def web(app) -> httpx2.AsyncClient:
    transport = httpx2.ASGITransport(app=app, client=("203.0.113.7", 40000))
    return httpx2.AsyncClient(transport=transport, base_url="http://127.0.0.1:8765")


def no_secret_in(event, *secrets):
    text = json.dumps(event)
    for secret in (NEW_TOKEN, ACCESS_TOKEN, REQUEST_TOKEN, USER_ID, "ZZ9999", "https://", KITES_WORDS, *secrets):
        assert secret not in text, secret


def no_secret_in_log(text, state):
    for secret in (NEW_TOKEN, REQUEST_TOKEN, WRONG_REQUEST_TOKEN, state, digest_of(state)):
        assert secret not in text, secret


def failed_id(state: str, reason: str = "exchange_failed") -> str:
    """The id of the login_failed event of the link with `state`: seeded with the digest of that state."""
    return login_failed_event(seed=digest_of(state), reason=reason, received_at=FIXED_NOW)["event_id"]


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


async def test_the_login_event_carries_the_stored_records_times(events_rt, fake_kite, monkeypatch):
    KiteSession(fake_kite)
    rt = events_rt()
    made = TokenRecord.new

    def new(access_token: str, logged_in_at: dt.datetime) -> TokenRecord:  # a record whose expiry is not 06:00
        record = made(access_token, logged_in_at)
        return dataclasses.replace(record, expires_at=record.expires_at - dt.timedelta(minutes=1))

    monkeypatch.setattr(TokenRecord, "new", staticmethod(new))
    result = await rt.login.complete(REQUEST_TOKEN)
    (event,) = rt.events.store.events()
    stored = rt.tokens.current()
    assert stored.access_token == NEW_TOKEN and result.expires_at == stored.expires_at
    assert event["expires_at"] == stored.expires_at.isoformat(timespec="seconds") == "2026-11-05T05:59:00+05:30"
    assert event["logged_in_at"] == stored.logged_in_at.isoformat(timespec="seconds")
    assert event["text"] == "Kite login completed: the session is valid until 05:59 IST tomorrow."


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
    assert event["text"] == EXCHANGE_FAILED_TEXT
    assert event["event_id"] == failed_id(state)
    no_secret_in(event, state, WRONG_REQUEST_TOKEN, digest_of(state))
    assert rt.events.store.summary(["notify"])["consumers"]["notify"]["pending"] == 1


async def test_a_pasted_request_token_with_a_links_state_that_fails_raises_login_failed(events_rt, fake_kite):
    KiteSession(fake_kite)
    rt = events_rt()
    state = state_of((await rt.login.issue_link())[0])
    assert (await rt.login.complete(WRONG_REQUEST_TOKEN, state)).reason == "exchange_failed"
    (event,) = rt.events.store.events()
    assert (event["session"], event["reason"]) == ("login_failed", "exchange_failed")
    assert event["event_id"] == failed_id(state)
    no_secret_in(event, state, WRONG_REQUEST_TOKEN, digest_of(state))


async def test_kite_not_answering_the_exchange_on_a_link_raises_login_failed(events_rt, fake_kite):
    kite = KiteSession(fake_kite)
    kite.no_answer_next = True
    rt = events_rt()
    state = state_of((await rt.login.issue_link())[0])
    result = await rt.login.handle_callback(callback_params(state))
    assert (result.ok, result.reason) == (False, "exchange_failed")
    assert "did not answer" in result.message  # the client's OutcomeUnknown
    (event,) = rt.events.store.events()
    assert (event["session"], event["reason"]) == ("login_failed", "exchange_failed")
    assert event["event_id"] == failed_id(state) and event["text"] == EXCHANGE_FAILED_TEXT
    no_secret_in(event, state, digest_of(state), "did not answer", "ReadTimeout", "OutcomeUnknown")
    assert (await rt.login.handle_callback(callback_params(state))).ok  # the link was released: tapped again, it works
    assert [e["session"] for e in rt.events.store.events()] == ["login_failed", "login"]


async def test_a_login_failed_event_is_raised_before_the_link_is_released(events_rt, fake_kite, monkeypatch):
    KiteSession(fake_kite)
    rt = events_rt()
    state = state_of((await rt.login.issue_link())[0])
    loop = asyncio.get_running_loop()
    releasing, go_on, released = asyncio.Event(), threading.Event(), threading.Event()
    release = StateStore.release

    def slow_release(self, reservation):
        loop.call_soon_threadsafe(releasing.set)
        go_on.wait(5)
        try:
            release(self, reservation)
        finally:
            released.set()

    monkeypatch.setattr(StateStore, "release", slow_release)
    task = asyncio.create_task(rt.login.handle_callback(callback_params(state, WRONG_REQUEST_TOKEN)))
    await releasing.wait()
    task.cancel()  # e.g. a client that cancels complete_login while the link is being released
    with pytest.raises(asyncio.CancelledError):
        await task
    go_on.set()
    assert await asyncio.to_thread(released.wait, 5)
    (event,) = rt.events.store.events()
    assert (event["session"], event["event_id"]) == ("login_failed", failed_id(state))


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
    assert rt.events.store.events() == []
    assert (await rt.login.handle_callback(callback_params(state))).ok
    assert (await rt.login.handle_callback(callback_params(state))).reason == "bad_state"  # a replay
    replay = await rt.login.handle_callback(callback_params(state, WRONG_REQUEST_TOKEN))  # a used link: no exchange
    assert replay.reason == "bad_state"
    assert [e["session"] for e in rt.events.store.events()] == ["login"]


async def test_rate_limiting_in_the_callback_route_never_becomes_an_event(events_app, fake_kite):
    kite = KiteSession(fake_kite)
    app, rt = events_app
    state = state_of((await rt.login.issue_link())[0])
    async with web(app) as http:
        for _ in range(CALLBACK_RATE_LIMIT):
            assert (await http.get("/callback")).status_code == 400  # malformed
        limited = await http.get("/callback", params=callback_params(state))  # a valid link, but too many requests
    assert limited.status_code == 400 and kite.exchanges == []
    assert [e["reason"] for e in rt.audit.read() if e["event"] == "login_refused"][-1] == "rate_limited"
    assert rt.events.store.events() == []


async def test_an_error_in_the_callback_route_never_becomes_an_event(events_app, fake_kite):
    KiteSession(fake_kite)
    app, rt = events_app
    state = state_of((await rt.login.issue_link())[0])

    def broken(_: httpx2.Request) -> httpx2.Response:
        raise RuntimeError("broken")  # after the link was reserved: an error in the route, not a failed login

    fake_kite.on("POST", "/session/token", handler=broken)
    async with web(app) as http:
        assert (await http.get("/callback", params=callback_params(state))).status_code == 400
    refusal = [e for e in rt.audit.read() if e["event"] == "login_refused"][-1]
    assert (refusal["reason"], refusal["error"]) == ("error", "RuntimeError")
    assert rt.events.store.events() == []


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


async def test_a_store_that_cannot_write_does_not_change_a_refusal(events_rt, fake_kite, caplog):
    kite = KiteSession(fake_kite, user_id="ZZ9999")
    rt = events_rt(KITE_MCP_ALLOWED_USER_ID=USER_ID)
    state = state_of((await rt.login.issue_link())[0])
    rt.events.store.close()
    with caplog.at_level(logging.WARNING):
        result = await rt.login.handle_callback(callback_params(state))
    assert (result.ok, result.reason) == (False, "account_mismatch")
    assert "that new session was ended and nothing was stored" in result.message and len(kite.logouts) == 1
    assert rt.tokens.current().access_token == ACCESS_TOKEN
    assert "could not record the login_refused session event" in caplog.text
    no_secret_in_log(caplog.text, state)


async def test_a_store_that_cannot_write_does_not_change_a_failure(events_rt, fake_kite, caplog):
    KiteSession(fake_kite)
    rt = events_rt()
    state = state_of((await rt.login.issue_link())[0])
    rt.events.store.close()
    with caplog.at_level(logging.WARNING):
        result = await rt.login.handle_callback(callback_params(state, WRONG_REQUEST_TOKEN))
        assert (result.ok, result.reason) == (False, "exchange_failed")
        assert (await rt.login.handle_callback(callback_params(state))).ok  # the link was released all the same
    assert rt.tokens.current().access_token == NEW_TOKEN
    assert "could not record the login_failed session event" in caplog.text
    no_secret_in_log(caplog.text, state)


async def test_without_the_events_a_login_works_as_before(runtime_factory, fake_kite):
    KiteSession(fake_kite)
    rt = runtime_factory()
    assert rt.login.session_notes is None and (await rt.login.complete(REQUEST_TOKEN)).ok


def ist(day: int, hour: int, minute: int = 0) -> dt.datetime:
    return dt.datetime(2026, 11, day, hour, minute, tzinfo=IST)


@pytest.mark.parametrize(
    ("logged_in", "expires", "words"),
    [
        (ist(16, 7, 35), ist(17, 6), "06:00 IST tomorrow"),
        (ist(16, 5, 30), ist(16, 6), "06:00 IST today"),
        (ist(16, 7, 35), ist(18, 6), "06:00 IST on 2026-11-18"),  # never a weekday or month name of the locale
    ],
)
def test_the_session_event_texts(logged_in, expires, words):
    body = login_event(seed="s", logged_in_at=logged_in, expires_at=expires, received_at=logged_in)
    assert body["text"] == f"Kite login completed: the session is valid until {words}."
    again = login_event(seed="s", logged_in_at=logged_in, expires_at=expires, received_at=expires)
    assert again["event_id"] == body["event_id"]  # the seed alone makes the id
    refused = login_refused_event(seed="t", session_ended=True, received_at=logged_in)
    assert refused["session"] == "login_refused" and refused["event_id"] != body["event_id"]
    assert LOGIN_FAILURES == ("exchange_failed", "token_not_stored")
    failed = login_failed_event(seed="s", reason="token_not_stored", received_at=logged_in)
    assert failed["session"] == "login_failed" and failed["event_id"] not in (body["event_id"], refused["event_id"])
    with pytest.raises(ValueError):
        login_failed_event(seed="s", reason="Kite said no", received_at=logged_in)  # a fixed vocabulary only


def test_every_session_event_name_is_listed_in_sessions():
    built = {
        session_event("token_rejected", seed="s", received_at=FIXED_NOW)["session"],
        login_event(seed="s", logged_in_at=FIXED_NOW, expires_at=ist(5, 6), received_at=FIXED_NOW)["session"],
        login_refused_event(seed="s", session_ended=True, received_at=FIXED_NOW)["session"],
        login_failed_event(seed="s", reason="exchange_failed", received_at=FIXED_NOW)["session"],
    }
    assert built == set(SESSIONS) and set(SESSION_TEXT) <= set(SESSIONS)
    with pytest.raises(ValueError, match="unknown session event"):
        _session("logged_out", "s", FIXED_NOW, "a name that is not in SESSIONS")


# --- what the service sees by itself: a token stored beside it, a rejection any call meets first ---------------------


def at(day: int, hour: int, minute: int = 0) -> dt.datetime:
    """A moment of November 2026 in IST: the 4th is a Wednesday, the 7th a Saturday."""
    return dt.datetime(2026, 11, day, hour, minute, tzinfo=IST)


@pytest.mark.parametrize("moment", [at(4, 6, 30), at(7, 11, 0)], ids=["weekday-before-the-poll-window", "saturday"])
async def test_a_token_stored_beside_the_service_raises_login_at_the_next_tick(service_factory, fake_kite, moment):
    fake_kite.on("GET", "/orders", [])
    svc, rt = service_factory(consumers=[SESSION_CONSUMER], clock=Clock(moment))
    svc.poller.prime()  # the token in use when the service started is not a login
    await svc.poller.tick()
    assert svc.store.events() == []
    write_token_file(rt.settings.token_file, token=NEW_TOKEN, logged_in_at=moment)  # `kite-connect-mcp login` beside it
    await svc.poller.tick()  # the next tick: at most Poller.TICK_S (5 s) after the write
    (event,) = svc.store.events()
    assert (event["session"], event["logged_in_at"]) == ("login", moment.isoformat(timespec="seconds"))
    assert event["expires_at"] == next_token_reset(moment).isoformat(timespec="seconds")
    no_secret_in(event)
    await svc.poller.tick()
    assert len(svc.store.events()) == 1


async def test_in_external_mode_a_new_token_raises_nothing(service_factory, fake_kite):
    fake_kite.on("GET", "/orders", [])
    svc, rt = service_factory(consumers=[SESSION_CONSUMER], clock=Clock(at(4, 6, 30)), KITE_MCP_AUTH_MODE="external")
    svc.poller.prime()
    write_token_file(rt.settings.token_file, token=NEW_TOKEN, logged_in_at=at(4, 6, 30))  # another program's login
    await svc.poller.tick()
    assert svc.store.events() == []  # as in 0.2.0


async def test_the_callback_and_the_poll_raise_one_login_for_one_token(events_rt, fake_kite):
    KiteSession(fake_kite)
    fake_kite.on("GET", "/orders", [])
    rt = events_rt()
    rt.events.poller.prime()
    url, _ = await rt.login.issue_link()
    assert (await rt.login.handle_callback(callback_params(state_of(url)))).ok
    await rt.events.poller.tick()  # the poll sees the new token too
    assert [e["session"] for e in rt.events.store.events()] == ["login"]


async def test_when_the_poll_sees_the_token_first_the_logins_own_report_is_the_same_event(events_rt, fake_kite):
    fake_kite.on("GET", "/orders", [])
    rt = events_rt()
    rt.events.poller.prime()
    # the manager's write, in a thread
    write_token_file(rt.settings.token_file, token=NEW_TOKEN, logged_in_at=FIXED_NOW)
    await rt.events.poller.tick()
    reset = next_token_reset(FIXED_NOW)
    assert rt.events.note_login(NEW_TOKEN, logged_in_at=FIXED_NOW, expires_at=reset) is None  # already stored
    assert [e["session"] for e in rt.events.store.events()] == ["login"]


async def test_a_store_that_cannot_write_the_polls_login_does_not_stop_the_poll(
    service_factory, fake_kite, caplog, monkeypatch
):
    fake_kite.on("GET", "/orders", [])
    svc, rt = service_factory(consumers=[SESSION_CONSUMER])
    svc.poller.prime()
    write_token_file(rt.settings.token_file, token=NEW_TOKEN, logged_in_at=FIXED_NOW)

    def cannot_write(*args, **kwargs):
        raise StoreError("cannot write the event store: disk I/O error")

    monkeypatch.setattr(svc.store, "insert_event", cannot_write)
    with caplog.at_level(logging.WARNING):
        await svc.poller.tick()
    assert "could not record the login session event (StoreError)" in caplog.text
    assert NEW_TOKEN not in caplog.text
    assert len(fake_kite.calls("GET", "/orders")) == 1  # the poll ran all the same
    assert svc.store.counters()["poll_ok"]["note"] == "login"


async def test_a_tool_call_that_kite_refuses_raises_token_rejected_once(events_rt, fake_kite):
    fake_kite.on("GET", "/user/profile", status=403, error_type="TokenException", message="Incorrect token")
    rt = events_rt()
    heard = []
    rt.events.on_token_rejected(lambda: heard.append(1))
    with pytest.raises(TokenError):
        await rt.client.get("/user/profile")  # what session_status(verify=true) does on the service's /mcp
    (event,) = rt.events.store.events()
    assert event["session"] == "token_rejected" and heard == [1]
    assert rt.tokens.current() is None  # marked, as before
    assert rt.events.note_token_rejected(ACCESS_TOKEN) is None  # the poll's or a doorbell's own report: the same event
    assert len(rt.events.store.events()) == 1 and heard == [1]


async def test_the_polls_rejection_is_one_event_with_the_clients_report(events_rt, fake_kite):
    fake_kite.on("GET", "/orders", status=403, error_type="TokenException", message="Invalid session")
    rt = events_rt()
    heard = []
    rt.events.on_token_rejected(lambda: heard.append(1))
    assert await rt.events.poller.poll("window") is False  # the client reports it, then the poll does
    assert [e["session"] for e in rt.events.store.events()] == ["token_rejected"] and heard == [1]


async def test_a_listener_that_fails_is_logged_and_the_call_still_raises(events_rt, fake_kite, caplog):
    fake_kite.on("GET", "/user/profile", status=403, error_type="TokenException", message="Incorrect token")
    rt = events_rt()
    rt.events.store.close()  # the event service cannot write: note_token_rejected raises StoreError
    with caplog.at_level(logging.WARNING), pytest.raises(TokenError):
        await rt.client.get("/user/profile")
    assert "could not report a rejected token to the order events (StoreError)" in caplog.text
    assert ACCESS_TOKEN not in caplog.text
    assert rt.tokens.current() is None  # marked all the same (in memory)


@pytest.mark.parametrize("env_token", [None, "testenvaccesstoken0000000000000003"], ids=["file", "environment"])
def test_only_a_rejection_of_the_token_in_use_is_reported(events_rt, env_token):
    rt = events_rt(KITE_ACCESS_TOKEN=env_token)
    in_use = env_token or ACCESS_TOKEN
    other = ACCESS_TOKEN if env_token else NEW_TOKEN
    heard: list[str] = []
    rt.tokens.on_rejected = heard.append
    rt.tokens.reject(other, report=True)  # a token no longer in use (the file was replaced since it was sent)
    assert heard == [] and rt.tokens.current().access_token == in_use
    rt.tokens.reject(in_use)  # a logout's or a refused account's session call: never reported
    assert heard == [] and rt.tokens.current() is None
    rt.tokens.reject(in_use, report=True)
    assert heard == [in_use]
    assert rt.events.store.events() == []  # the listener was replaced: nothing reached the store


async def test_without_the_events_a_refused_call_works_as_before(runtime_factory, fake_kite):
    fake_kite.on("GET", "/user/profile", status=403, error_type="TokenException", message="Incorrect token")
    rt = runtime_factory()
    assert rt.events is None and rt.tokens.on_rejected is None
    with pytest.raises(TokenError):
        await rt.client.get("/user/profile")
    assert rt.tokens.current() is None  # marked, as in 0.2.0
    assert len(fake_kite.calls("GET", "/user/profile")) == 1


async def test_a_logout_raises_no_token_rejected(events_rt, fake_kite):
    kite = KiteSession(fake_kite)
    kite.logout_error = (403, "TokenException")  # Kite no longer knows the stored token
    rt = events_rt()
    assert (await rt.login.logout()).kite == "already_invalid"
    assert rt.events.store.events() == []


async def test_ending_a_refused_accounts_session_raises_no_token_rejected(events_rt, fake_kite):
    kite = KiteSession(fake_kite, user_id="ZZ9999")
    kite.logout_error = (403, "TokenException")
    rt = events_rt(KITE_MCP_ALLOWED_USER_ID=USER_ID)
    assert (await rt.login.complete(REQUEST_TOKEN)).reason == "account_mismatch"
    assert [e["session"] for e in rt.events.store.events()] == ["login_refused"]


def test_token_rejected_tells_its_listeners_once_per_token(service_factory):
    svc, _ = service_factory()
    heard = []
    svc.on_token_rejected(lambda: heard.append(1))
    assert svc.note_token_rejected(ACCESS_TOKEN) is not None
    assert svc.note_token_rejected(ACCESS_TOKEN) is None  # the same token: stored once, told once
    assert heard == [1]


@pytest.mark.parametrize(
    ("mode", "logged_in_at", "cleared"),
    [
        ("managed", FIXED_NOW, True),  # a new login, later than the record the service saw
        ("managed", FIXED_NOW - dt.timedelta(hours=3), False),  # the same record written again: not a login
        ("external", FIXED_NOW, False),  # another program's file: its marks stay, as in 0.2.0
    ],
    ids=["managed-new-login", "managed-same-record", "external"],
)
async def test_a_login_beside_the_service_that_kite_answers_with_the_rejected_token_clears_its_mark(
    events_rt, fake_kite, mode, logged_in_at, cleared
):
    fake_kite.on("GET", "/user/profile", status=403, error_type="TokenException", message="Incorrect token")
    fake_kite.on("GET", "/orders", [])
    rt = events_rt(KITE_MCP_AUTH_MODE=mode)
    poller = rt.events.poller
    poller.prime()
    with pytest.raises(TokenError):
        await rt.client.get("/user/profile")
    await poller.tick()
    assert rt.tokens.current() is None and fake_kite.calls("GET", "/orders") == []  # refused: the poll skips quietly
    # `kite-connect-mcp login` beside the service, and Kite gave the same token string again: the command clears the
    # mark only in its own memory, and writes the file as the login does (a new file in its place)
    TokenFile(rt.settings.token_file, writable=True).write(TokenRecord.new(ACCESS_TOKEN, logged_in_at))
    await poller.tick()
    marks = json.loads(rt.events.store.meta("rejected_tokens"))
    if cleared:
        assert rt.tokens.current().access_token == ACCESS_TOKEN and marks == []
        assert [e["session"] for e in rt.events.store.events()] == ["token_rejected", "login"]
        assert len(fake_kite.calls("GET", "/orders")) == 1  # the login's poll, with the token
    else:
        assert rt.tokens.current() is None and len(marks) == 1
        assert [e["session"] for e in rt.events.store.events()] == ["token_rejected"]
        assert fake_kite.calls("GET", "/orders") == []
