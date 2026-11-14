"""The login link service: the link reaches the command and nothing else; the state file keeps the last attempt, the
last scheduled slot and the last link for a rejected token, never a link (this part); and the schedule, the links
after a rejected token and the server's lifespan (Task 9)."""

from __future__ import annotations

import asyncio
import json
import logging
import stat
from pathlib import Path

import pytest
import uvicorn
from uvicorn.lifespan.on import LifespanOn

from conftest import ACCESS_TOKEN, BEARER, close_runtimes, write_token_file
from events_helpers import Clock
from kite_connect_mcp.auth import reminders
from kite_connect_mcp.auth.reminders import STATE_FILE, LoginLinks, SkipDates, latest_slot
from kite_connect_mcp.cli import main
from kite_connect_mcp.errors import ConfigError, KiteMCPError
from kite_connect_mcp.server import build_http_app, build_runtime, build_server
from kite_login import state_of
from links_helpers import BACKGROUND, at, ended, link_command, links_env, pids, script, until

HTTP = {"KITE_MCP_TRANSPORT": "http", "KITE_MCP_BEARER_TOKEN": BEARER}
EVENTS = {**HTTP, "KITE_MCP_EVENTS": "true"}
TIMES = {"KITE_MCP_LOGIN_REMINDER_TIMES": "07:30,08:30"}
SLOT = at(2, 7, 30)  # Monday 2 November, 07:30


async def _no_sleep(_: float) -> None:
    return None


@pytest.fixture
def links(settings_factory, fake_kite, tmp_path, monkeypatch):
    """(make, out): make(clock, command=None, **env) builds a runtime whose link service runs a recording link command;
    `out` is where that command writes what it was given (the last run only)."""
    out = tmp_path / "got.json"
    monkeypatch.setenv("LINK_OUT", str(out))
    monkeypatch.setenv("NOT_PASSED", "x")
    default = link_command(tmp_path)
    made = []

    def make(clock, command=None, **env):
        settings = settings_factory(**links_env(command or default, **TIMES, **env))
        rt = build_runtime(settings, transport=fake_kite.transport(), clock=clock, sleep=_no_sleep)
        made.append(rt)
        return rt

    yield make, out
    close_runtimes(made)


def logged_out(rt):
    rt.settings.token_file.unlink()
    return rt


def issued(rt) -> list[str]:
    """The source of every login link issued so far (the audit log): one per link handed to the command."""
    return [e["source"] for e in rt.audit.read() if e["event"] == "login_link_issued"]


async def test_a_link_goes_to_the_command_and_nowhere_else(links, caplog):
    make, out = links
    rt = logged_out(make(Clock(SLOT), **EVENTS))
    caplog.set_level(logging.DEBUG)
    attempt = await rt.links.send("scheduled", slot=SLOT)
    got = json.loads(out.read_text())
    assert set(got) == {"url", "expires_at", "reason"} and got["reason"] == "scheduled"
    assert got["url"].startswith("https://kite.zerodha.com/connect/login?v=3&api_key=")
    assert got["expires_at"] == "2026-11-02T08:30:00+05:30"  # KITE_MCP_LOGIN_LINK_TTL_S, 3600 by default
    names = set(out.with_name("got.json.env").read_text().split())
    assert {"PATH", "LANG", "LINK_OUT"} <= names and "NOT_PASSED" not in names
    assert not [n for n in names if n.startswith("KITE_")]
    assert attempt.outcome == "sent" and rt.links.status()["last"] == {
        "at": "2026-11-02T07:30:00+05:30", "reason": "scheduled", "outcome": "sent", "error": None,
        "link_expires_at": "2026-11-02T08:30:00+05:30",
    }  # fmt: skip
    state = state_of(got["url"])
    db = rt.settings.events_db
    stored = b"".join(p.read_bytes() for p in db.parent.glob(db.name + "*")).decode("utf-8", "replace")  # and its WAL
    places = [
        caplog.text, (rt.settings.state_dir / STATE_FILE).read_text(), json.dumps(rt.audit.read()),
        json.dumps(rt.links.status()), stored,
    ]  # fmt: skip
    for place in places:  # spec 5.13: never in a log, the state file, the audit log, a status or the event store
        assert state not in place and got["url"] not in place
    assert issued(rt) == ["scheduled"]


