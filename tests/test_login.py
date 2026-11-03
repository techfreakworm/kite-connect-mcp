"""The login: the link and its state file, the token exchange (callback and pasted token), the account pin, logout, the
public callback route and the CLI. Kite is FakeKite; nothing leaves the process (the CLI listen tests talk to a
temporary server on loopback)."""

from __future__ import annotations

import asyncio
import datetime as dt
import fcntl
import json
import logging
import os
import re
import socket
import stat
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest

import kite_connect_mcp
from conftest import ACCESS_TOKEN, API_KEY, API_SECRET, BEARER, FIXED_NOW
from kite_connect_mcp.auth import login as login_module
from kite_connect_mcp.auth import loginstate
from kite_connect_mcp.auth.callback import SECURITY_HEADERS, CallbackRateLimiter
from kite_connect_mcp.auth.login import cli_login, cli_logout, link_deadline, unpack_redirect
from kite_connect_mcp.auth.loginstate import Reservation, StateStore, StateStoreError, digest_of
from kite_connect_mcp.auth.tokenstore import TokenFile
from kite_connect_mcp.cli import build_parser, main
from kite_connect_mcp.client.http import checksum
from kite_connect_mcp.errors import KiteMCPError
from kite_connect_mcp.server import build_http_app, build_runtime
from kite_connect_mcp.timeutil import IST
from kite_login import FORBIDDEN_IN_LOGS, NEW_TOKEN, REQUEST_TOKEN, USER_ID, KiteSession, callback_params, state_of

STATE = "TestState0000000000000000000000000000001"
HOUR = dt.timedelta(hours=1)


def ist(*args: int) -> dt.datetime:
    return dt.datetime(*args, tzinfo=IST)


async def _no_sleep(_: float) -> None:
    return None


@pytest.fixture
def kite(fake_kite) -> KiteSession:
    return KiteSession(fake_kite)


@pytest.fixture
def rt(runtime_factory, kite):
    return runtime_factory()


def runtime_at(settings, fake_kite, moment: dt.datetime):
    return build_runtime(settings, transport=fake_kite.transport(), clock=lambda: moment, sleep=_no_sleep)


def state_file(rt) -> Path:
    return rt.settings.state_dir / "login_states.json"


def stored_states(rt) -> dict:
    return json.loads(state_file(rt).read_text())["states"]


async def new_state(rt) -> str:
    url, _ = await rt.login.issue_link()
    return state_of(url)


# --- the link -------------------------------------------------------------------------------------------------------


async def test_link_format(rt):
    url, expires_at = await rt.login.issue_link()
    parts = urlsplit(url)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == "https://kite.zerodha.com/connect/login"
    assert parts.query.startswith(f"v=3&api_key={API_KEY}&redirect_params=state%3D")  # redirect_params is encoded
    state = state_of(url)
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", state)
    assert parse_qs(parts.query)["redirect_params"] == [f"state={state}"]
    assert expires_at == FIXED_NOW + HOUR  # KITE_MCP_LOGIN_LINK_TTL_S defaults to an hour
    assert await new_state(rt) != state  # a new state every time


async def test_login_url_and_ttl_settings_are_used(runtime_factory, kite):
    rt = runtime_factory(KITE_LOGIN_URL="https://login.example.invalid/connect/login", KITE_MCP_LOGIN_LINK_TTL_S="600")
    url, expires_at = await rt.login.issue_link()
    assert url.startswith("https://login.example.invalid/connect/login?v=3&api_key=")
    assert expires_at == FIXED_NOW + dt.timedelta(minutes=10)


async def test_only_the_digest_of_the_state_is_stored(rt):
    state = await new_state(rt)
    path = state_file(rt)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    data = json.loads(path.read_text())
    assert data["version"] == 1
    assert list(data["states"]) == [digest_of(state)]
    entry = data["states"][digest_of(state)]
    assert entry["status"] == "pending"
    assert entry["issued_at"] == "2026-11-04T10:00:00+05:30" and entry["expires_at"] == "2026-11-04T11:00:00+05:30"
    for file in rt.settings.state_dir.rglob("*"):  # the state is in no file at all, the audit log included
        if file.is_file():
            assert state not in file.read_text(errors="replace"), file.name


@pytest.mark.parametrize(
    ("now", "ttl_s", "expected"),
    [
        (ist(2026, 11, 4, 10, 0), 3600, ist(2026, 11, 4, 11, 0)),
        (ist(2026, 11, 4, 5, 30), 3600, ist(2026, 11, 4, 5, 59)),  # would be 06:30: capped before the reset
        (ist(2026, 11, 4, 5, 58, 59), 3600, ist(2026, 11, 4, 5, 59)),
        (ist(2026, 11, 4, 23, 30), 10 * 3600, ist(2026, 11, 5, 5, 59)),  # a long ttl ends next morning
        (ist(2026, 11, 4, 23, 30), 3600, ist(2026, 11, 5, 0, 30)),  # a short one is untouched
        (ist(2026, 11, 4, 5, 59, 30), 3600, ist(2026, 11, 4, 6, 59, 30)),  # last minute: the cap moves to tomorrow
        (ist(2026, 11, 4, 6, 0), 3600, ist(2026, 11, 4, 7, 0)),
    ],
)
def test_link_deadline_never_passes_0559_ist(now, ttl_s, expected):
    assert link_deadline(now, ttl_s) == expected
    assert link_deadline(now.astimezone(dt.UTC), ttl_s) == expected  # any timezone in


async def test_link_expiry_is_capped_before_the_reset(settings_factory, fake_kite, kite):
    rt = runtime_at(settings_factory(), fake_kite, ist(2026, 11, 4, 5, 30))
    url, expires_at = await rt.login.issue_link()
    assert expires_at == ist(2026, 11, 4, 5, 59)
    assert stored_states(rt)[digest_of(state_of(url))]["expires_at"] == "2026-11-04T05:59:00+05:30"
    rt.login.clock = lambda: ist(2026, 11, 4, 5, 59, 1)  # one second past: the link is dead
    result = await rt.login.handle_callback(callback_params(state_of(url)))
    assert (result.ok, result.reason) == (False, "bad_state")
    assert kite.exchanges == []


async def test_a_link_needs_the_api_secret_too(settings_factory, fake_kite):
    rt = runtime_at(settings_factory(KITE_API_SECRET=None), fake_kite, FIXED_NOW)
    with pytest.raises(KiteMCPError, match="KITE_API_SECRET"):
        await rt.login.issue_link()
    assert not state_file(rt).exists()


# --- the state file -------------------------------------------------------------------------------------------------


