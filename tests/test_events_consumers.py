"""The consumers file: who gets which events, how a command is run, the tag labels, and that the file and each program
go through the trust check of events/trust.py."""

from __future__ import annotations

import errno
import json
import os
import re
from pathlib import Path

import pytest

from conftest import FIXED_NOW
from events_helpers import filled_row, folders, order_row
from kite_connect_mcp.errors import ConfigError
from kite_connect_mcp.events.consumers import DEFAULT_RETRY_DELAYS, Consumer, ConsumersConfig, Match, consumer_env
from kite_connect_mcp.events.model import OrderView, order_event, session_event

# A program the trust check accepts on any Linux or macOS machine: root's, in root's folders. The interpreter running
# the tests is not used here because some CI images keep it below folders anyone can write (see test_events_trust.py).
PROG = "/bin/sh"
PREFIX = "KITE_MCP_EVENTS_CONSUMERS_FILE: "


def write(tmp_path, data, mode=0o600):
    return write_text(tmp_path, json.dumps(data), mode)


def write_text(tmp_path, text, mode=0o600):
    return write_bytes(tmp_path, text.encode("utf-8"), mode)


def write_bytes(tmp_path, raw, mode=0o600):
    path = tmp_path / "consumers.json"
    path.write_bytes(raw)
    path.chmod(mode)
    return path


def refused(path, phrase):
    """Loading `path` fails with an error that names the file setting and holds `phrase`; returns the message."""
    with pytest.raises(ConfigError, match=re.escape(phrase)) as caught:
        ConsumersConfig.load(path)
    message = str(caught.value)
    assert message.startswith(PREFIX)
    return message


def event(row, change="filled", source="postback"):
    return order_event(
        OrderView.from_row(row),
        change=change,
        source=source,
        received_at=FIXED_NOW,
        tag_labels={},
        previous_event_id="",
    )


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
                    "command": [PROG, "-c", "exit 0"],
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
    assert c.name == "notify" and c.command == (PROG, "-c", "exit 0")
    assert c.timeout_s == 20 and c.alert_after_s == 90 and c.env_pass == ("NOTIFY_TOKEN",)
    assert [c.retry_delay(n) for n in (1, 2, 3, 4)] == [5, 15, 60, 60]
    assert c.give_up_after_s == 600
    assert config.tag_labels == {"mytag": "my strategy"}


def test_defaults(tmp_path):
    (c,) = ConsumersConfig.load(write(tmp_path, {"consumers": [{"name": "a", "command": [PROG]}]})).consumers
    assert c.match == Match()
    assert c.timeout_s == 30 and c.alert_after_s == 60 and c.give_up_after_s == 1800
    assert [c.retry_delay(n) for n in range(1, 8)] == [*DEFAULT_RETRY_DELAYS, 300, 300]


def test_the_limits_are_inclusive_and_give_up_zero_means_one_try(tmp_path):
    retry = {"delays_s": [1, 86400], "every_s": 86400, "give_up_after_s": 0}
    data = {"consumers": [{"name": "a", "command": [PROG], "timeout_s": 600, "alert_after_s": 86400, "retry": retry}]}
    (c,) = ConsumersConfig.load(write(tmp_path, data)).consumers
    assert c.timeout_s == 600 and c.alert_after_s == 86400 and c.give_up_after_s == 0
    assert [c.retry_delay(n) for n in (1, 2, 3)] == [1, 86400, 86400]


def test_statuses_and_transaction_types_are_read_in_any_case(tmp_path):
    match = {"statuses": ["complete", " Open "], "transaction_types": ["sell"]}
    data = {"consumers": [{"name": "a", "command": [PROG], "match": match}]}
    (c,) = ConsumersConfig.load(write(tmp_path, data)).consumers
    assert c.match == Match(statuses=frozenset({"COMPLETE", "OPEN"}), transaction_types=frozenset({"SELL"}))


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
                {"name": "a", "command": [PROG]},
                {"name": "b", "command": [PROG], "match": {"kinds": ["order", "selftest"]}},
            ]
        },
    )
    config = ConsumersConfig.load(path)
    assert config.matching(event(filled_row())) == ["a", "b"]
    assert config.matching(event(filled_row()), exclude="a") == ["b"]
    selftest = {"kind": "selftest", "event_id": "x"}
    assert config.matching(selftest) == ["b"]
    assert config.get("b") is not None and config.get("zzz") is None