async def test_a_failing_command_is_recorded_with_a_short_error_and_uses_up_its_slot(links, tmp_path):
    make, _ = links
    rt = logged_out(make(Clock(SLOT), command=link_command(tmp_path, "fails", exit_code=3)))
    attempt = await rt.links.send("scheduled", slot=SLOT)
    assert (attempt.outcome, attempt.error) == ("failed", "exit 3")
    assert rt.links.status()["last_scheduled_for"] == "2026-11-02T07:30+05:30"


async def test_a_command_someone_else_could_change_is_never_run(links):
    make, out = links
    rt = logged_out(make(Clock(at(2, 12, 0))))
    rt.links.command.chmod(0o777)
    attempt = await rt.links.send("requested")
    assert attempt.outcome == "failed" and "writable by group or others" in attempt.error
    assert not out.exists() and not (rt.settings.state_dir / "login_states.json").exists()  # no link was issued


async def test_only_a_scheduled_attempt_records_its_slot(links):
    make, _ = links
    clock = Clock(SLOT)
    rt = logged_out(make(clock))
    await rt.links.send("scheduled", slot=SLOT)
    clock.now = at(2, 7, 45)
    await rt.links.send("requested")
    await rt.links.send("token_rejected")
    path = rt.settings.state_dir / STATE_FILE
    data = json.loads(path.read_text())
    assert data["last_scheduled_for"] == "2026-11-02T07:30:00+05:30"  # requested and token_rejected never touch it
    assert data["token_rejected_link_at"] == "2026-11-02T07:45:00+05:30" and data["last"]["reason"] == "token_rejected"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and "https://" not in path.read_text()
    assert rt.links.handled_slot() == SLOT
    assert make(clock).links.handled_slot() == SLOT  # a new process on the same state folder knows it too
    with pytest.raises(ValueError):
        await rt.links.send("requested", slot=SLOT)  # only a scheduled attempt has a slot


async def test_a_state_file_that_cannot_be_written_still_holds_the_slot(links, monkeypatch, caplog):
    make, _ = links
    rt = logged_out(make(Clock(SLOT)))

    def cannot(path, state):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(reminders, "write_state", cannot)
    with caplog.at_level(logging.WARNING):
        assert (await rt.links.send("scheduled", slot=SLOT)).outcome == "sent"
    assert rt.links.handled_slot() == SLOT and "could not write" in caplog.text


@pytest.mark.parametrize(
    "damaged",
    [
        "not json",
        "[]",
        '{"last": 5, "last_scheduled_for": "x"}',
        # a time without an offset, a moment that is not a string
        '{"last": {"at": "2026-11-02T07:30:00", "reason": "scheduled", "outcome": "sent"}, '
        '"token_rejected_link_at": 7}',
        '{"last": {"at": "2026-11-02T07:30:00+05:30", "reason": "other", "outcome": "sent"}}',  # an unknown reason
        "[" * 100_000,  # nested too deep for the JSON parser (RecursionError)
    ],
)
async def test_a_damaged_state_file_reads_as_nothing_and_is_replaced(links, damaged):
    make, _ = links
    rt = logged_out(make(Clock(SLOT)))
    path = rt.settings.state_dir / STATE_FILE
    path.write_text(damaged)
    status = rt.links.status()
    assert (status["last"], status["last_scheduled_for"], rt.links.handled_slot()) == (None, None, None)
    assert (await rt.links.send("scheduled", slot=SLOT)).outcome == "sent"
    data = json.loads(path.read_text())
    assert data["last_scheduled_for"] == "2026-11-02T07:30:00+05:30" and data["token_rejected_link_at"] is None
    assert data["last"]["outcome"] == "sent" and stat.S_IMODE(path.stat().st_mode) == 0o600


def test_the_link_service_needs_a_link_command(runtime_factory):
    rt = runtime_factory()
    assert rt.links is None  # without KITE_MCP_LOGIN_LINK_COMMAND there is no link service
    with pytest.raises(ConfigError, match="KITE_MCP_LOGIN_LINK_COMMAND"):
        LoginLinks(rt.settings, rt.login)