def test_state_file_and_folder_are_private(tmp_path):
    path = tmp_path / "fresh" / "login_states.json"
    StateStore(path).issue(STATE, FIXED_NOW, FIXED_NOW + HOUR)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE((path.parent / ".login_states.lock").stat().st_mode) == 0o600
    assert STATE not in path.read_text()


def test_reservations_are_exclusive_and_release_frees_the_state(tmp_path):
    store = StateStore(tmp_path / "login_states.json")
    store.issue(STATE, FIXED_NOW, FIXED_NOW + HOUR)
    first = store.reserve(STATE, FIXED_NOW)
    assert isinstance(first, Reservation)
    assert store.reserve(STATE, FIXED_NOW) is None  # in use
    store.release(Reservation(first.digest, "not-the-holder"))  # somebody else's release changes nothing
    assert store.reserve(STATE, FIXED_NOW) is None
    store.release(first)
    assert store.reserve(STATE, FIXED_NOW) is not None  # the same link can be tried again


def test_a_crashed_exchange_frees_the_state_after_the_reservation_ttl(tmp_path):
    store = StateStore(tmp_path / "login_states.json")
    store.issue(STATE, FIXED_NOW, FIXED_NOW + HOUR)
    assert store.reserve(STATE, FIXED_NOW) is not None  # ...and never committed or released
    almost = FIXED_NOW + dt.timedelta(seconds=loginstate.RESERVATION_TTL_S - 1)
    assert store.reserve(STATE, almost) is None
    assert store.reserve(STATE, FIXED_NOW + dt.timedelta(seconds=loginstate.RESERVATION_TTL_S)) is not None


def test_commit_uses_the_state_up_and_closes_every_other_pending_one(tmp_path):
    store = StateStore(tmp_path / "login_states.json")
    for name in ("one", "two", "three"):
        store.issue(f"{name}-{STATE}", FIXED_NOW, FIXED_NOW + HOUR)
    won = store.reserve(f"two-{STATE}", FIXED_NOW)
    assert won is not None
    store.commit(won, FIXED_NOW)
    assert store.reserve(f"two-{STATE}", FIXED_NOW) is None  # used
    assert store.reserve(f"one-{STATE}", FIXED_NOW) is None  # closed
    assert store.reserve(f"three-{STATE}", FIXED_NOW) is None
    states = json.loads((tmp_path / "login_states.json").read_text())["states"]
    assert [e["status"] for e in states.values()] == ["used"]


def test_committing_without_a_state_closes_all_pending_ones(tmp_path):
    store = StateStore(tmp_path / "login_states.json")
    store.issue(STATE, FIXED_NOW, FIXED_NOW + HOUR)
    store.commit(None, FIXED_NOW)
    assert store.reserve(STATE, FIXED_NOW) is None


def test_expired_and_unknown_states_cannot_be_reserved_and_old_entries_are_pruned(tmp_path):
    path = tmp_path / "login_states.json"
    store = StateStore(path)
    store.issue("old-" + STATE, FIXED_NOW, FIXED_NOW + HOUR)
    assert store.reserve("old-" + STATE, FIXED_NOW + HOUR) is None  # expired exactly now
    assert store.reserve("never-issued-" + STATE, FIXED_NOW) is None
    assert digest_of("old-" + STATE) in path.read_text()  # reading does not rewrite the file
    store.issue("new-" + STATE, FIXED_NOW + 2 * HOUR, FIXED_NOW + 3 * HOUR)
    assert list(json.loads(path.read_text())["states"]) == [digest_of("new-" + STATE)]


def test_the_number_of_kept_states_is_capped_newest_first(tmp_path):
    path = tmp_path / "login_states.json"
    store = StateStore(path)
    total = loginstate.MAX_STATES + 5
    for number in range(total):
        moment = FIXED_NOW + dt.timedelta(seconds=number)
        store.issue(f"state-{number:03d}-{STATE}", moment, moment + HOUR)
    kept = json.loads(path.read_text())["states"]
    assert len(kept) == loginstate.MAX_STATES
    assert digest_of(f"state-{total - 1:03d}-{STATE}") in kept
    assert digest_of(f"state-000-{STATE}") not in kept


@pytest.mark.parametrize("garbage", ["", "not json", "[1, 2]", '{"states": []}', '{"states": {"x": 5}}'])
def test_a_damaged_state_file_counts_as_no_open_links(tmp_path, garbage):
    path = tmp_path / "login_states.json"
    path.write_text(garbage)
    store = StateStore(path)
    assert store.reserve(STATE, FIXED_NOW) is None
    store.issue(STATE, FIXED_NOW, FIXED_NOW + HOUR)  # and it heals on the next write
    assert store.reserve(STATE, FIXED_NOW) is not None


@pytest.mark.parametrize("shared", [True, False], ids=["one store", "one store per thread"])
def test_concurrent_reservations_of_one_state_cannot_both_win(tmp_path, shared):
    path = tmp_path / "login_states.json"
    StateStore(path).issue(STATE, FIXED_NOW, FIXED_NOW + HOUR)
    threads = 12
    one = StateStore(path)
    stores = [one if shared else StateStore(path) for _ in range(threads)]  # separate stores lock only by flock
    barrier = threading.Barrier(threads)

    def attempt(store: StateStore) -> Reservation | None:
        barrier.wait()
        return store.reserve(STATE, FIXED_NOW)

    with ThreadPoolExecutor(threads) as pool:
        results = list(pool.map(attempt, stores))
    assert sum(r is not None for r in results) == 1


RACE = """
import datetime as dt, sys
from pathlib import Path
from kite_connect_mcp.auth.loginstate import StateStore
store, state = StateStore(Path(sys.argv[1])), sys.argv[2]
print("ready", flush=True)
sys.stdin.readline()
print("won" if store.reserve(state, dt.datetime.now(dt.UTC)) else "lost", flush=True)
"""


def test_separate_processes_cannot_both_reserve_a_state(tmp_path):
    path = tmp_path / "login_states.json"
    now = dt.datetime.now(dt.UTC)
    StateStore(path).issue(STATE, now, now + HOUR)
    env = {**os.environ, "PYTHONPATH": str(Path(kite_connect_mcp.__file__).resolve().parents[1])}
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", RACE, str(path), STATE],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            env=env,
        )
        for _ in range(6)
    ]
    for proc in procs:  # everyone has finished starting up...
        assert proc.stdout.readline().strip() == "ready"
    for proc in procs:  # ...and now they all go at once
        proc.stdin.write("go\n")
        proc.stdin.flush()
    answers = sorted(proc.stdout.readline().strip() for proc in procs)
    for proc in procs:
        proc.wait(timeout=30)
    assert answers == ["lost"] * 5 + ["won"]