def consumer(**fields):
    return {"consumers": [{"name": "a", "command": [PROG], **fields}]}


@pytest.mark.parametrize(
    ("data", "problem"),
    [
        ({"consumers": [{"name": "Bad Name", "command": [PROG]}]}, "name must be 1-40"),
        ({"consumers": [{"name": "a", "command": [PROG]}, {"name": "a", "command": [PROG]}]}, "is listed twice"),
        ({"consumers": [{"name": "a", "command": []}]}, "command must be a non-empty list"),
        ({"consumers": [{"name": "a", "command": [PROG, "a\x00b"]}]}, "command must be a non-empty list"),
        ({"consumers": [{"name": "a", "command": ["sh"]}]}, "must be an absolute path"),
        ({"consumers": [{"name": "a", "command": ["/no/such/program"]}]}, "is not an executable file"),
        (consumer(env_pass=["KITE_API_SECRET"]), "env_pass must not pass KITE_* variables"),
        (consumer(env_pass=["kite_x"]), "env_pass must not pass KITE_* variables"),
        (consumer(env_pass=["BAD-NAME"]), "env_pass must be a list"),
        (consumer(env_pass=None), "env_pass must be a list"),
        (consumer(match={"kinds": ["orders"]}), "match.kinds has unknown values"),
        (consumer(match={"changes": []}), "match.changes must not be empty"),
        (consumer(match={"colour": ["red"]}), "unknown match key 'colour'"),
        (consumer(match=None), "match must be an object"),
        (consumer(match=["order"]), "match must be an object"),
        (consumer(match={"kinds": ["session"], "changes": ["filled"]}), "match.changes applies to order events only"),
        (consumer(match={"kinds": ["router"], "tags": ["mytag"]}), "match.tags applies to order events only"),
        (consumer(timeout_s=0), "timeout_s must be"),
        (consumer(timeout_s=None), "timeout_s must be"),
        (consumer(alert_after_s=0), "alert_after_s must be"),
        (consumer(alert_after_s=86401), "alert_after_s must be"),
        (consumer(retry=None), "retry must be an object"),
        (consumer(retry=[5]), "retry must be an object"),
        (consumer(retry={"tries": 3}), "unknown retry key 'tries'"),
        (consumer(retry={"delays_s": [0]}), "retry.delays_s must be"),
        (consumer(retry={"delays_s": [86401]}), "retry.delays_s must be"),
        (consumer(retry={"every_s": 0}), "retry.every_s must be"),
        (consumer(retry={"every_s": "60"}), "retry.every_s must be"),
        (consumer(retry={"every_s": 10**400}), "retry.every_s must be"),
        (consumer(retry={"give_up_after_s": -1}), "retry.give_up_after_s must be"),
        (consumer(retry={"give_up_after_s": 86401}), "retry.give_up_after_s must be"),
        (consumer(retry={"give_up_after_s": "x"}), "retry.give_up_after_s must be"),
        (consumer(retry={"give_up_after_s": True}), "retry.give_up_after_s must be"),
        (consumer(unknown=1), "unknown key 'unknown'"),
        ({"consumers": ["a"]}, "consumers[0] must be an object"),
        ({"consumers": {"name": "a"}}, "consumers must be a list"),
        ({"consumers": None}, "consumers must be a list"),
        ({"consumers": [], "tag_labels": {"t": "x" * 41}}, "tag_labels must map"),
        ({"consumers": [], "tag_labels": {"t": "two\rlines"}}, "tag_labels must map"),
        ({"consumers": [], "tag_labels": {"t": "   "}}, "tag_labels must map"),
        ({"consumers": [], "tag_labels": {"t": "a\u202eb"}}, "tag_labels must map"),  # a right-to-left override
        ({"consumers": [], "tag_labels": ["t"]}, "tag_labels must map"),
        ({"consumers": [], "extra": 1}, "unknown key 'extra'"),
        ([], "the file must hold a JSON object"),
    ],
)
def test_problems_are_refused(tmp_path, data, problem):
    refused(write(tmp_path, data), problem)