def test_the_status_of_a_process_that_never_sends(links):
    make, out = links
    rt = make(Clock(SLOT))
    assert rt.links.status() == {
        "times": ["07:30", "08:30"], "days": [0, 1, 2, 3, 4], "next_at": "2026-11-02T08:30+05:30",
        "last_scheduled_for": None, "last": None,
    }  # fmt: skip
    assert rt.links.handled_slot() is None
    assert not out.exists() and not (rt.settings.state_dir / STATE_FILE).exists()  # reading the status writes nothing


async def test_no_link_while_forcing_is_off_and_a_token_is_valid(links):
    make, out = links
    rt = make(Clock(SLOT), KITE_MCP_LOGIN_ALLOW_FORCE="false")
    with pytest.raises(ConfigError, match="KITE_MCP_LOGIN_ALLOW_FORCE"):
        await rt.links.send("scheduled", slot=SLOT)
    # nothing recorded: the caller decides
    assert not out.exists() and not (rt.settings.state_dir / STATE_FILE).exists()
    assert rt.links.handled_slot() is None and issued(rt) == []
    assert [e["source"] for e in rt.audit.read() if e["event"] == "login_link_refused"] == ["scheduled"]


async def test_a_link_that_cannot_be_issued_is_logged_and_failed_with_a_clean_short_error(links, monkeypatch, caplog):
    make, out = links
    rt = logged_out(make(Clock(SLOT)))

    async def broken(**_):
        raise KiteMCPError("the login state\nfile is \x1b[31mlocked " + "again " * 100)

    monkeypatch.setattr(rt.login, "issue_link", broken)
    with caplog.at_level(logging.WARNING):
        attempt = await rt.links.send("scheduled", slot=SLOT)
    assert attempt.outcome == "failed" and attempt.error.startswith("the login state file is locked again again")
    assert len(attempt.error) <= 200 and attempt.error.endswith(" again")  # cut at a whole word
    # the journal says why too, in the same clean words: the unattended loop logs nothing more
    assert f"login links: no scheduled link could be issued ({attempt.error})" in caplog.text
    assert "\x1b" not in caplog.text and "state\nfile" not in caplog.text
    assert not out.exists() and rt.links.handled_slot() == SLOT


async def test_one_attempt_at_a_time(links, monkeypatch):
    make, _ = links
    rt = logged_out(make(Clock(SLOT)))
    running, most = 0, 0
    other_started = asyncio.Event()  # the second attempt waits for the lock or, were there none, runs alongside

    class WatchedLock(asyncio.Lock):
        async def acquire(self):
            if self.locked():
                other_started.set()
            return await super().acquire()

    async def run(*_, **__):
        nonlocal running, most
        running += 1
        most = max(most, running)
        if running > 1:
            other_started.set()
        await other_started.wait()  # no run ends before the other attempt has started: no timing involved
        running -= 1
        return None

    monkeypatch.setattr(rt.links, "_lock", WatchedLock())
    monkeypatch.setattr(reminders, "run_link_command", run)
    attempts = await asyncio.wait_for(asyncio.gather(rt.links.send("requested"), rt.links.send("requested")), 10)
    assert other_started.is_set() and most == 1
    assert [a.outcome for a in attempts] == ["sent", "sent"] and issued(rt) == ["tool", "tool"]


def _cannot_write(path, state):
    raise PermissionError(13, "Permission denied")


async def test_a_scheduled_attempt_needs_its_slot(links):
    make, out = links
    rt = logged_out(make(Clock(SLOT)))
    with pytest.raises(ValueError):
        await rt.links.send("scheduled")  # every scheduled attempt records its slot
    assert not out.exists() and issued(rt) == [] and not (rt.settings.state_dir / STATE_FILE).exists()


async def test_a_naive_slot_is_taken_as_ist(links):
    make, _ = links
    clock = Clock(SLOT)
    rt = logged_out(make(clock))
    await rt.links.send("scheduled", slot=SLOT)
    clock.now = at(2, 8, 30)
    attempt = await rt.links.send("scheduled", slot=at(2, 8, 30).replace(tzinfo=None))
    assert attempt.outcome == "sent" and rt.links.handled_slot() == at(2, 8, 30)
    data = json.loads((rt.settings.state_dir / STATE_FILE).read_text())
    assert data["last_scheduled_for"] == "2026-11-02T08:30:00+05:30"