def test_a_state_file_that_cannot_be_written_is_a_clear_error(tmp_path, monkeypatch):
    def refuse(path: Path, text: str) -> None:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(loginstate, "write_private_file", refuse)
    with pytest.raises(StateStoreError, match=r"cannot write the login state file .*Permission denied"):
        StateStore(tmp_path / "login_states.json").issue(STATE, FIXED_NOW, FIXED_NOW + HOUR)


def test_waiting_too_long_for_a_lock_held_elsewhere_is_an_error(tmp_path, monkeypatch):
    path = tmp_path / "login_states.json"
    store = StateStore(path)
    store.issue(STATE, FIXED_NOW, FIXED_NOW + HOUR)
    monkeypatch.setattr(loginstate, "LOCK_TIMEOUT_S", 0.05)
    fd = os.open(tmp_path / ".login_states.lock", os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        with pytest.raises(StateStoreError, match="locked by another process"):
            store.reserve(STATE, FIXED_NOW)
    finally:
        os.close(fd)
    assert store.reserve(STATE, FIXED_NOW) is not None


# --- the exchange ---------------------------------------------------------------------------------------------------


async def test_callback_stores_the_token_and_commits_the_state(rt, kite, fake_kite):
    other = await new_state(rt)  # another open link: a finished login closes it
    state = await new_state(rt)
    result = await rt.login.handle_callback(callback_params(state))
    assert (result.ok, result.reason, result.user_id) == (True, "ok", USER_ID)

    assert kite.exchanges == [
        {"api_key": API_KEY, "request_token": REQUEST_TOKEN, "checksum": checksum(API_KEY, REQUEST_TOKEN, API_SECRET)}
    ]
    assert "authorization" not in fake_kite.calls("POST", "/session/token")[0].headers

    token_file = rt.settings.token_file
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
    stored = json.loads(token_file.read_text())
    assert stored == {
        "access_token": NEW_TOKEN,
        "login_date": "2026-11-04",
        "logged_in_at": "2026-11-04T10:00:00+05:30",
        "expires_at": "2026-11-05T06:00:00+05:30",
    }  # only what the token store needs: no name, e-mail, public token...
    assert rt.tokens.current().access_token == NEW_TOKEN

    states = stored_states(rt)
    assert list(states) == [digest_of(state)] and states[digest_of(state)]["status"] == "used"
    assert (await rt.login.handle_callback(callback_params(other))).reason == "bad_state"

    replay = await rt.login.handle_callback(callback_params(state))
    assert (replay.ok, replay.reason) == (False, "bad_state")
    assert len(kite.exchanges) == 1  # the replay never reached Kite


async def test_a_failed_exchange_releases_the_state_so_the_link_can_be_retried(rt, kite):
    state = await new_state(rt)
    kite.fail_next = True
    failed = await rt.login.handle_callback(callback_params(state))
    assert (failed.ok, failed.reason) == (False, "exchange_failed")
    assert "Token is invalid or has expired" in failed.message
    for secret in (state, REQUEST_TOKEN):
        assert secret not in failed.message
    assert "reservation" not in stored_states(rt)[digest_of(state)]
    assert rt.tokens.current().access_token == ACCESS_TOKEN  # nothing changed

    retry = await rt.login.handle_callback(callback_params(state))
    assert retry.ok and rt.tokens.current().access_token == NEW_TOKEN
    assert len(kite.exchanges) == 2


async def test_an_exchange_that_gets_no_answer_also_releases_the_state(rt, kite, fake_kite):
    state = await new_state(rt)

    def dead(_: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("no answer")

    fake_kite.on("POST", "/session/token", handler=dead)
    result = await rt.login.handle_callback(callback_params(state))
    assert (result.ok, result.reason) == (False, "exchange_failed")
    assert "order book" not in result.message  # the client's wording is for writes
    assert "reservation" not in stored_states(rt)[digest_of(state)]


async def test_concurrent_callbacks_for_one_state_run_one_exchange(rt, kite):
    state = await new_state(rt)
    kite.delay = 0.05
    results = await asyncio.gather(*(rt.login.handle_callback(callback_params(state)) for _ in range(5)))
    assert sorted(r.reason for r in results) == ["bad_state"] * 4 + ["ok"]
    assert len(kite.exchanges) == 1


async def test_two_processes_of_the_server_cannot_both_exchange_one_state(runtime_factory, kite):
    first, second = runtime_factory(), runtime_factory()  # separate managers over the same state folder
    state = await new_state(first)
    kite.delay = 0.05
    results = await asyncio.gather(
        first.login.handle_callback(callback_params(state)), second.login.handle_callback(callback_params(state))
    )
    assert sorted(r.reason for r in results) == ["bad_state", "ok"]
    assert len(kite.exchanges) == 1


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: {k: v for k, v in p.items() if k != "state"},
        lambda p: {k: v for k, v in p.items() if k != "request_token"},
        lambda p: {k: v for k, v in p.items() if k != "status"},
        lambda p: {**p, "status": "failed"},
        lambda p: {**p, "status": "Success"},
        lambda p: {**p, "state": "short"},
        lambda p: {**p, "state": p["state"] + "!"},
        lambda p: {**p, "state": p["state"] * 4},
        lambda p: {**p, "request_token": "x" * 129},
        lambda p: {**p, "request_token": "bad token"},
        lambda p: {**p, "request_token": ""},
    ],
    ids=[
        "no state",
        "no request_token",
        "no status",
        "status failed",
        "status case",
        "short state",
        "state chars",
        "long state",
        "long request_token",
        "request_token chars",
        "empty request_token",
    ],
)
async def test_malformed_callbacks_are_refused_without_touching_the_state_or_kite(rt, kite, mutate):
    state = await new_state(rt)
    result = await rt.login.handle_callback(mutate(callback_params(state)))
    assert (result.ok, result.reason) == (False, "bad_request")
    assert kite.exchanges == []
    assert "reservation" not in stored_states(rt)[digest_of(state)]
    assert (await rt.login.handle_callback(callback_params(state))).ok  # the link is still good


async def test_a_repeated_parameter_is_refused(rt, kite):
    state = await new_state(rt)
    good = [("status", "success"), ("request_token", REQUEST_TOKEN), ("state", state)]
    for extra in (("state", state), ("request_token", REQUEST_TOKEN), ("status", "success")):
        result = await rt.login.handle_callback([*good, extra])
        assert (result.ok, result.reason) == (False, "bad_request")
    assert (
        await rt.login.handle_callback({"status": "success", "request_token": [REQUEST_TOKEN] * 2, "state": state})
    ).reason == ("bad_request")
    assert kite.exchanges == []
    assert (await rt.login.handle_callback(good)).ok  # pairs work as well as dicts