def test_every_problem_is_reported_at_once(tmp_path):
    data = consumer(timeout_s=0, alert_after_s=0, retry={"every_s": 0})
    message = refused(write(tmp_path, {**data, "extra": 1}), "invalid consumers file")
    for phrase in ("timeout_s must be", "alert_after_s must be", "retry.every_s must be", "unknown key 'extra'"):
        assert phrase in message


@pytest.mark.parametrize(
    ("text", "key"),
    [
        (
            '{"consumers": [{"name": "a", "command": ["/bin/sh"], "match": {"kinds": ["session"]}, "match": {}}]}',
            "match",
        ),
        ('{"consumers": [{"name": "a", "command": ["/bin/sh"], "timeout_s": 0}], "consumers": []}', "consumers"),
        ('{"tag_labels": {"mytag": "one", "mytag": "two"}}', "mytag"),
    ],
)
def test_a_key_given_twice_is_refused(tmp_path, text, key):
    refused(write_text(tmp_path, text), f"is not valid JSON: duplicate key {key!r}")


def test_tag_labels_are_stripped_read_only_and_may_join_letters(tmp_path):
    hindi = "\u0915\u094d\u200c\u0937"  # a zero-width non-joiner keeps the conjunct apart
    emoji = "\U0001f468\u200d\U0001f4bb"  # a zero-width joiner makes one picture of two
    config = ConsumersConfig.load(write(tmp_path, {"tag_labels": {"mytag": "  my strategy  ", "h": hindi, "e": emoji}}))
    assert config.tag_labels == {"mytag": "my strategy", "h": hindi, "e": emoji}
    with pytest.raises(TypeError):
        config.tag_labels["mytag"] = "changed"
    assert hash(config) == hash(ConsumersConfig.load(write(tmp_path, {"tag_labels": {"mytag": "my strategy"}})))
    direct = ConsumersConfig(tag_labels={"mytag": "x"})
    with pytest.raises(TypeError):
        direct.tag_labels["mytag"] = "changed"


def test_a_missing_or_broken_file_is_refused(tmp_path):
    refused(tmp_path / "nope.json", "cannot read the consumers file")
    refused(tmp_path / "nope" / "consumers.json", f"cannot read the consumers file {tmp_path}/nope/consumers.json")
    refused(write_text(tmp_path, "{"), "is not valid JSON")
    refused(write_text(tmp_path, "[" * 100_000), "is not valid JSON")  # too deep for the parser: not a traceback
    refused(write(tmp_path, {}) / "x", f"{tmp_path}/consumers.json/x: {os.strerror(errno.ENOTDIR)}")


@pytest.mark.parametrize(
    "raw",
    [b'{"tag_labels": {"t": "\xe9"}}', '{"consumers": []}'.encode("utf-16"), '{"consumers": []}'.encode("utf-32")],
    ids=["latin-1", "utf-16", "utf-32"],
)
def test_a_file_that_is_not_utf8_is_refused(tmp_path, raw):
    refused(write_bytes(tmp_path, raw), "is not UTF-8 text")


def test_a_utf8_byte_order_mark_is_ignored(tmp_path):
    path = write_bytes(tmp_path, b"\xef\xbb\xbf" + b'{"tag_labels": {"mytag": "my strategy"}}')
    assert ConsumersConfig.load(path).tag_labels == {"mytag": "my strategy"}


# --- which files are trusted: the check itself is tested in test_events_trust.py ------------------------------------


