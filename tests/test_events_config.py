"""Settings of the order-events feature: off by default, and nothing changes for existing users."""

from __future__ import annotations

import datetime as dt

import pytest

from conftest import BEARER
from kite_connect_mcp.config import TOOLSETS, unknown_variables
from kite_connect_mcp.errors import ConfigError

SERVICE = {"KITE_MCP_EVENTS": "true", "KITE_MCP_TRANSPORT": "http", "KITE_MCP_BEARER_TOKEN": BEARER}


def test_off_by_default(settings_factory):
    s = settings_factory()
    assert s.events is False
    assert s.events_db is None
    assert s.events_consumers_file is None
    assert s.order_poll_interval_s == 60
    assert s.order_poll_window == (dt.time(9, 0), dt.time(16, 0))
    assert s.order_poll_days == frozenset({0, 1, 2, 3, 4})
    assert s.events_keep_days == 30


def test_events_is_a_toolset():
    assert "events" in TOOLSETS


def test_the_service_keeps_its_store_in_the_state_folder(settings_factory, tmp_path):
    s = settings_factory(**SERVICE)
    assert s.events is True
    assert s.events_db == tmp_path / "state" / "events.sqlite3"


def test_the_service_can_keep_its_store_elsewhere(settings_factory, tmp_path):
    s = settings_factory(**SERVICE, KITE_MCP_EVENTS_DB=str(tmp_path / "elsewhere.sqlite3"))
    assert s.events is True
    assert s.events_db == tmp_path / "elsewhere.sqlite3"


def test_another_process_names_the_store_it_reads(settings_factory, tmp_path):
    s = settings_factory(KITE_MCP_EVENTS_DB=str(tmp_path / "other.sqlite3"))
    assert s.events is False
    assert s.events_db == tmp_path / "other.sqlite3"


def test_the_consumers_file(settings_factory, tmp_path):
    s = settings_factory(**SERVICE, KITE_MCP_EVENTS_CONSUMERS_FILE=str(tmp_path / "consumers.json"))
    assert s.events_consumers_file == tmp_path / "consumers.json"


def test_the_event_service_needs_the_http_transport(settings_factory):
    with pytest.raises(ConfigError, match="KITE_MCP_EVENTS=true needs KITE_MCP_TRANSPORT=http"):
        settings_factory(KITE_MCP_EVENTS="true")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("09:15-15:30", (dt.time(9, 15), dt.time(15, 30))), (" 8:00 - 17:00 ", (dt.time(8, 0), dt.time(17, 0)))],
)
def test_the_poll_window(settings_factory, raw, expected):
    assert settings_factory(KITE_MCP_ORDER_POLL_WINDOW=raw).order_poll_window == expected


@pytest.mark.parametrize("raw", ["16:00-09:00", "09:00-09:00", "9-16", "09:00", "25:00-26:00"])
def test_a_bad_poll_window_is_refused(settings_factory, raw):
    with pytest.raises(ConfigError, match="KITE_MCP_ORDER_POLL_WINDOW"):
        settings_factory(KITE_MCP_ORDER_POLL_WINDOW=raw)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Mon-Fri", {0, 1, 2, 3, 4}),
        ("mon,wed,fri", {0, 2, 4}),
        ("Sat-Sun", {5, 6}),
        ("Mon-Sun", set(range(7))),
        ("*", set(range(7))),
        ("Tue", {1}),
        ("Mon - Fri", {0, 1, 2, 3, 4}),
        ("Tues-Thurs", {1, 2, 3}),
    ],
)
def test_the_poll_days(settings_factory, raw, expected):
    assert settings_factory(KITE_MCP_ORDER_POLL_DAYS=raw).order_poll_days == frozenset(expected)


@pytest.mark.parametrize("raw", ["Fri-Mon", "Funday", "Mon-", "Monkey"])
def test_bad_poll_days_are_refused(settings_factory, raw):
    with pytest.raises(ConfigError, match="KITE_MCP_ORDER_POLL_DAYS"):
        settings_factory(KITE_MCP_ORDER_POLL_DAYS=raw)


@pytest.mark.parametrize("raw", ["Fri-Mon", "Sun-Thu"])
def test_a_range_that_runs_backwards_is_explained(settings_factory, raw):
    with pytest.raises(ConfigError, match="ranges run forward from Mon to Sun; list the days to wrap"):
        settings_factory(KITE_MCP_ORDER_POLL_DAYS=raw)


def test_the_poll_interval_has_a_floor(settings_factory):
    with pytest.raises(ConfigError, match="KITE_MCP_ORDER_POLL_INTERVAL_S"):
        settings_factory(KITE_MCP_ORDER_POLL_INTERVAL_S="5")


def test_keep_days_is_at_least_one(settings_factory):
    with pytest.raises(ConfigError, match="KITE_MCP_EVENTS_KEEP_DAYS"):
        settings_factory(KITE_MCP_EVENTS_KEEP_DAYS="0")


def test_keep_days_has_a_ceiling(settings_factory):
    assert settings_factory(KITE_MCP_EVENTS_KEEP_DAYS="3650").events_keep_days == 3650
    for raw in ("3651", "1000000000"):  # far above, a timedelta of that many days would overflow in the purge
        with pytest.raises(ConfigError, match="KITE_MCP_EVENTS_KEEP_DAYS cannot exceed 3650"):
            settings_factory(KITE_MCP_EVENTS_KEEP_DAYS=raw)


def test_the_new_variables_are_known():
    names = (
        "KITE_MCP_EVENTS",
        "KITE_MCP_EVENTS_DB",
        "KITE_MCP_EVENTS_CONSUMERS_FILE",
        "KITE_MCP_ORDER_POLL_INTERVAL_S",
        "KITE_MCP_ORDER_POLL_WINDOW",
        "KITE_MCP_ORDER_POLL_DAYS",
        "KITE_MCP_EVENTS_KEEP_DAYS",
    )
    assert unknown_variables(dict.fromkeys(names, "x")) == []


def test_redacted_lists_the_events_settings(settings_factory):
    out = settings_factory().redacted()
    assert out["events"] is False
    assert out["order_poll_days"] == [0, 1, 2, 3, 4]
    assert out["order_poll_window"] == "09:00-16:00"


def test_redacted_with_the_service_on(settings_factory, tmp_path):
    out = settings_factory(**SERVICE).redacted()
    assert out["events"] is True
    assert out["events_db"] == str(tmp_path / "state" / "events.sqlite3")
    assert BEARER not in str(out)