async def test_a_state_this_server_never_issued_never_reaches_kite(rt, kite):
    result = await rt.login.handle_callback(callback_params(STATE))
    assert (result.ok, result.reason) == (False, "bad_state")
    assert kite.exchanges == []


async def test_nothing_from_kites_answer_is_kept_but_the_token_and_user_id(rt, kite):
    await rt.login.handle_callback(callback_params(await new_state(rt)))
    for file in rt.settings.state_dir.rglob("*"):
        if file.is_file():
            text = file.read_text(errors="replace")
            for forbidden in FORBIDDEN_IN_LOGS:
                assert forbidden not in text, file.name


# --- the account pin ------------------------------------------------------------------------------------------------


async def test_a_login_by_another_account_is_ended_at_kite_and_nothing_is_stored(runtime_factory, fake_kite):
    kite = KiteSession(fake_kite, user_id="ZZ9999")
    rt = runtime_factory(KITE_MCP_ALLOWED_USER_ID=USER_ID)
    state = await new_state(rt)
    before = rt.settings.token_file.read_text()

    result = await rt.login.handle_callback(callback_params(state))
    assert (result.ok, result.reason) == (False, "account_mismatch")
    assert "ZZ9999" not in result.message and NEW_TOKEN not in result.message
    assert kite.logouts == [{"api_key": API_KEY, "access_token": NEW_TOKEN}]  # the NEW token, not the stored one
    delete = fake_kite.calls("DELETE", "/session/token")[0]
    assert "authorization" not in delete.headers
    assert rt.settings.token_file.read_text() == before
    assert rt.tokens.current().access_token == ACCESS_TOKEN
    assert "reservation" not in stored_states(rt)[digest_of(state)]  # released: the right account can use the link

    event = [e for e in rt.audit.read() if e["event"] == "login_refused"][-1]
    assert event["reason"] == "account_mismatch" and event["session_ended"] is True
    assert "ZZ9999" not in json.dumps(rt.audit.read())  # the other person's id is not recorded either

    kite.spent, kite.user_id = False, USER_ID.lower()  # the pin is case-insensitive
    assert (await rt.login.handle_callback(callback_params(state))).ok


async def test_a_pinned_login_without_a_user_id_in_the_answer_is_refused(runtime_factory, fake_kite):
    kite = KiteSession(fake_kite, user_id=None)
    rt = runtime_factory(KITE_MCP_ALLOWED_USER_ID=USER_ID)
    result = await rt.login.handle_callback(callback_params(await new_state(rt)))
    assert (result.ok, result.reason) == (False, "account_mismatch")
    assert kite.logouts == [{"api_key": API_KEY, "access_token": NEW_TOKEN}]


async def test_the_pin_also_guards_the_pasted_token_path(runtime_factory, fake_kite):
    kite = KiteSession(fake_kite, user_id="ZZ9999")
    rt = runtime_factory(KITE_MCP_ALLOWED_USER_ID=USER_ID)
    result = await rt.login.complete(REQUEST_TOKEN)
    assert (result.ok, result.reason) == (False, "account_mismatch")
    assert len(kite.logouts) == 1 and rt.tokens.current().access_token == ACCESS_TOKEN


async def test_a_failed_invalidation_is_reported_and_still_stores_nothing(runtime_factory, fake_kite):
    kite = KiteSession(fake_kite, user_id="ZZ9999")
    kite.logout_error = (500, "GeneralException")
    rt = runtime_factory(KITE_MCP_ALLOWED_USER_ID=USER_ID)
    result = await rt.login.complete(REQUEST_TOKEN)
    assert (result.ok, result.reason) == (False, "account_mismatch")
    assert "failed" in result.message and rt.tokens.current().access_token == ACCESS_TOKEN
    assert [e for e in rt.audit.read() if e["event"] == "login_refused"][-1]["session_ended"] is False


async def test_a_token_that_cannot_be_stored_is_not_left_alive(rt, kite, monkeypatch):
    def refuse(self: TokenFile, record: object) -> None:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(TokenFile, "write", refuse)
    state = await new_state(rt)
    result = await rt.login.handle_callback(callback_params(state))
    assert (result.ok, result.reason) == (False, "token_not_stored")
    assert kite.logouts == [{"api_key": API_KEY, "access_token": NEW_TOKEN}]
    assert "reservation" not in stored_states(rt)[digest_of(state)]
    assert rt.tokens.current().access_token == ACCESS_TOKEN


# --- a pasted token -------------------------------------------------------------------------------------------------


async def test_a_state_file_that_cannot_be_updated_does_not_undo_a_login(rt, kite, monkeypatch):
    def broken(self: StateStore, reservation: Reservation | None, now: dt.datetime) -> None:
        raise StateStoreError("disk full")

    monkeypatch.setattr(StateStore, "commit", broken)
    result = await rt.login.handle_callback(callback_params(await new_state(rt)))
    assert result.ok and rt.tokens.current().access_token == NEW_TOKEN  # the token is on disk: the login stands


async def test_a_login_clears_an_earlier_rejection_of_the_same_token(rt, kite):
    rt.tokens.reject(NEW_TOKEN)  # Kite once said no to this exact string
    assert (await rt.login.complete(REQUEST_TOKEN)).ok
    assert rt.tokens.current().access_token == NEW_TOKEN


async def test_complete_without_a_state_stores_the_token_and_closes_open_links(rt, kite):
    open_link = await new_state(rt)
    result = await rt.login.complete(f"  {REQUEST_TOKEN}\n")
    assert result.ok and rt.tokens.current().access_token == NEW_TOKEN
    assert (await rt.login.handle_callback(callback_params(open_link))).reason == "bad_state"


async def test_complete_with_a_state_uses_it_up_and_a_bad_state_is_refused(rt, kite):
    state = await new_state(rt)
    bad = await rt.login.complete(REQUEST_TOKEN, STATE)  # well formed but never issued
    assert (bad.ok, bad.reason) == (False, "bad_state") and kite.exchanges == []
    malformed = await rt.login.complete(REQUEST_TOKEN, "short")
    assert (malformed.ok, malformed.reason) == (False, "bad_state") and kite.exchanges == []
    assert (await rt.login.complete(REQUEST_TOKEN, state)).ok
    assert stored_states(rt)[digest_of(state)]["status"] == "used"


async def test_complete_refuses_what_is_not_a_request_token(rt, kite):
    for text in ("", "short", "has space in it 12345", "x" * 200):
        result = await rt.login.complete(text)
        assert (result.ok, result.reason) == (False, "bad_request")
    assert kite.exchanges == []