async def test_a_later_attempt_keeps_the_time_of_the_last_token_rejected_link(links):
    make, _ = links
    clock = Clock(at(2, 7, 0))
    rt = logged_out(make(clock))
    await rt.links.send("token_rejected")
    clock.now = SLOT
    await rt.links.send("scheduled", slot=SLOT)
    clock.now = at(2, 8, 0)
    await rt.links.send("requested")
    data = json.loads((rt.settings.state_dir / STATE_FILE).read_text())
    assert (data["last"]["reason"], data["token_rejected_link_at"], data["last_scheduled_for"]) == (
        "requested", "2026-11-02T07:00:00+05:30", "2026-11-02T07:30:00+05:30",
    )  # fmt: skip
    assert rt.links.token_rejected_link_at() == at(2, 7, 0)
    assert make(clock).links.token_rejected_link_at() == at(2, 7, 0)  # a new process knows it too


async def test_what_memory_held_while_the_file_could_not_be_written_reaches_it_later(links, monkeypatch):
    make, _ = links
    clock = Clock(SLOT)
    rt = logged_out(make(clock))
    path = rt.settings.state_dir / STATE_FILE
    with monkeypatch.context() as m:
        m.setattr(reminders, "write_state", _cannot_write)
        await rt.links.send("scheduled", slot=SLOT)
        clock.now = at(2, 7, 45)
        await rt.links.send("token_rejected")
        assert not path.exists()
        # held in memory: the slot never runs twice, and the two hours start at 07:45
        assert (rt.links.handled_slot(), rt.links.token_rejected_link_at()) == (SLOT, at(2, 7, 45))
    clock.now = at(2, 8, 0)
    await rt.links.send("requested")  # the file can be written again
    data = json.loads(path.read_text())
    assert (data["last"]["reason"], data["token_rejected_link_at"], data["last_scheduled_for"]) == (
        "requested", "2026-11-02T07:45:00+05:30", "2026-11-02T07:30:00+05:30",
    )  # fmt: skip
    restarted = make(clock).links
    assert (restarted.handled_slot(), restarted.token_rejected_link_at()) == (SLOT, at(2, 7, 45))


async def test_a_slot_in_the_future_is_ignored_with_one_warning_and_replaced(links, caplog):
    make, _ = links
    clock = Clock(SLOT)
    rt = logged_out(make(clock))
    path = rt.settings.state_dir / STATE_FILE
    ahead = {"last": None, "token_rejected_link_at": "2026-11-09T07:00:00+05:30"}
    path.write_text(json.dumps({**ahead, "last_scheduled_for": "2026-11-09T07:30:00+05:30"}))  # a clock a week ahead
    with caplog.at_level(logging.WARNING):
        # kept, they would hold back every slot, and the two hours, until then
        assert rt.links.handled_slot() is None and rt.links.handled_slot() is None
        assert rt.links.token_rejected_link_at() is None
    assert caplog.text.count("the login links' state file holds a slot in the future; ignoring it") == 1
    await rt.links.send("scheduled", slot=SLOT)
    data = json.loads(path.read_text())
    assert (data["last_scheduled_for"], data["token_rejected_link_at"]) == ("2026-11-02T07:30:00+05:30", None)
    assert make(clock).links.handled_slot() == SLOT  # a restart knows the real slot


def test_an_error_read_back_from_the_state_file_is_clean_and_short(links):
    make, _ = links
    rt = make(Clock(SLOT))
    last = {"at": "2026-11-02T07:30:00+05:30", "reason": "scheduled", "outcome": "failed"}
    path = rt.settings.state_dir / STATE_FILE
    path.write_text(json.dumps({"last": {**last, "error": "line1\nline2\x1b[31m " + "word " * 1000}}))
    error = rt.links.status()["last"]["error"]
    assert error.startswith("line1 line2 word word") and len(error) <= 200 and "\x1b" not in error
    path.write_text(json.dumps({"last": {**last, "error": "\x1b[31m\n"}}))
    assert rt.links.status()["last"]["error"] is None  # nothing left of it


# --- the schedule, the links after a rejected token, the lifespan (Task 9) ------------------------------------------

