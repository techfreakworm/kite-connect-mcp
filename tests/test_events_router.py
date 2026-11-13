"""The router runs consumer commands: the exit-code contract, timeouts, retries, alerts, ordering, pause, restarts."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os

import pytest

from events_helpers import PY, Clock, filled_row, folders, make_service, order_row
from kite_connect_mcp.events.router import Router


def consumer(name, code, tmp_path, **extra):
    """A consumer that runs `code` (Python) with the event on stdin; `OUT` is a file it may append to."""
    out = tmp_path / f"{name}.out"
    script = f"import json, os, sys, time\nOUT = {str(out)!r}\nevent = json.loads(sys.stdin.read())\n{code}\n"
    return {"name": name, "command": [PY, "-c", script], **extra}, out


RECORD = "open(OUT, 'a').write(json.dumps({'seq': event['seq'], 'env': dict(os.environ)}) + '\\n')"


def lines(path):
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


@pytest.fixture
def build(runtime_factory, tmp_path, fake_kite):
    made = []

    def make(*consumers, clock=None, environ=None):
        clock = clock or Clock()
        svc, rt = make_service(runtime_factory, tmp_path, consumers=list(consumers), clock=clock)
        router = Router(svc, environ=environ)
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
    await asyncio.sleep(0.2)
    assert not out.exists()  # killed before it could write


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
        svc.store.resume("notify", skip_backlog=True, now=clock())  # the operator skips the backlog meanwhile
        return await real_run(c, body)

    router.run_command = skip_while_running
    assert await router.step(router.config.get("notify")) is None
    assert svc.store.summary()["consumers"]["notify"]["skipped"] == 1
    assert [e for e in svc.store.events() if e["kind"] == "router"] == []  # no delivery_failed for a skipped event


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
        if out.exists():
            break
        await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(task, 5)
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