def test_a_pasted_url_gives_the_token_and_the_state():
    url = f"http://127.0.0.1:8765/callback?state={STATE}&action=login&type=login&status=success&request_token={REQUEST_TOKEN}"
    assert unpack_redirect(url) == (REQUEST_TOKEN, STATE)
    assert unpack_redirect(url.partition("?")[2]) == (REQUEST_TOKEN, STATE)  # just the query
    assert unpack_redirect(url, "other-state") == (REQUEST_TOKEN, "other-state")  # an explicit state wins
    assert unpack_redirect(REQUEST_TOKEN) == (REQUEST_TOKEN, None)
    assert unpack_redirect(f" {REQUEST_TOKEN} ", "s") == (REQUEST_TOKEN, "s")
    assert unpack_redirect(f"https://x.invalid/cb?request_token={REQUEST_TOKEN}#frag") == (REQUEST_TOKEN, None)


@pytest.mark.parametrize(
    "pasted",
    [
        "http://127.0.0.1:8765/callback?request_token=aaaaaaaaaa&request_token=bbbbbbbbbb",
        "http://127.0.0.1:8765/callback?status=error&request_token=aaaaaaaaaa",
        "http://127.0.0.1:8765/callback?request_token=&x=1",
    ],
)
def test_a_pasted_url_must_hold_exactly_one_successful_request_token(pasted):
    with pytest.raises(KiteMCPError):
        unpack_redirect(pasted)


# --- external mode --------------------------------------------------------------------------------------------------


async def test_external_mode_refuses_everything_and_sends_nothing(runtime_factory, fake_kite):
    kite = KiteSession(fake_kite)
    rt = runtime_factory(KITE_MCP_AUTH_MODE="external")
    token_file = rt.settings.token_file.read_text()
    calls = [
        rt.login.issue_link(),
        rt.login.handle_callback(callback_params(STATE)),
        rt.login.complete(REQUEST_TOKEN),
        rt.login.complete(REQUEST_TOKEN, STATE),
        rt.login.logout(),
    ]
    for call in calls:
        with pytest.raises(KiteMCPError, match="external"):
            await call
    assert fake_kite.requests == [] and kite.exchanges == [] and kite.logouts == []
    assert rt.settings.token_file.read_text() == token_file
    assert not state_file(rt).exists()
    assert rt.tokens.current().access_token == ACCESS_TOKEN


# --- logout ---------------------------------------------------------------------------------------------------------


async def test_logout_ends_the_session_removes_the_file_and_stops_serving_the_token(rt, kite):
    token_file = rt.settings.token_file
    result = await rt.login.logout()
    assert (result.kite, result.file_removed) == ("ended", True)
    assert kite.logouts == [{"api_key": API_KEY, "access_token": ACCESS_TOKEN}]
    assert not token_file.exists()
    assert rt.tokens.current() is None
    # even if the same token reappears on disk, it is not served again; a different one is
    token_file.write_text(json.dumps({"access_token": ACCESS_TOKEN, "expires_at": "2026-11-05T06:00:00+05:30"}))
    assert rt.tokens.current() is None
    token_file.write_text(json.dumps({"access_token": NEW_TOKEN, "expires_at": "2026-11-05T06:00:00+05:30"}))
    assert rt.tokens.current().access_token == NEW_TOKEN
    assert [e["event"] for e in rt.audit.read()] == ["logout_ok"]


async def test_logout_also_closes_every_open_login_link(rt, kite):
    link = await new_state(rt)
    await rt.login.logout()
    assert (await rt.login.handle_callback(callback_params(link))).reason == "bad_state"
    assert kite.exchanges == []


async def test_a_failed_logout_leaves_the_login_links_alone(rt, kite):
    link = await new_state(rt)
    kite.logout_error = (500, "GeneralException")
    with pytest.raises(KiteMCPError):
        await rt.login.logout()
    assert (await rt.login.handle_callback(callback_params(link))).ok


async def test_logout_of_a_token_kite_no_longer_knows_still_cleans_up(rt, kite):
    kite.logout_error = (403, "TokenException")
    result = await rt.login.logout()
    assert (result.kite, result.file_removed) == ("already_invalid", True)
    assert not rt.settings.token_file.exists()


async def test_logout_keeps_the_file_when_kite_cannot_be_told(rt, kite):
    kite.logout_error = (500, "GeneralException")
    with pytest.raises(KiteMCPError, match="token file was kept"):
        await rt.login.logout()
    assert rt.settings.token_file.exists() and rt.tokens.current().access_token == ACCESS_TOKEN
    assert [e["event"] for e in rt.audit.read()] == ["logout_failed"]


async def test_logout_of_an_expired_token_does_not_depend_on_kite(settings_factory, fake_kite, kite):
    rt = runtime_at(
        settings_factory(), fake_kite, FIXED_NOW + dt.timedelta(days=1)
    )  # the stored token expired at 06:00
    kite.logout_error = (500, "GeneralException")
    result = await rt.login.logout()
    assert (result.kite, result.file_removed) == ("already_invalid", True)


async def test_logout_still_stops_serving_the_token_if_the_file_cannot_be_deleted(rt, kite, monkeypatch):
    def stuck(self: TokenFile) -> bool:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(TokenFile, "remove", stuck)
    with pytest.raises(KiteMCPError, match="session was ended, but the token file could not be deleted"):
        await rt.login.logout()
    assert kite.logouts == [{"api_key": API_KEY, "access_token": ACCESS_TOKEN}]
    assert rt.tokens.current() is None  # the file is still there, but its token is not served
    assert [e["event"] for e in rt.audit.read()] == ["logout_failed"]


async def test_logout_with_no_token_just_removes_an_unusable_file(rt, kite):
    rt.settings.token_file.write_text("garbage that is not a token!")
    result = await rt.login.logout()
    assert (result.kite, result.file_removed) == ("no_token", True)
    assert kite.logouts == []
    assert (await rt.login.logout()).file_removed is False


# --- the callback route ---------------------------------------------------------------------------------------------


@pytest.fixture
def http_app(mcp_factory, kite):
    server, _, rt = mcp_factory(KITE_MCP_TRANSPORT="http", KITE_MCP_BEARER_TOKEN=BEARER)
    return build_http_app(server, rt), rt


def web(app, peer: str = "203.0.113.7") -> httpx2.AsyncClient:
    transport = httpx2.ASGITransport(app=app, client=(peer, 40000))
    return httpx2.AsyncClient(transport=transport, base_url="http://127.0.0.1:8765")


def signature(response: httpx2.Response) -> tuple:
    return response.status_code, response.text, dict(response.headers)


REFUSED = (400, "Login not completed.")