COMMAND = Path("/usr/local/bin/send-link")  # settings only: never run


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (at(2, 7, 29), None),  # Monday before the first time; Friday's last one is days old
        (at(2, 7, 30), at(2, 7, 30)),
        (at(2, 8, 29), at(2, 7, 30)),
        (at(2, 10, 0), at(2, 8, 30)),  # the latest one, not 07:30
        (at(2, 11, 30), at(2, 8, 30)),  # exactly 3 hours: still
        (at(2, 11, 31), None),  # more than 3 hours: dropped, and 07:30 is older still
        (at(7, 8, 0), None),  # Saturday: not a reminder day, and Friday's 08:30 is a day old
    ],
)
def test_the_latest_slot(settings_factory, now, expected):
    s = settings_factory(**links_env(COMMAND, **TIMES))
    assert latest_slot(s, now, SkipDates(None)) == expected


def test_an_evening_slot_is_caught_up_after_midnight_and_stays_its_days(settings_factory):
    s = settings_factory(**links_env(COMMAND, KITE_MCP_LOGIN_REMINDER_TIMES="23:30"))
    assert latest_slot(s, at(3, 1, 0), SkipDates(None)) == at(2, 23, 30)
    assert latest_slot(s, at(7, 1, 0), SkipDates(None)) == at(6, 23, 30)  # Friday's slot, caught up early on Saturday


def test_no_latest_slot_without_times(settings_factory):
    s = settings_factory(**links_env(COMMAND))  # a link command for requested links only
    assert latest_slot(s, at(2, 7, 30), SkipDates(None)) is None


async def test_a_slot_is_sent_at_its_time_and_once(links):
    make, out = links
    clock = Clock(at(2, 7, 29))  # Monday
    rt = logged_out(make(clock))
    await rt.links.tick()
    assert issued(rt) == []
    clock.now = at(2, 7, 30)
    await rt.links.tick()
    assert json.loads(out.read_text())["reason"] == "scheduled"
    clock.now = at(2, 7, 31)
    await rt.links.tick()
    assert issued(rt) == ["scheduled"]
    status = rt.links.status()
    assert status["last_scheduled_for"] == "2026-11-02T07:30+05:30" and status["next_at"] == "2026-11-02T08:30+05:30"


async def test_with_a_valid_token_the_slot_is_skipped_and_never_caught_up_later(links):
    make, out = links
    clock = Clock(at(2, 7, 30))
    rt = make(clock)
    write_token_file(rt.settings.token_file, logged_in_at=at(2, 7, 0))
    await rt.links.tick()
    assert not out.exists() and rt.links.status()["last"]["outcome"] == "skipped"
    rt.settings.token_file.unlink()  # the token goes later that morning
    clock.now = at(2, 7, 45)
    await rt.links.tick()
    assert issued(rt) == []  # the 07:30 slot had its attempt


async def test_a_slot_missed_while_down_is_caught_up_once_within_three_hours(links, caplog):
    make, out = links
    clock = Clock(at(2, 7, 50))  # the server comes up 20 minutes after the 07:30 slot
    rt = logged_out(make(clock))
    with caplog.at_level(logging.INFO):
        await rt.links.tick()
    assert json.loads(out.read_text())["reason"] == "scheduled" and "late" in caplog.text
    await rt.links.tick()
    restarted = make(clock)  # a restart: the state file says 07:30 was handled
    await restarted.links.tick()
    assert issued(rt) == ["scheduled"]  # the same audit log: the same state folder


@pytest.mark.parametrize(("now", "sent"), [(at(2, 11, 30), True), (at(2, 11, 31), False)])
async def test_a_slot_is_caught_up_up_to_three_hours_late_and_dropped_after(links, now, sent):
    make, out = links
    rt = logged_out(make(Clock(now)))
    await rt.links.tick()
    if sent:
        assert issued(rt) == ["scheduled"] and rt.links.status()["last_scheduled_for"] == "2026-11-02T08:30+05:30"
    else:
        assert not out.exists() and rt.links.status()["next_at"] == "2026-11-03T07:30+05:30"


async def test_only_the_latest_missed_slot_is_sent(links):
    make, _ = links
    rt = logged_out(make(Clock(at(2, 9, 0))))  # down through 07:30 and 08:30
    await rt.links.tick()
    await rt.links.tick()
    assert issued(rt) == ["scheduled"] and rt.links.status()["last_scheduled_for"] == "2026-11-02T08:30+05:30"


