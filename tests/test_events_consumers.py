"""The consumers file: who gets which events, how a command is run, and the tag labels."""

from __future__ import annotations

import json
import sys

import pytest

from conftest import FIXED_NOW
from events_helpers import filled_row, order_row
from kite_connect_mcp.errors import ConfigError
from kite_connect_mcp.events.model import OrderView, order_event, session_event
from kite_connect_mcp.events.router import DEFAULT_RETRY_DELAYS, ConsumersConfig, Match

PY = sys.executable


def write(tmp_path, data, mode=0o600):
    path = tmp_path / "consumers.json"
    path.write_text(json.dumps(data))
    path.chmod(mode)
    return path


def event(row, change="filled", source="postback"):
    return order_event(OrderView.from_row(row), change=change, source=source, received_at=FIXED_NOW, tag_labels={})


def test_no_file_means_no_consumers():
    config = ConsumersConfig.load(None)
    assert config.consumers == () and dict(config.tag_labels) == {}


def test_a_full_consumer(tmp_path):
    path = write(
        tmp_path,
        {
            "tag_labels": {"mytag": "my strategy"},
            "consumers": [
                {
                    "name": "notify",
                    "command": [PY, "-c", "pass"],
                    "match": {"kinds": ["order", "session"], "changes": ["filled", "rejected"]},
                    "timeout_s": 20,
                    "alert_after_s": 90,
                    "retry": {"delays_s": [5, 15], "every_s": 60, "give_up_after_s": 600},
                    "env_pass": ["NOTIFY_TOKEN"],
                }
            ],
        },
    )
    config = ConsumersConfig.load(path)
    (c,) = config.consumers
    assert c.name == "notify" and c.command == (PY, "-c", "pass")
    assert c.timeout_s == 20 and c.alert_after_s == 90 and c.env_pass == ("NOTIFY_TOKEN",)
    assert [c.retry_delay(n) for n in (1, 2, 3, 4)] == [5, 15, 60, 60]
    assert c.give_up_after_s == 600
    assert config.tag_labels == {"mytag": "my strategy"}


def test_defaults(tmp_path):
    (c,) = ConsumersConfig.load(write(tmp_path, {"consumers": [{"name": "a", "command": [PY]}]})).consumers
    assert c.match == Match()
    assert c.timeout_s == 30 and c.alert_after_s == 60 and c.give_up_after_s == 1800
    assert [c.retry_delay(n) for n in range(1, 8)] == [*DEFAULT_RETRY_DELAYS, 300, 300]


def test_matching():
    m = Match(kinds=frozenset({"order"}), changes=frozenset({"filled"}), tags=frozenset({"mytag"}))
    assert m.accepts(event(filled_row()))
    assert not m.accepts(event(filled_row(tag="other", tags=["other"])))
    assert not m.accepts(event(order_row(), "placed"))
    assert not m.accepts(session_event("token_rejected", seed="s", received_at=FIXED_NOW))
    by_source = Match(
        sources=frozenset({"poll"}), transaction_types=frozenset({"SELL"}), statuses=frozenset({"COMPLETE"})
    )
    assert by_source.accepts(event(filled_row(transaction_type="SELL"), source="poll"))
    assert not by_source.accepts(event(filled_row(transaction_type="SELL"), source="postback"))
    sessions = Match(kinds=frozenset({"session"}), changes=frozenset({"filled"}))
    assert sessions.accepts(session_event("token_rejected", seed="s", received_at=FIXED_NOW))  # order filters ignored


def test_only_kinds_asked_for_are_delivered_and_a_consumer_can_be_left_out(tmp_path):
    path = write(
        tmp_path,
        {
            "consumers": [
                {"name": "a", "command": [PY]},
                {"name": "b", "command": [PY], "match": {"kinds": ["order", "selftest"]}},
            ]
        },
    )
    config = ConsumersConfig.load(path)
    assert config.matching(event(filled_row())) == ["a", "b"]
    assert config.matching(event(filled_row()), exclude="a") == ["b"]
    selftest = {"kind": "selftest", "event_id": "x"}
    assert config.matching(selftest) == ["b"]
    assert config.get("b") is not None and config.get("zzz") is None