async def test_callback_success_needs_no_authentication_and_stores_the_token(http_app, kite):
    app, rt = http_app
    state = await new_state(rt)
    async with web(app) as http:
        response = await http.get("/callback", params=callback_params(state))
    assert response.status_code == 200
    assert "Kite login done, you can close this tab" in response.text
    assert response.headers["content-type"].startswith("text/html")
    for name, value in SECURITY_HEADERS.items():
        assert response.headers[name] == value
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-security-policy"] == "default-src 'none'"
    for secret in (NEW_TOKEN, REQUEST_TOKEN, state, USER_ID):
        assert secret not in response.text and secret not in str(response.headers)
    assert rt.tokens.current().access_token == NEW_TOKEN


async def test_every_refusal_is_the_same_response(http_app, kite):
    app, rt = http_app
    state = await new_state(rt)
    spent = await new_state(rt)
    seen: dict[str, tuple] = {}
    peers = iter(range(1, 50))

    async def get(label: str, params=None) -> None:
        async with web(app, f"203.0.113.{next(peers)}") as http:  # one client each: the rate limit is not the subject
            seen[label] = signature(await http.get("/callback", params=params))

    await get("no query")
    await get("status", {**callback_params(state), "status": "failed"})
    await get("unknown state", callback_params(STATE))
    await get("bad token", callback_params(state, "bad token"))
    await get("two states", [*callback_params(state).items(), ("state", state)])
    assert kite.exchanges == []
    kite.fail_next = True
    await get("kite refuses", callback_params(state))
    async with web(app, "203.0.113.99") as http:  # a login that works...
        assert (await http.get("/callback", params=callback_params(spent))).status_code == 200
    await get("replay", callback_params(spent))  # ...and its replay

    first = seen["no query"]
    assert first[:2] == REFUSED
    assert {name: sig for name, sig in seen.items() if sig != first} == {}
    for name, value in SECURITY_HEADERS.items():
        assert first[2][name.lower()] == value


async def test_head_requests_never_run_an_exchange(http_app, kite):
    app, rt = http_app
    state = await new_state(rt)
    async with web(app) as http:
        response = await http.head("/callback", params=callback_params(state))
    assert response.status_code == 400 and kite.exchanges == []
    for name, value in SECURITY_HEADERS.items():
        assert response.headers[name] == value
    assert (await rt.login.handle_callback(callback_params(state))).ok  # the link is untouched


async def test_a_login_by_the_wrong_account_gets_the_same_refusal(mcp_factory, fake_kite):
    kite = KiteSession(fake_kite, user_id="ZZ9999")
    server, _, rt = mcp_factory(
        KITE_MCP_TRANSPORT="http", KITE_MCP_BEARER_TOKEN=BEARER, KITE_MCP_ALLOWED_USER_ID=USER_ID
    )
    app = build_http_app(server, rt)
    async with web(app) as http:
        wrong = await http.get("/callback", params=callback_params(await new_state(rt)))
        junk = await http.get("/callback")
    assert signature(wrong) == signature(junk)
    assert (wrong.status_code, wrong.text) == REFUSED and len(kite.logouts) == 1


async def test_mcp_still_needs_the_bearer_token_and_health_is_public(http_app):
    app, _ = http_app
    async with web(app) as http:
        assert (await http.post("/mcp", json={})).status_code == 401
        wrong = await http.post("/mcp", json={}, headers={"Authorization": "Bearer " + "x" * 40})
        assert wrong.status_code == 401
        assert (await http.get("/health")).status_code == 200
        assert (await http.get("/callback")).status_code == 400  # reachable without any Authorization header


async def test_the_callback_path_is_configurable(mcp_factory, kite):
    server, _, rt = mcp_factory(
        KITE_MCP_TRANSPORT="http", KITE_MCP_BEARER_TOKEN=BEARER, KITE_MCP_CALLBACK_PATH="/kite/done"
    )
    app = build_http_app(server, rt)
    state = await new_state(rt)
    async with web(app) as http:
        assert (await http.get("/callback", params=callback_params(state))).status_code == 404
        assert (await http.get("/kite/done", params=callback_params(state))).status_code == 200
    assert rt.login.redirect_url() == "http://127.0.0.1:8765/kite/done"


async def test_the_redirect_url_is_the_public_url_when_there_is_one(runtime_factory, kite):
    local = runtime_factory()
    public = runtime_factory(KITE_MCP_PUBLIC_URL="https://kite.example.invalid/")
    assert local.login.redirect_url() == "http://127.0.0.1:8765/callback"
    assert public.login.redirect_url() == "https://kite.example.invalid/callback"


async def test_external_mode_has_no_callback_route(mcp_factory, kite):
    server, _, rt = mcp_factory(KITE_MCP_AUTH_MODE="external", KITE_MCP_TRANSPORT="http", KITE_MCP_BEARER_TOKEN=BEARER)
    assert not rt.extras.get("http_routes")
    app = build_http_app(server, rt)
    async with web(app) as http:
        assert (await http.get("/callback", params=callback_params(STATE))).status_code == 404
    assert kite.exchanges == []


async def test_the_callback_is_rate_limited_per_client(http_app, kite):
    app, rt = http_app
    state = await new_state(rt)
    async with web(app, "203.0.113.7") as http:
        for _ in range(10):
            assert (await http.get("/callback")).status_code == 400
        limited = await http.get("/callback", params=callback_params(state))  # valid, but too many requests
        assert (limited.status_code, limited.text) == REFUSED
        assert kite.exchanges == []
    async with web(app, "203.0.113.8") as other:  # somebody else is not affected
        assert (await other.get("/callback", params=callback_params(state))).status_code == 200


async def test_forwarded_for_is_believed_only_from_a_loopback_peer(http_app, kite):
    app, rt = http_app
    state = await new_state(rt)
    async with web(app, "127.0.0.1") as proxy:  # a reverse proxy on this machine
        for _ in range(10):
            await proxy.get("/callback", headers={"X-Forwarded-For": "198.51.100.1, 198.51.100.9"})
        same = await proxy.get(
            "/callback", params=callback_params(state), headers={"X-Forwarded-For": "10.0.0.5, 198.51.100.9"}
        )
        assert same.status_code == 400  # the LAST entry is the client, and it has used up its requests
        other = await proxy.get(
            "/callback", params=callback_params(state), headers={"X-Forwarded-For": "198.51.100.9, 198.51.100.77"}
        )
        assert other.status_code == 200  # another client behind the same proxy


async def test_forwarded_for_from_any_other_peer_is_ignored(http_app, kite):
    app, rt = http_app
    state = await new_state(rt)
    async with web(app, "203.0.113.7") as http:
        for number in range(10):
            await http.get("/callback", headers={"X-Forwarded-For": f"198.51.100.{number}"})
        spoofed = await http.get(
            "/callback", params=callback_params(state), headers={"X-Forwarded-For": "198.51.100.200"}
        )
    assert (spoofed.status_code, spoofed.text) == REFUSED
    assert kite.exchanges == []