@pytest.mark.parametrize("holiday", [True, False])
async def test_no_slot_on_a_skip_date_or_a_day_that_is_not_allowed(links, tmp_path, holiday):
    make, out = links
    skip = tmp_path / "skip.txt"
    skip.write_text("2026-11-02\n")
    if holiday:  # Monday 2 November is in the skip-dates file
        rt = logged_out(make(Clock(at(2, 7, 45)), KITE_MCP_LOGIN_REMINDER_SKIP_DATES_FILE=str(skip)))
        next_at = "2026-11-03T07:30+05:30"
    else:  # Saturday is not in Mon-Fri
        rt = logged_out(make(Clock(at(7, 7, 45))))
        next_at = "2026-11-09T07:30+05:30"
    await rt.links.tick()
    assert not out.exists() and rt.links.status()["next_at"] == next_at
    assert rt.links.status()["last_scheduled_for"] is None


async def test_requested_and_token_rejected_links_leave_the_slot_to_run(links):
    make, _ = links
    clock = Clock(at(2, 7, 20))
    rt = logged_out(make(clock))
    await rt.links.send("requested")
    await rt.links.send("token_rejected")
    clock.now = at(2, 7, 30)
    await rt.links.tick()
    assert issued(rt) == ["tool", "token_rejected", "scheduled"]


async def test_a_rejected_token_brings_one_link_every_two_hours_at_most(links):
    make, out = links
    clock = Clock(at(2, 12, 0))  # more than 3 hours after 08:30: no slot to catch up
    rt = make(clock, **EVENTS)
    write_token_file(rt.settings.token_file, logged_in_at=at(2, 7, 35))
    rt.tokens.reject(ACCESS_TOKEN)  # the client's mark when Kite refuses the token
    rt.events.note_token_rejected(ACCESS_TOKEN)  # the event; its listener wakes the links
    await rt.links.tick()
    assert json.loads(out.read_text())["reason"] == "token_rejected"
    out.unlink()
    clock.now = at(2, 13, 0)
    rt.links.token_rejected()
    await rt.links.tick()
    assert not out.exists()
    restarted = make(clock, **EVENTS)  # a restart: the token is still marked (the store), and so is the last link
    restarted.links.token_rejected()
    await restarted.links.tick()
    assert not out.exists()
    clock.now = at(2, 14, 1)
    rt.links.token_rejected()
    await rt.links.tick()
    assert json.loads(out.read_text())["reason"] == "token_rejected"


async def test_no_link_for_a_rejected_token_once_a_new_login_came(links):
    make, out = links
    clock = Clock(at(2, 12, 0))
    rt = make(clock)
    write_token_file(rt.settings.token_file, logged_in_at=at(2, 11, 0))  # the newest token is valid
    rt.links.token_rejected()
    await rt.links.tick()
    assert not out.exists()


async def test_a_failed_link_for_a_rejected_token_counts_toward_the_two_hours(links, tmp_path):
    make, out = links
    clock = Clock(at(2, 12, 0))
    rt = logged_out(make(clock, command=link_command(tmp_path, "fails", exit_code=3)))
    rt.links.token_rejected()
    await rt.links.tick()
    assert rt.links.status()["last"]["outcome"] == "failed" and rt.links.token_rejected_link_at() == at(2, 12, 0)
    clock.now = at(2, 13, 0)
    rt.links.token_rejected()
    await rt.links.tick()
    assert issued(rt) == ["token_rejected"]  # one link only: no retry within two hours


async def test_a_rejection_waits_for_one_tick_only(links):
    make, _ = links
    clock = Clock(at(2, 12, 0))
    rt = logged_out(make(clock))
    rt.links.token_rejected()
    await rt.links.tick()
    clock.now = at(2, 14, 30)  # past the two hours, with no new rejection
    await rt.links.tick()
    assert issued(rt) == ["token_rejected"]