@pytest.mark.parametrize(
    ("data", "problem"),
    [
        ({"consumers": [{"name": "Bad Name", "command": [PY]}]}, "name"),
        ({"consumers": [{"name": "a", "command": [PY]}, {"name": "a", "command": [PY]}]}, "twice"),
        ({"consumers": [{"name": "a", "command": []}]}, "command"),
        ({"consumers": [{"name": "a", "command": ["python3"]}]}, "absolute"),
        ({"consumers": [{"name": "a", "command": ["/no/such/program"]}]}, "executable"),
        ({"consumers": [{"name": "a", "command": [PY], "env_pass": ["KITE_API_SECRET"]}]}, "KITE_"),
        ({"consumers": [{"name": "a", "command": [PY], "env_pass": ["kite_x"]}]}, "KITE_"),
        ({"consumers": [{"name": "a", "command": [PY], "env_pass": ["BAD-NAME"]}]}, "env_pass"),
        ({"consumers": [{"name": "a", "command": [PY], "match": {"kinds": ["orders"]}}]}, "kinds"),
        ({"consumers": [{"name": "a", "command": [PY], "match": {"changes": []}}]}, "empty"),
        ({"consumers": [{"name": "a", "command": [PY], "match": {"colour": ["red"]}}]}, "colour"),
        ({"consumers": [{"name": "a", "command": [PY], "timeout_s": 0}]}, "timeout_s"),
        ({"consumers": [{"name": "a", "command": [PY], "retry": {"delays_s": [0]}}]}, "delays_s"),
        ({"consumers": [{"name": "a", "command": [PY], "retry": {"every_s": 0}}]}, "every_s"),
        ({"consumers": [{"name": "a", "command": [PY], "retry": {"every_s": "60"}}]}, "every_s"),
        ({"consumers": [{"name": "a", "command": [PY], "unknown": 1}]}, "unknown"),
        ({"consumers": [], "tag_labels": {"t": "x" * 41}}, "tag_labels"),
        ({"consumers": [], "tag_labels": {"t": "two\rlines"}}, "tag_labels"),
        ({"consumers": [], "extra": 1}, "extra"),
        ([], "object"),
    ],
)
def test_problems_are_refused(tmp_path, data, problem):
    with pytest.raises(ConfigError, match=problem):
        ConsumersConfig.load(write(tmp_path, data))


def test_a_file_others_can_write_is_refused(tmp_path):
    with pytest.raises(ConfigError, match="chmod"):
        ConsumersConfig.load(write(tmp_path, {"consumers": []}, mode=0o666))


def test_a_missing_or_broken_file_is_refused(tmp_path):
    with pytest.raises(ConfigError, match="cannot read"):
        ConsumersConfig.load(tmp_path / "nope.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{")
    bad.chmod(0o600)
    with pytest.raises(ConfigError, match="JSON"):
        ConsumersConfig.load(bad)
    not_utf8 = tmp_path / "latin1.json"
    not_utf8.write_bytes(b'{"tag_labels": {"t": "\xe9"}}')
    not_utf8.chmod(0o600)
    with pytest.raises(ConfigError, match="JSON"):
        ConsumersConfig.load(not_utf8)


def test_the_consumer_environment_is_minimal():
    from kite_connect_mcp.events.router import Consumer, consumer_env

    c = Consumer(name="notify", command=(PY,), env_pass=("NOTIFY_TOKEN", "MISSING"))
    env = consumer_env(
        c,
        {
            "PATH": "/bin",
            "HOME": "/h",
            "LANG": "en_IN.UTF-8",
            "NOTIFY_TOKEN": "t",
            "KITE_API_SECRET": "s",
            "OTHER": "o",
        },
    )
    assert env == {
        "PATH": "/bin",
        "HOME": "/h",
        "LANG": "en_IN.UTF-8",
        "KITE_EVENT_CONSUMER": "notify",
        "NOTIFY_TOKEN": "t",
    }
