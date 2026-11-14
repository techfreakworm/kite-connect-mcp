"""The router runs consumer commands: the exit-code contract, timeouts, retries, alerts, ordering, pause, restarts."""

from __future__ import annotations

import asyncio
import datetime as dt
import errno
import json
import logging
import os
import sqlite3

import pytest

from events_helpers import PY, Clock, filled_row, folders, make_service, order_row
from kite_connect_mcp.events.model import MAX_ERROR_CHARS
from kite_connect_mcp.events.router import STDERR_KEEP, Router, _error, _read_tail, _stderr_text
from kite_connect_mcp.events.store import StoreError


def consumer(name, code, tmp_path, **extra):
    """A consumer that runs `code` (Python) with the event on stdin; `OUT` is a file it may append to."""
    out = tmp_path / f"{name}.out"
    script = f"import json, os, sys, time\nOUT = {str(out)!r}\nevent = json.loads(sys.stdin.read())\n{code}\n"
    return {"name": name, "command": [PY, "-c", script], **extra}, out


RECORD = "open(OUT, 'a').write(json.dumps({'seq': event['seq'], 'env': dict(os.environ)}) + '\\n')"
HOOK_PART = "placeholderhookpart0001"  # the private part of a webhook link


def beating(path, beats=100):
    """Python code that appends a byte to `path` every 50 ms, `beats` times: alive while the file grows."""
    return f"for _ in range({beats}):\n    open({str(path)!r}, 'a').write('x')\n    time.sleep(0.05)\n"


async def assert_stopped(path):
    """The process beating on `path` (see beating()) has stopped: the file no longer grows."""
    await asyncio.sleep(0.1)
    size = path.stat().st_size
    await asyncio.sleep(0.3)
    assert path.stat().st_size == size, "the process still runs"


def last_error(svc, clock, name="notify"):
    """The last error of `name`'s first pending delivery (one that is retrying is due later)."""
    delivery, _ = svc.store.next_delivery(name, clock.now + dt.timedelta(days=1))
    return delivery.last_error


def router_events(svc):
    return [e for e in svc.store.events() if e["kind"] == "router"]


def lines(path):
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


@pytest.fixture
def build(runtime_factory, tmp_path, fake_kite):
    made = []

    def make(*consumers, clock=None, environ=None, spawn=None):
        clock = clock or Clock()
        svc, rt = make_service(runtime_factory, tmp_path, consumers=list(consumers), clock=clock)
        router = Router(svc, environ=environ, spawn=spawn)
        svc.router = router
        made.append(svc)
        return svc, router, clock

    yield make
    for svc in made:
        svc.store.close()
    assert fake_kite.writes() == []  # the router never talks to Kite


async def test_exit_0_is_done_and_the_event_arrives_on_stdin(build, tmp_path):
    spec, out = consumer("notify", RECORD + "\nsys.exit(0)", tmp_path)
    svc, router, _ = build(spec, environ={"PATH": "/usr/bin", "HOME": "/h", "SECRET_X": "s", "KITE_API_SECRET": "k"})
    seq = svc.ingest_order(order_row(), source="poll")
    assert await router.step(router.config.get("notify")) is None  # it delivered something
    (got,) = lines(out)
    assert got["seq"] == seq
    env = got["env"]
    assert env["KITE_EVENT_CONSUMER"] == "notify" and env["HOME"] == "/h"
    assert "KITE_API_SECRET" not in env and "SECRET_X" not in env
    assert svc.store.summary()["consumers"]["notify"]["done"] == 1


async def test_env_pass_passes_named_variables_only(build, tmp_path):
    spec, out = consumer("notify", RECORD, tmp_path, env_pass=["SECRET_X"])
    svc, router, _ = build(spec, environ={"PATH": "/usr/bin", "SECRET_X": "s"})
    svc.ingest_order(order_row(), source="poll")
    await router.step(router.config.get("notify"))
    assert lines(out)[0]["env"]["SECRET_X"] == "s"