async def test_a_state_file_that_cannot_be_written_still_sends_a_slot_once(links, monkeypatch):
    make, _ = links
    clock = Clock(SLOT)
    rt = logged_out(make(clock))
    monkeypatch.setattr(reminders, "write_state", _cannot_write)
    await rt.links.tick()
    clock.now = at(2, 7, 31)
    await rt.links.tick()
    assert issued(rt) == ["scheduled"] and not (rt.settings.state_dir / STATE_FILE).exists()


async def test_a_link_refused_because_a_login_came_in_between_skips_the_slot(links, monkeypatch):
    make, out = links
    rt = logged_out(make(Clock(SLOT), KITE_MCP_LOGIN_ALLOW_FORCE="false"))
    real = rt.login.issue_link

    async def login_comes_first(**kwargs):
        write_token_file(rt.settings.token_file, logged_in_at=at(2, 7, 29))  # a login between the check and the link
        return await real(**kwargs)

    monkeypatch.setattr(rt.login, "issue_link", login_comes_first)
    await rt.links.tick()
    assert not out.exists() and issued(rt) == []
    assert rt.links.status()["last"]["outcome"] == "skipped" and rt.links.handled_slot() == SLOT
    assert [e["source"] for e in rt.audit.read() if e["event"] == "login_link_refused"] == ["scheduled"]


@pytest.mark.parametrize(
    ("raised", "error"),
    [
        (ConfigError("no link: KITE_API_SECRET\nis not set"), "no link: KITE_API_SECRET is not set"),
        (RuntimeError("a bug"), "an unexpected error (RuntimeError)"),
    ],
)
async def test_a_link_the_loop_cannot_issue_fails_its_slot_and_the_loop_goes_on(links, monkeypatch, raised, error):
    make, out = links
    clock = Clock(SLOT)
    rt = logged_out(make(clock))

    async def refused(**_):
        raise raised

    with monkeypatch.context() as m:
        m.setattr(rt.login, "issue_link", refused)
        await rt.links.tick()
    last = rt.links.status()["last"]
    assert (last["outcome"], last["error"]) == ("failed", error) and rt.links.handled_slot() == SLOT
    clock.now = at(2, 8, 30)
    await rt.links.tick()  # the next slot runs as usual
    assert json.loads(out.read_text())["reason"] == "scheduled"


async def test_stopping_while_the_command_runs_kills_it_and_records_nothing(links, tmp_path, monkeypatch):
    """The server stops while the command sends: it is killed and the attempt is not recorded, on purpose (the killed
    command likely delivered nothing), so a restart within three hours sends that slot again."""
    make, _ = links
    pid_out = tmp_path / "pids"
    monkeypatch.setenv("PID_OUT", str(pid_out))
    command = script(tmp_path, "hangs", 'cat > "$LINK_OUT"\n' + BACKGROUND)
    clock = Clock(SLOT)
    env = {**HTTP, "KITE_MCP_LOGIN_LINK_ENV_PASS": "LINK_OUT,PID_OUT"}
    rt = logged_out(make(clock, command=command, **env))
    monkeypatch.setattr(reminders, "STOP_TIMEOUT_S", 0)  # no grace: the command would run for 30 s
    await rt.links.start()
    await until(lambda: bool(pids(pid_out)), "the command did not start")
    await rt.links.stop()
    shell, child = pids(pid_out)
    await until(lambda: ended(shell) and ended(child), "the command or its child runs on after stop()")
    assert not rt.links.running and issued(rt) == ["scheduled"]  # issued, and handed to a command that was killed
    assert rt.links.status()["last"] is None and rt.links.handled_slot() is None
    restarted = make(clock)  # a restart within three hours
    await restarted.links.tick()
    assert restarted.links.status()["last"]["outcome"] == "sent" and issued(rt) == ["scheduled", "scheduled"]


async def test_stop_before_start_and_twice_is_harmless(links):
    make, _ = links
    rt = make(Clock(at(2, 5, 0)), **HTTP)
    await rt.links.stop()
    assert not rt.links.running
    await rt.links.start()
    await rt.links.start()  # a second start keeps the one loop
    assert rt.links.running
    assert len([t for t in asyncio.all_tasks() if t.get_name() == "kite-login-links"]) == 1
    await rt.links.stop()
    await rt.links.stop()
    assert not rt.links.running


