"""The runtime of one server process: how it closes what it holds, and how the tests close the ones they build."""

from __future__ import annotations

import logging

import pytest

from conftest import close_runtimes


async def test_every_closer_runs_newest_first_and_a_failing_one_hides_nothing(runtime_factory, caplog, monkeypatch):
    rt = runtime_factory()
    closed: list[str] = []

    def failing() -> None:
        closed.append("failing")
        raise OSError("cannot close")

    rt.closers.extend([lambda: closed.append("first"), failing, lambda: closed.append("last")])
    try:
        with caplog.at_level(logging.ERROR):
            await rt.aclose()
        assert closed == ["last", "failing", "first"]
        assert "cannot close" in caplog.text

        async def broken() -> None:
            raise RuntimeError("the client did not close")

        monkeypatch.setattr(rt.client, "aclose", broken)
        closed.clear()
        with pytest.raises(RuntimeError, match="the client did not close"):  # not the closer's OSError
            await rt.aclose()
        assert closed == ["last", "failing", "first"]
    finally:
        rt.closers.clear()  # the fixture's teardown would run them again


def test_the_tests_close_their_runtimes_like_aclose_and_say_what_failed(runtime_factory):
    """runtime_factory's teardown: every closer of every runtime, newest first, each on its own; then an error."""
    first, second = runtime_factory(), runtime_factory()
    closed: list[str] = []

    def failing(name: str):
        def close() -> None:
            closed.append(name)
            raise OSError(f"{name} cannot close")

        return close

    first.closers.extend([lambda: closed.append("a1"), failing("a2"), lambda: closed.append("a3")])
    second.closers.extend([failing("b1"), lambda: closed.append("b2")])
    try:
        with pytest.raises(OSError, match="a2 cannot close"):
            close_runtimes([first, second])
        assert closed == ["a3", "a2", "a1", "b2", "b1"]
    finally:
        first.closers.clear()
        second.closers.clear()