async def test_exit_75_retries_on_the_schedule_then_fails(build, tmp_path):
    spec, _ = consumer(
        "notify", "sys.exit(75)", tmp_path, retry={"delays_s": [10, 30], "every_s": 60, "give_up_after_s": 100}
    )
    watcher, wout = consumer("watch", RECORD, tmp_path, match={"kinds": ["router"]})
    svc, router, clock = build(spec, watcher)
    svc.ingest_order(order_row(), source="poll")
    notify = router.config.get("notify")
    await router.step(notify)  # try 1 at t=0
    d, later = svc.store.next_delivery("notify", clock())
    assert d is None and later == clock.now + dt.timedelta(seconds=10)
    clock.advance(10)
    await router.step(notify)  # try 2 at t=10, next in 30
    clock.advance(30)
    await router.step(notify)  # try 3 at t=40, next in 60
    clock.advance(60)
    await router.step(notify)  # try 4 at t=100: 100 s since the first try, give up
    assert svc.store.summary()["consumers"]["notify"]["failed"] == 1
    kinds = [e.get("router") for e in svc.store.events() if e["kind"] == "router"]
    assert kinds == ["delivery_delayed", "delivery_failed"]  # the alert at 60 s, then the failure
    await router.step(router.config.get("watch"))
    await router.step(router.config.get("watch"))
    assert len(lines(wout)) == 2  # the watcher got both router events; notify never hears of its own failure


async def test_give_up_after_0_means_one_try_and_no_retries(build, tmp_path):
    spec, out = consumer("notify", RECORD + "\nsys.exit(75)", tmp_path, retry={"give_up_after_s": 0})
    watcher, _ = consumer("watch", "pass", tmp_path, match={"kinds": ["router"]})
    svc, router, _ = build(spec, watcher)
    svc.ingest_order(order_row(), source="poll")
    assert await router.step(router.config.get("notify")) is None
    summary = svc.store.summary()["consumers"]["notify"]
    assert summary["failed"] == 1 and summary["pending"] == 0 and summary["last_error"] == "exit 75"
    (failed,) = [e for e in svc.store.events() if e["kind"] == "router"]
    assert failed["router"] == "delivery_failed" and failed["consumer"] == "notify"
    assert len(lines(out)) == 1  # it ran once


async def test_step_waits_for_the_next_retry_or_alert(build, tmp_path):
    spec, _ = consumer("notify", "sys.exit(75)", tmp_path, alert_after_s=5, retry={"delays_s": [10]})
    svc, router, clock = build(spec)
    notify = router.config.get("notify")
    assert await router.step(notify) == Router.IDLE_S  # nothing stored: the idle tick
    svc.ingest_order(order_row(), source="poll")
    assert await router.step(notify) is None  # try 1 at t=0, next at t=10, alert at t=5
    assert await router.step(notify) == 5
    clock.advance(5)
    assert await router.step(notify) == 5  # the alert is sent; the retry is due at t=10
    assert [e["router"] for e in svc.store.events() if e["kind"] == "router"] == ["delivery_delayed"]


async def test_any_other_exit_fails_at_once_with_the_error(build, tmp_path):
    spec, _ = consumer("notify", "sys.stderr.write('bad input'); sys.exit(3)", tmp_path)
    watcher, _ = consumer("watch", "pass", tmp_path, match={"kinds": ["router"]})
    svc, router, _ = build(spec, watcher)
    svc.ingest_order(order_row(), source="poll")
    await router.step(router.config.get("notify"))
    failed = [e for e in svc.store.events() if e["kind"] == "router"]
    assert failed[0]["router"] == "delivery_failed" and failed[0]["error"] == "exit 3: bad input"
    assert svc.store.summary()["consumers"]["notify"]["last_error"] == "exit 3: bad input"


async def test_a_timeout_is_a_temporary_failure_and_the_process_is_killed(build, tmp_path):
    spec, out = consumer("notify", "time.sleep(30); " + RECORD, tmp_path, timeout_s=1)
    svc, router, clock = build(spec)
    svc.ingest_order(order_row(), source="poll")
    await asyncio.wait_for(router.step(router.config.get("notify")), 10)
    d, later = svc.store.next_delivery("notify", clock())
    assert d is None and later is not None
    summary = svc.store.summary()["consumers"]["notify"]
    assert summary["retrying"] == 1
    assert last_error(svc, clock) == "timed out after 1 s"
    await asyncio.sleep(0.2)
    assert not out.exists()  # killed before it could write