async def test_a_wake_up_runs_a_tick_at_once(links):
    make, out = links
    rt = logged_out(make(Clock(at(2, 12, 0)), **HTTP))  # no slot due: the loop then waits up to TICK_S
    await rt.links.start()
    try:
        await asyncio.sleep(0)
        rt.links.token_rejected()
        await until(lambda: rt.links.status()["last"] is not None, "the wake-up did not run a tick")  # after the run
        assert rt.links.status()["last"]["outcome"] == "sent"
        assert json.loads(out.read_text())["reason"] == "token_rejected"
    finally:
        await rt.links.stop()


async def test_aclose_stops_the_links_before_the_events(links, fake_kite, monkeypatch):
    fake_kite.on("GET", "/orders", [])
    make, _ = links
    rt = make(Clock(at(2, 5, 0)), **EVENTS)
    await rt.events.start()
    await rt.links.start()
    order = []
    for name, part in (("links", rt.links), ("events", rt.events)):
        real = part.stop

        async def stop(real=real, name=name):
            order.append(name)
            await real()

        monkeypatch.setattr(part, "stop", stop)
    await rt.aclose()
    assert order == ["links", "events"] and not rt.links.running and not rt.events.running


def _lifespan(rt) -> LifespanOn:
    server, _ = build_server(rt)
    return LifespanOn(uvicorn.Config(build_http_app(server, rt), lifespan="on", log_config=None))


async def test_the_http_lifespan_starts_the_events_then_the_links_and_stops_both(links, fake_kite, monkeypatch):
    fake_kite.on("GET", "/orders", [])
    make, _ = links
    rt = make(Clock(at(2, 5, 0)), **EVENTS)
    order = []
    for name, part in (("events", rt.events), ("links", rt.links)):
        real = part.start

        async def start(real=real, name=name):
            order.append(name)
            await real()

        monkeypatch.setattr(part, "start", start)
    lifespan = _lifespan(rt)
    await lifespan.startup()
    assert not lifespan.should_exit and order == ["events", "links"] and rt.links.running and rt.events.running
    await lifespan.shutdown()
    assert not rt.links.running and not rt.events.running
    assert [t for t in asyncio.all_tasks() if t.get_name() == "kite-login-links" and not t.done()] == []


async def test_the_http_lifespan_starts_the_links_without_the_events(links):
    make, _ = links
    rt = make(Clock(at(2, 5, 0)), **HTTP)
    lifespan = _lifespan(rt)
    await lifespan.startup()
    assert not lifespan.should_exit and rt.events is None and rt.links.running
    await lifespan.shutdown()
    assert not rt.links.running


async def test_a_part_that_fails_to_start_stops_the_parts_started_before_it(links, fake_kite):
    fake_kite.on("GET", "/orders", [])
    make, _ = links
    rt = make(Clock(at(2, 5, 0)), **EVENTS)
    rt.links.command.chmod(0o777)  # the links refuse to start, after the events have started
    lifespan = _lifespan(rt)
    await lifespan.startup()
    assert lifespan.should_exit and lifespan.startup_failed
    assert not rt.events.running and not rt.links.running
    assert [t for t in asyncio.all_tasks() if t.get_name().startswith("kite-") and not t.done()] == []


async def test_an_untrusted_command_stops_the_links_from_starting(links):
    make, _ = links
    rt = make(Clock(at(2, 5, 0)), **HTTP)
    rt.links.command.chmod(0o777)
    with pytest.raises(ConfigError, match="KITE_MCP_LOGIN_LINK_COMMAND"):
        await rt.links.start()


async def test_the_links_do_not_start_on_the_stdio_transport(links):
    make, _ = links
    rt = make(Clock(at(2, 5, 0)))  # stdio, the default
    with pytest.raises(ConfigError, match="HTTP transport"):
        await rt.links.start()
    assert not rt.links.running


def test_serve_refuses_the_stdio_transport_with_links(cli_env, monkeypatch, tmp_path, capsys):
    import anyio

    def served(*args, **kwargs):
        raise AssertionError("the stdio server ran: the check must come first")

    monkeypatch.setattr(anyio, "run", served)
    for key, value in links_env(link_command(tmp_path), **TIMES).items():
        monkeypatch.setenv(key, value)
    assert main(["serve"]) == 2
    assert "HTTP transport" in capsys.readouterr().err