async def test_an_oversized_query_is_refused_before_it_is_looked_at(http_app, kite):
    app, rt = http_app
    state = await new_state(rt)
    async with web(app) as http:
        response = await http.get("/callback", params={**callback_params(state), "junk": "a" * 5000})
        assert (response.status_code, response.text) == REFUSED  # otherwise valid, but far bigger than Kite's
        assert kite.exchanges == []
        assert (await http.get("/callback", params=callback_params(state))).status_code == 200


def test_rate_limiter_window_slides_and_forgets_clients():
    now = [1000.0]
    limiter = CallbackRateLimiter(limit=3, period_s=60.0, clock=lambda: now[0], max_clients=2)
    assert [limiter.allow("a") for _ in range(4)] == [True, True, True, False]
    assert limiter.allow("b") is True
    assert limiter.allow("c") is False  # a third client while two are tracked: refused, not remembered
    now[0] += 60.0
    assert limiter.allow("a") is True and limiter.allow("c") is True  # the window moved on; idle clients forgotten


async def test_junk_aimed_at_the_callback_cannot_flood_the_audit_log(http_app, monkeypatch):
    app, rt = http_app
    monkeypatch.setattr(login_module, "NOISE_BURST", 3)
    for number in range(8):
        async with web(app, f"203.0.113.{number + 1}") as http:
            await http.get("/callback", params={"status": "success"})
    refusals = [e for e in rt.audit.read() if e["event"] == "login_refused"]
    assert len(refusals) == 3
    assert {e["reason"] for e in refusals} == {"bad_request"} and refusals[0]["client"] == "203.0.113.1"


async def test_the_audit_log_names_what_happened_without_secrets(http_app, kite):
    app, rt = http_app
    state = await new_state(rt)
    async with web(app) as http:
        await http.get("/callback", params={**callback_params(state), "request_token": "wrong-request-token-000000"})
        kite.fail_next = False
        await http.get("/callback", params=callback_params(state))
    events = rt.audit.read()
    assert [e["event"] for e in events] == ["login_link_issued", "login_refused", "login_ok"]
    assert events[1]["reason"] == "exchange_failed" and events[1]["source"] == "callback"
    assert events[2]["user_id"] == USER_ID and events[2]["source"] == "callback"
    text = json.dumps(events)
    for secret in (
        state,
        REQUEST_TOKEN,
        "wrong-request-token-000000",
        NEW_TOKEN,
        ACCESS_TOKEN,
        API_SECRET,
        *FORBIDDEN_IN_LOGS,
    ):
        assert secret not in text


async def test_nothing_secret_reaches_the_logs(http_app, kite, caplog):
    app, rt = http_app
    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.DEBUG, logger="httpx2")  # in case a CLI run elsewhere raised it
    state = await new_state(rt)
    async with web(app) as http:
        await http.get("/callback", params=callback_params(state, "wrong-request-token-000000"))
        await http.get("/callback", params=callback_params(state))
    assert (await rt.login.logout()).kite == "ended"  # the DELETE carries the access token in its URL
    other = await rt.login.complete(REQUEST_TOKEN)  # spent: refused
    assert not other.ok

    assert any(r.name == "httpx2" and "DELETE" in r.getMessage() for r in caplog.records), "the URL log is the point"
    secrets = (
        NEW_TOKEN,
        ACCESS_TOKEN,
        REQUEST_TOKEN,
        "wrong-request-token-000000",
        state,
        API_SECRET,
        *FORBIDDEN_IN_LOGS,
    )
    for record in caplog.records:
        if record.name == "httpx2" and "http://127.0.0.1:8765/" in record.getMessage():
            continue  # the TEST's own client logging the request it makes to the app: a browser logs nothing here
        text = f"{record.getMessage()} {record.args!r} {record.exc_text or ''}"
        for secret in secrets:
            assert secret not in text, (record.name, record.getMessage())
    assert "access_token=[redacted]" in caplog.text


# --- the CLI --------------------------------------------------------------------------------------------------------


async def run_login(settings, fake_kite, **kwargs):
    return await cli_login(settings, transport=fake_kite.transport(), clock=lambda: FIXED_NOW, **kwargs)


async def test_cli_login_prints_the_link_on_stdout_and_the_rest_on_stderr(settings_factory, fake_kite, capsys):
    code = await run_login(settings_factory(), fake_kite, request_token=None, listen=False)
    out, err = capsys.readouterr()
    assert code == 0
    assert len(out.strip().splitlines()) == 1
    assert out.startswith("https://kite.zerodha.com/connect/login?v=3&api_key=")
    assert "http://127.0.0.1:8765/callback" in err and "--request-token" in err
    assert "WARNING: AI systems are unpredictable and non-deterministic." in err and "WARNING" not in out
    assert state_of(out.strip()) not in err
    assert fake_kite.requests == []  # a link needs no call to Kite


async def test_cli_login_exchanges_a_request_token(settings_factory, fake_kite, capsys):
    kite = KiteSession(fake_kite)
    settings = settings_factory()
    code = await run_login(settings, fake_kite, request_token=REQUEST_TOKEN, listen=False)
    out, err = capsys.readouterr()
    assert code == 0 and f"Logged in as {USER_ID}" in out and "2026-11-05 06:00 IST" in out
    for secret in (NEW_TOKEN, REQUEST_TOKEN):
        assert secret not in out + err
    assert json.loads(settings.token_file.read_text())["access_token"] == NEW_TOKEN
    assert len(kite.exchanges) == 1


async def test_cli_login_accepts_the_whole_redirected_url(settings_factory, fake_kite, capsys):
    KiteSession(fake_kite)
    settings = settings_factory()
    rt = runtime_at(settings, fake_kite, FIXED_NOW)
    state = await new_state(rt)
    pasted = f"http://127.0.0.1:8765/callback?state={state}&status=success&request_token={REQUEST_TOKEN}"
    assert await run_login(settings, fake_kite, request_token=pasted, listen=False) == 0
    assert stored_states(rt)[digest_of(state)]["status"] == "used"


async def test_cli_login_reports_a_refused_token(settings_factory, fake_kite, capsys):
    kite = KiteSession(fake_kite)
    kite.fail_next = True
    settings = settings_factory()
    before = settings.token_file.read_text()
    code = await run_login(settings, fake_kite, request_token=REQUEST_TOKEN, listen=False)
    out, err = capsys.readouterr()
    assert code == 1 and "login failed" in err and out == ""
    assert settings.token_file.read_text() == before