async def test_a_timeout_kills_what_the_command_started_too(build, tmp_path):
    beat = tmp_path / "beat"
    code = (
        "import subprocess\n"
        f"subprocess.Popen([sys.executable, '-c', 'import time\\n' + {beating(beat)!r}], stdin=subprocess.DEVNULL, "
        "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        f"while not os.path.exists({str(beat)!r}):\n    time.sleep(0.01)\n"  # the child runs: now wait to be killed
        "time.sleep(30)"
    )
    # two Python start-ups before the timeout: 3 s, so a slow machine still gets there (the wait above only says when)
    spec, _ = consumer("notify", code, tmp_path, timeout_s=3)
    svc, router, _ = build(spec)
    svc.ingest_order(order_row(), source="poll")
    await asyncio.wait_for(router.step(router.config.get("notify")), 10)
    assert beat.exists()  # the child it started ran ...
    await assert_stopped(beat)  # ... and was killed with it: the whole process group


async def test_a_background_child_that_keeps_stderr_open_times_out(build, tmp_path):
    code = "import subprocess\nsubprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\nsys.exit(0)"
    # the command must start its child and exit before the timeout, or the error loses its reason: 3 s, so a slow
    # machine still gets there
    spec, _ = consumer("notify", code, tmp_path, timeout_s=3)
    svc, router, clock = build(spec)
    svc.ingest_order(order_row(), source="poll")
    await asyncio.wait_for(router.step(router.config.get("notify")), 10)
    assert svc.store.summary()["consumers"]["notify"]["retrying"] == 1
    assert (
        last_error(svc, clock) == "timed out after 3 s: the command exited 0, but a process it started kept stderr open"
    )


async def test_a_timeout_after_the_command_died_of_a_signal_names_the_signal(build, tmp_path):
    code = (
        "import signal, subprocess\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        "os.kill(os.getpid(), signal.SIGTERM)"
    )
    spec, _ = consumer("notify", code, tmp_path, timeout_s=3)  # 3 s, as above: it must have died before the timeout
    svc, router, clock = build(spec)
    svc.ingest_order(order_row(), source="poll")
    await asyncio.wait_for(router.step(router.config.get("notify")), 10)
    assert last_error(svc, clock) == (
        "timed out after 3 s: the command was killed by SIGTERM, but a process it started kept stderr open"
    )


def test_a_clipped_stderr_starts_at_a_whole_word():
    room = MAX_ERROR_CHARS - len("exit 3") - 2
    cut_in_a_word = "start [link] " + "b" * (room - 3)  # the last `room` characters begin inside "[link]"
    assert _error("exit 3", cut_in_a_word) == "exit 3: " + "b" * (room - 3)
    at_a_word = "start " + "c" * room  # they begin at a word: it stays
    assert _error("exit 3", at_a_word) == "exit 3: " + "c" * room
    assert _error("exit 3", "short [link] end") == "exit 3: short [link] end"
    assert _error("exit 3", "z" * (room + 5)) == "exit 3"  # one long word, cut: nothing whole is left of it


def test_an_8_bit_csi_byte_does_not_hide_a_link_in_stderr():
    text = _stderr_text(b"\x9b31mhttps://hooks.example.com/T000/" + HOOK_PART.encode() + b"\x9b0m failed", cut=False)
    assert text == "[link] failed"


@pytest.mark.parametrize("name", ["SIGTERM", "SIGKILL"])
async def test_a_consumer_killed_by_a_signal_is_retried(build, tmp_path, name):
    # a service stop sends SIGTERM to every process of the unit, consumers too; the OOM killer sends SIGKILL
    spec, _ = consumer("notify", f"import signal\nos.kill(os.getpid(), signal.{name})", tmp_path)
    watcher, _ = consumer("watch", "pass", tmp_path, match={"kinds": ["router"]})
    svc, router, clock = build(spec, watcher)
    svc.ingest_order(order_row(), source="poll")
    assert await router.step(router.config.get("notify")) is None
    summary = svc.store.summary()["consumers"]["notify"]
    assert summary["retrying"] == 1 and summary["failed"] == 0
    assert last_error(svc, clock) == f"killed by {name}"
    assert router_events(svc) == []  # no delivery_failed