def test_a_consumers_file_others_can_change_is_refused(tmp_path):
    path = write(tmp_path, {"consumers": []}, mode=0o666)
    refused(path, f"the consumers file {path} is writable by group or others (mode 666); run chmod go-w {path}")
    path.chmod(0o600)
    (folder,) = folders(tmp_path, "open")
    inner = write(folder, {"consumers": []})
    folder.chmod(0o777)
    refused(inner, f"the folder {folder} on the path to the consumers file {inner} is writable by group or others")


def test_the_file_read_is_the_one_checked(tmp_path):
    safe, other = folders(tmp_path, "safe", "other")
    write(safe, {"tag_labels": {"mytag": "safe"}})
    write(other, {"tag_labels": {"mytag": "other"}})
    folders(other, "sub")
    link = safe / "link"
    link.symlink_to(other / "sub")
    path = Path(f"{link}/../consumers.json")  # the system reads other/consumers.json, not safe/consumers.json
    assert ConsumersConfig.load(path).tag_labels == {"mytag": "other"}


def notify_program(folder, mode=0o700):
    path = folder / "notify"
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(mode)
    return path


def with_program(tmp_path, program):
    return write(tmp_path, {"consumers": [{"name": "a", "command": [str(program)]}]})


def test_a_program_others_can_change_is_refused(tmp_path):
    (folder,) = folders(tmp_path, "bin")
    program = notify_program(folder)
    (c,) = ConsumersConfig.load(with_program(tmp_path, program)).consumers
    assert c.command == (str(program),)
    program.chmod(0o777)
    refused(with_program(tmp_path, program), f"consumer 'a': the program {program} is writable by group or others")
    program.chmod(0o700)
    folder.chmod(0o777)
    refused(with_program(tmp_path, program), f"consumer 'a': the folder {folder} on the path to the program")
    folder.chmod(0o700)
    (open_folder,) = folders(tmp_path, "open")
    open_folder.chmod(0o777)
    link = open_folder / "notify"
    link.symlink_to(program)
    refused(with_program(tmp_path, link), f"consumer 'a': the folder {open_folder} on the path to the program")


def test_a_link_to_a_trusted_program_is_accepted(tmp_path):
    link = tmp_path / "python"  # like a virtualenv's bin/python, a link to a system interpreter
    link.symlink_to(PROG)
    (c,) = ConsumersConfig.load(with_program(tmp_path, link)).consumers
    assert c.command == (str(link),)


def test_a_link_to_the_program_is_followed_one_step_at_a_time(tmp_path):
    # The program is checked as written, not as os.path.realpath would give it: the links on the way count.
    safe, open_ = folders(tmp_path, "safe", "open")
    program = notify_program(safe)
    (open_ / "notify").symlink_to(program)
    link = safe / "start"
    link.symlink_to(open_ / "notify")
    assert ConsumersConfig.load(with_program(tmp_path, link)).consumers[0].command == (str(link),)
    open_.chmod(0o777)
    refused(with_program(tmp_path, link), f"consumer 'a': the folder {open_} on the path to the program {link}")


def test_the_consumer_environment_is_minimal():
    c = Consumer(name="notify", command=(PROG,), env_pass=("NOTIFY_TOKEN", "MISSING"))
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


def test_the_consumer_environment_never_holds_a_kite_variable_of_the_service():
    # A Consumer built in code skips the file's checks, so consumer_env filters again.
    c = Consumer(name="notify", command=(PROG,), env_pass=("KITE_API_SECRET", "kite_x", "KITE_EVENT_CONSUMER", "TOK"))
    environ = {"KITE_API_SECRET": "s", "kite_x": "x", "KITE_EVENT_CONSUMER": "spoofed", "TOK": "t"}
    assert consumer_env(c, environ) == {
        "PATH": os.defpath,
        "LANG": "C.UTF-8",
        "KITE_EVENT_CONSUMER": "notify",
        "TOK": "t",
    }