async def test_cli_login_in_external_mode_is_refused_with_a_message(settings_factory, fake_kite, capsys):
    kite = KiteSession(fake_kite)
    for options in ({"request_token": None, "listen": False}, {"request_token": REQUEST_TOKEN, "listen": False}):
        code = await run_login(settings_factory(KITE_MCP_AUTH_MODE="external"), fake_kite, **options)
        assert code == 2
    _, err = capsys.readouterr()
    assert "external" in err
    assert fake_kite.requests == [] and kite.exchanges == []


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Output:
    """capsys, accumulated: readouterr() empties the buffers, and the listen tests read them several times."""

    def __init__(self, capsys):
        self.capsys = capsys
        self.out = self.err = ""

    def poll(self) -> None:
        captured = self.capsys.readouterr()
        self.out += captured.out
        self.err += captured.err


async def wait_for_link(output: Output) -> str:
    for _ in range(200):
        output.poll()
        link = next((line for line in output.out.splitlines() if line.startswith("https://")), None)
        if link:
            return link
        await asyncio.sleep(0.05)
    raise AssertionError("no login link was printed")


async def test_cli_login_listen_serves_the_callback_until_one_login_completes(settings_factory, fake_kite, capsys):
    KiteSession(fake_kite)
    port = free_port()
    settings = settings_factory(KITE_MCP_PORT=str(port))
    task = asyncio.create_task(run_login(settings, fake_kite, request_token=None, listen=True))
    output = Output(capsys)
    state = state_of(await wait_for_link(output))
    base = f"http://127.0.0.1:{port}"
    async with httpx2.AsyncClient() as http:
        for _ in range(100):  # the server needs a moment to come up
            try:
                other = await http.get(f"{base}/health")  # only the callback path is served
                break
            except httpx2.ConnectError:
                await asyncio.sleep(0.05)
        assert other.status_code == 404
        junk = await http.get(f"{base}/callback")
        assert (junk.status_code, junk.text) == REFUSED
        done = await http.get(f"{base}/callback", params=callback_params(state))
    assert done.status_code == 200 and "Kite login done" in done.text
    assert await asyncio.wait_for(task, 15) == 0
    output.poll()
    assert f"http://127.0.0.1:{port}/callback" in output.err  # says which redirect URL this needs
    assert json.loads(settings.token_file.read_text())["access_token"] == NEW_TOKEN


async def test_cli_login_listen_gives_up_after_the_timeout(settings_factory, fake_kite, capsys, monkeypatch):
    monkeypatch.setattr(login_module, "LISTEN_TIMEOUT_S", 0.3)
    settings = settings_factory(KITE_MCP_PORT=str(free_port()))
    code = await asyncio.wait_for(run_login(settings, fake_kite, request_token=None, listen=True), 15)
    _, err = capsys.readouterr()
    assert code == 1 and "no login arrived within" in err


async def test_cli_login_listen_says_so_when_the_port_is_taken(settings_factory, fake_kite, capsys):
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        settings = settings_factory(KITE_MCP_PORT=str(busy.getsockname()[1]))
        code = await asyncio.wait_for(run_login(settings, fake_kite, request_token=None, listen=True), 15)
    _, err = capsys.readouterr()
    assert code == 1 and "could not listen" in err


async def test_cli_logout_needs_yes(settings_factory, fake_kite, capsys):
    kite = KiteSession(fake_kite)
    settings = settings_factory()
    code = await cli_logout(settings, yes=False, transport=fake_kite.transport(), clock=lambda: FIXED_NOW)
    _, err = capsys.readouterr()
    assert code == 2 and "--yes" in err
    assert settings.token_file.exists() and kite.logouts == []


async def test_cli_logout_ends_the_session_and_removes_the_file(settings_factory, fake_kite, capsys):
    kite = KiteSession(fake_kite)
    settings = settings_factory()
    code = await cli_logout(settings, yes=True, transport=fake_kite.transport(), clock=lambda: FIXED_NOW)
    out, _ = capsys.readouterr()
    assert code == 0 and "Logged out" in out and ACCESS_TOKEN not in out
    assert not settings.token_file.exists()
    assert kite.logouts == [{"api_key": API_KEY, "access_token": ACCESS_TOKEN}]


async def test_cli_logout_in_external_mode_does_nothing(settings_factory, fake_kite, capsys):
    kite = KiteSession(fake_kite)
    settings = settings_factory(KITE_MCP_AUTH_MODE="external")
    code = await cli_logout(settings, yes=True, transport=fake_kite.transport(), clock=lambda: FIXED_NOW)
    _, err = capsys.readouterr()
    assert code == 2 and "external" in err
    assert settings.token_file.exists() and fake_kite.requests == [] and kite.logouts == []


@pytest.fixture
def cli_env(base_env, monkeypatch):
    """The environment a real `kite-connect-mcp` run sees; main() also lowers some log levels, so put them back."""
    names = ("mcp", "httpx2", "httpcore2", "uvicorn.access", "websockets")
    levels = {name: logging.getLogger(name).level for name in names}
    for key, value in base_env.items():
        monkeypatch.setenv(key, value)
    yield base_env
    for name, level in levels.items():
        logging.getLogger(name).setLevel(level)


def test_main_login_prints_only_the_link_on_stdout(cli_env, capsys):
    assert main(["login"]) == 0
    out, err = capsys.readouterr()
    assert len(out.strip().splitlines()) == 1
    assert out.startswith("https://kite.zerodha.com/connect/login?v=3&api_key=")
    assert "Kite redirects to http://127.0.0.1:8765/callback" in err
    stored = Path(cli_env["KITE_MCP_STATE_DIR"], "login_states.json").read_text()
    state = state_of(out.strip())
    assert digest_of(state) in stored and state not in stored  # the link is open; only its digest is on disk


def test_main_logout_changes_nothing_without_yes(cli_env, capsys):
    assert main(["logout"]) == 2
    assert "--yes" in capsys.readouterr().err
    assert Path(cli_env["KITE_TOKEN_FILE"]).exists()


def test_main_login_in_external_mode_fails_with_a_message(cli_env, monkeypatch, capsys):
    monkeypatch.setenv("KITE_MCP_AUTH_MODE", "external")
    assert main(["login"]) == 2
    out, err = capsys.readouterr()
    assert out == "" and "external" in err


def test_the_login_help_names_the_redirect_url_that_listen_needs(capsys):
    args = build_parser().parse_args(["login", "--listen", "--request-token", "abc"])
    assert (args.listen, args.request_token) == (True, "abc")
    with pytest.raises(SystemExit):
        build_parser().parse_args(["login", "--help"])
    help_text = " ".join(capsys.readouterr().out.split())
    assert "http://127.0.0.1:<port><callback path>" in help_text and "10 minutes" in help_text