async def test_any_other_error_while_the_command_runs_kills_it(build, tmp_path):
    beat = tmp_path / "beat"
    spec, _ = consumer("notify", beating(beat), tmp_path)

    async def spawn(*args, **kwargs):
        proc = await asyncio.create_subprocess_exec(*args, **kwargs)

        async def broken_read(n=-1):
            for _ in range(300):
                if beat.exists():
                    break
                await asyncio.sleep(0.01)
            raise RuntimeError("reading stderr failed")

        proc.stderr.read = broken_read
        return proc

    svc, router, _ = build(spec, spawn=spawn)
    svc.ingest_order(order_row(), source="poll")
    with pytest.raises(RuntimeError):
        await asyncio.wait_for(router.step(router.config.get("notify")), 10)
    assert beat.exists()
    await assert_stopped(beat)
    summary = svc.store.summary()["consumers"]["notify"]
    assert summary["pending"] == 1 and summary["retrying"] == 0  # no try counted: it runs again


async def test_stderr_becomes_one_clean_line(build, tmp_path):
    message = (
        "\x1b[31mnotify:\x1b[0m posting to https://hooks.example.com/T000/" + HOOK_PART + "\x07\r\n"
        "failed:\t403 \u202eForbidden\n\n"
    )
    spec, _ = consumer("notify", f"sys.stderr.write({message!r}); sys.exit(3)", tmp_path)
    svc, router, _ = build(spec)
    svc.ingest_order(order_row(), source="poll")
    await router.step(router.config.get("notify"))
    assert (
        svc.store.summary()["consumers"]["notify"]["last_error"]
        == "exit 3: notify: posting to [link] failed: 403 Forbidden"
    )


async def test_a_long_stderr_keeps_its_end_and_never_part_of_a_link(build, tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    message = (
        "x " * 100 + "https://hooks.example.com/services/T000/B000/" + HOOK_PART + " failed: 403 Forbidden " + "z" * 150
    )
    spec, _ = consumer("notify", f"sys.stderr.write({message!r}); sys.exit(1)", tmp_path)
    watcher, _ = consumer("watch", "pass", tmp_path, match={"kinds": ["router"]})
    svc, router, _ = build(spec, watcher)
    svc.ingest_order(order_row(), source="poll")
    await router.step(router.config.get("notify"))
    error = svc.store.summary()["consumers"]["notify"]["last_error"]
    assert error.startswith("exit 1: ") and error.endswith(" [link] failed: 403 Forbidden " + "z" * 150)
    assert len(error) <= MAX_ERROR_CHARS
    (failed,) = router_events(svc)
    assert failed["error"] == error  # the same text in the store, the router event ...
    assert error in caplog.text  # ... and the log
    assert HOOK_PART not in json.dumps(failed) and HOOK_PART not in caplog.text


@pytest.mark.parametrize(
    ("write", "expected"),
    [
        ("sys.stderr.write('e' * 2000000 + ' the end')", "exit 3: the end"),  # a flood: only its end is kept
        # the kept end starts inside a link: the cut word is dropped, not shown without its scheme
        ("sys.stderr.write('https://hooks.example.com/' + 'S' * 5000 + ' failed')", "exit 3: failed"),
    ],
)
async def test_only_the_end_of_stderr_is_read(build, tmp_path, write, expected):
    spec, _ = consumer("notify", write + "\nsys.exit(3)", tmp_path)
    svc, router, _ = build(spec)
    svc.ingest_order(order_row(), source="poll")
    await router.step(router.config.get("notify"))
    assert svc.store.summary()["consumers"]["notify"]["last_error"] == expected


async def test_a_command_that_floods_stderr_until_its_timeout_is_killed_and_reaped_at_once(build, tmp_path):
    # the reader stops at the timeout; the rest is read after the kill, or a full pipe would hide its end for 5 s
    spec, _ = consumer("notify", "while True:\n    sys.stderr.write('e' * 65536)", tmp_path, timeout_s=1)
    svc, router, clock = build(spec)
    svc.ingest_order(order_row(), source="poll")
    await asyncio.wait_for(router.step(router.config.get("notify")), 3)
    assert last_error(svc, clock) == "timed out after 1 s"


async def test_the_stderr_reader_keeps_only_the_last_bytes():
    data = bytes(range(256)) * 4096  # 1 MiB
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    reader.feed_eof()
    assert await _read_tail(reader) == (data[-STDERR_KEEP:], True)
    short = asyncio.StreamReader()
    short.feed_data(b"short")
    short.feed_eof()
    assert await _read_tail(short) == (b"short", False)


async def test_a_program_that_is_gone_fails(build, tmp_path):
    (bin_dir,) = folders(tmp_path, "bin")
    script = bin_dir / "gone.sh"
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(0o755)
    svc, router, _ = build({"name": "notify", "command": [str(script)]})
    script.unlink()
    svc.ingest_order(order_row(), source="poll")
    await router.step(router.config.get("notify"))
    summary = svc.store.summary()["consumers"]["notify"]
    assert summary["failed"] == 1
    assert summary["last_error"].startswith("cannot read the program ") and "No such file" in summary["last_error"]


async def test_a_command_that_cannot_start_fails(build, tmp_path):
    (bin_dir,) = folders(tmp_path, "bin")
    script = bin_dir / "noexec.sh"
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(0o755)
    svc, router, _ = build({"name": "notify", "command": [str(script)]})
    script.chmod(0o644)  # still trusted (only this user can change it), but no longer executable
    svc.ingest_order(order_row(), source="poll")
    await router.step(router.config.get("notify"))
    summary = svc.store.summary()["consumers"]["notify"]
    assert summary["failed"] == 1
    assert summary["last_error"] == "cannot start the command: Permission denied"


@pytest.mark.parametrize("code", ["EAGAIN", "ENOMEM", "EMFILE", "ENFILE", "ETXTBSY"])
async def test_a_temporary_failure_to_start_is_retried(build, tmp_path, code):
    number = getattr(errno, code)

    async def spawn(*args, **kwargs):
        raise OSError(number, os.strerror(number))

    spec, _ = consumer("notify", "pass", tmp_path)
    svc, router, clock = build(spec, spawn=spawn)
    svc.ingest_order(order_row(), source="poll")
    await router.step(router.config.get("notify"))
    summary = svc.store.summary()["consumers"]["notify"]
    assert summary["retrying"] == 1 and summary["failed"] == 0
    assert last_error(svc, clock) == f"cannot start the command: {os.strerror(number)}"


async def test_a_program_others_can_change_since_loading_is_refused_and_never_run(build, tmp_path):
    (bin_dir,) = folders(tmp_path, "bin")
    ran = tmp_path / "ran"
    script = bin_dir / "notify.sh"
    script.write_text(f"#!/bin/sh\ntouch {ran}\n")
    script.chmod(0o755)
    svc, router, _ = build({"name": "notify", "command": [str(script)]})
    script.chmod(0o777)  # anyone could now put code in it
    svc.ingest_order(order_row(), source="poll")
    await router.step(router.config.get("notify"))
    summary = svc.store.summary()["consumers"]["notify"]
    assert summary["failed"] == 1 and "writable by group or others" in summary["last_error"]
    assert not ran.exists()


async def test_events_of_one_order_go_in_order_and_others_are_not_held(build, tmp_path):
    spec, out = consumer("notify", RECORD + "\nsys.exit(75 if event['order']['order_id'] == '1' else 0)", tmp_path)
    svc, router, clock = build(spec)
    a = svc.ingest_order(order_row(order_id="1"), source="poll")
    b = svc.ingest_order(order_row(order_id="1", price=1), source="poll")
    c = svc.ingest_order(order_row(order_id="2"), source="poll")
    notify = router.config.get("notify")
    for _ in range(3):
        await router.step(notify)
    assert [x["seq"] for x in lines(out)] == [a, c]  # b waits behind a, c is not held up
    assert b not in [x["seq"] for x in lines(out)]


async def test_pause_holds_deliveries_and_resume_delivers_the_backlog(build, tmp_path):
    spec, out = consumer("notify", RECORD, tmp_path)
    svc, router, clock = build(spec)
    svc.store.pause("notify", clock())
    svc.ingest_order(order_row(), source="poll")
    assert await router.step(router.config.get("notify")) is not None  # paused: nothing ran
    assert not out.exists()
    svc.store.resume("notify", skip_backlog=False, now=clock())
    await router.step(router.config.get("notify"))
    assert len(lines(out)) == 1


async def test_a_paused_consumer_raises_no_delayed_alert(build, tmp_path):
    """Its deliveries wait because you paused it: the other consumers are not told that they are late."""
    spec, _ = consumer("notify", "sys.exit(75)", tmp_path, alert_after_s=5, retry={"delays_s": [10]})
    watcher, _ = consumer("watch", "pass", tmp_path, match={"kinds": ["router"]})
    svc, router, clock = build(spec, watcher)
    notify = router.config.get("notify")
    svc.ingest_order(order_row(), source="poll")
    assert await router.step(notify) is None  # try 1 fails at t=0: the retry is due at t=10, the alert at t=5
    svc.store.pause("notify", clock())
    clock.advance(60)
    assert await router.step(notify) == Router.PAUSED_S
    assert router_events(svc) == []


async def test_resume_starts_the_retries_of_a_waiting_delivery_afresh(build, tmp_path):
    """A pause does not use up a delivery's give-up window: with the target fixed an hour later, a temporary failure on
    the first try after the resume is retried, not failed for good, and its alert counts from that try."""
    spec, out = consumer(
        "notify",
        RECORD + "\nsys.exit(75)",
        tmp_path,
        alert_after_s=60,
        retry={"delays_s": [10], "give_up_after_s": 100},
    )
    svc, router, clock = build(spec)
    notify = router.config.get("notify")
    svc.ingest_order(order_row(), source="poll")
    assert await router.step(notify) is None  # try 1 fails at t=0
    svc.store.pause("notify", clock())
    clock.advance(3600)
    svc.store.resume("notify", skip_backlog=False, now=clock())
    assert await router.step(notify) is None  # try 2 fails: retried
    summary = svc.store.summary()["consumers"]["notify"]
    assert summary["failed"] == 0 and summary["retrying"] == 1 and len(lines(out)) == 2
    assert router_events(svc) == []
    delivery, _ = svc.store.next_delivery("notify", clock.now + dt.timedelta(days=1))
    assert delivery.attempts == 1 and delivery.first_try_at == clock.now


async def test_a_restart_loses_nothing(build, tmp_path):
    spec, out = consumer("notify", RECORD, tmp_path)
    svc, _, _ = build(spec)
    svc.ingest_order(order_row(), source="poll")
    svc.ingest_order(filled_row(), source="poll")
    fresh = Router(svc)  # a new router over the same store, as after a restart
    notify = fresh.config.get("notify")
    await fresh.step(notify)
    await fresh.step(notify)
    assert len(lines(out)) == 2


async def test_a_delivery_skipped_while_it_runs_stays_skipped(build, tmp_path):
    spec, _ = consumer("notify", "sys.exit(1)", tmp_path)
    watcher, wout = consumer("watch", RECORD, tmp_path, match={"kinds": ["router"]})
    svc, router, clock = build(spec, watcher)
    svc.ingest_order(order_row(), source="poll")
    real_run = router.run_command

    async def skip_while_running(c, body):
        svc.store.resume("notify", skip_backlog=True, now=clock())  # you skip the backlog meanwhile
        return await real_run(c, body)

    router.run_command = skip_while_running
    assert await router.step(router.config.get("notify")) is None
    assert svc.store.summary()["consumers"]["notify"]["skipped"] == 1
    assert [e for e in svc.store.events() if e["kind"] == "router"] == []  # no delivery_failed for a skipped event


def fail_router_events(svc, monkeypatch):
    """Make the store fail to write a router event (as a full disk would), and nothing else."""
    real = svc.store.insert_event

    def insert_event(con, body, **kwargs):
        if body["kind"] == "router":
            raise sqlite3.OperationalError("disk I/O error")
        return real(con, body, **kwargs)

    monkeypatch.setattr(svc.store, "insert_event", insert_event)


async def test_a_failure_is_kept_only_with_its_delivery_failed_event(build, tmp_path, monkeypatch):
    spec, out = consumer("notify", RECORD + "\nsys.exit(1)", tmp_path)
    watcher, _ = consumer("watch", "pass", tmp_path, match={"kinds": ["router"]})
    svc, router, _ = build(spec, watcher)
    svc.ingest_order(order_row(), source="poll")
    notify = router.config.get("notify")
    with monkeypatch.context() as patch:
        fail_router_events(svc, patch)
        with pytest.raises(StoreError):
            await router.step(notify)
    summary = svc.store.summary()["consumers"]["notify"]
    assert (summary["pending"], summary["failed"]) == (1, 0) and router_events(svc) == []  # neither was kept
    assert await router.step(notify) is None  # so it runs again (deliveries are at least once) ...
    assert len(lines(out)) == 2
    summary = svc.store.summary()["consumers"]["notify"]
    assert (summary["pending"], summary["failed"]) == (0, 1)
    assert [e["router"] for e in router_events(svc)] == ["delivery_failed"]  # ... and both are kept


async def test_a_delayed_alert_is_marked_sent_only_with_its_event(build, tmp_path, monkeypatch):
    spec, _ = consumer("notify", "sys.exit(75)", tmp_path, alert_after_s=5, retry={"delays_s": [10]})
    watcher, _ = consumer("watch", "pass", tmp_path, match={"kinds": ["router"]})
    svc, router, clock = build(spec, watcher)
    svc.ingest_order(order_row(), source="poll")
    notify = router.config.get("notify")
    await router.step(notify)  # try 1 at t=0, alert due at t=5
    clock.advance(5)
    with monkeypatch.context() as patch:
        fail_router_events(svc, patch)
        with pytest.raises(StoreError):
            await router.step(notify)
    assert svc.store.summary()["consumers"]["notify"]["delayed"] == 0 and router_events(svc) == []
    await router.step(notify)  # the alert is still due
    assert svc.store.summary()["consumers"]["notify"]["delayed"] == 1
    assert [e["router"] for e in router_events(svc)] == ["delivery_delayed"]


async def test_the_log_names_the_program_never_its_arguments(build, tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    marker = "argv-marker-0001"
    spec, _ = consumer("notify", f"'{marker}'\nsys.exit(75 if event['order']['status'] == 'OPEN' else 3)", tmp_path)
    svc, router, _ = build(spec)
    svc.ingest_order(order_row(), source="poll")
    svc.ingest_order(filled_row(order_id="250101000000002"), source="poll")
    notify = router.config.get("notify")
    await router.step(notify)  # a retry
    await router.step(notify)  # a failure
    summary = svc.store.summary()["consumers"]["notify"]
    assert summary["retrying"] == 1 and summary["failed"] == 1
    assert caplog.records and marker not in caplog.text


async def test_run_delivers_in_the_background_and_stops(build, tmp_path):
    spec, out = consumer("notify", RECORD, tmp_path)
    svc, router, _ = build(spec)
    stop = asyncio.Event()
    task = asyncio.create_task(router.run(stop))
    await asyncio.sleep(0)
    svc.ingest_order(order_row(), source="poll")  # tells the router through on_event
    for _ in range(300):
        if svc.store.summary()["consumers"]["notify"]["done"] == 1:
            break
        await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(task, 5)
    assert svc.store.summary()["consumers"]["notify"]["done"] == 1
    assert len(lines(out)) == 1
    assert os.environ.get("KITE_EVENT_CONSUMER") is None


async def test_stopping_kills_a_running_command_and_keeps_its_delivery(build, tmp_path):
    started = tmp_path / "started"
    code = f"open({str(started)!r}, 'w').write(str(os.getpid()))\ntime.sleep(30)\n" + RECORD
    spec, out = consumer("notify", code, tmp_path)
    svc, router, _ = build(spec)
    svc.ingest_order(order_row(), source="poll")
    stop = asyncio.Event()
    task = asyncio.create_task(router.run(stop))
    for _ in range(300):
        if started.exists() and started.read_text():
            break
        await asyncio.sleep(0.01)
    pid = int(started.read_text())
    stop.set()
    await asyncio.wait_for(task, 5)
    for _ in range(200):  # the process group was killed: the pid goes once it is reaped
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("the consumer still runs after the router stopped")
    summary = svc.store.summary()["consumers"]["notify"]
    assert summary["pending"] == 1 and summary["retrying"] == 0  # delivered again after a restart
    assert not out.exists()
