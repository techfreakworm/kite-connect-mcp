"""The consumers file: who gets which events, how a command is run, the tag labels, and which files are trusted."""

from __future__ import annotations

import grp
import json
import os
import pwd
import re
import stat
import sys

import pytest

from conftest import FIXED_NOW
from events_helpers import filled_row, order_row
from kite_connect_mcp.errors import ConfigError
from kite_connect_mcp.events import consumers as consumers_module
from kite_connect_mcp.events.consumers import (
    DEFAULT_RETRY_DELAYS,
    Consumer,
    ConsumersConfig,
    Match,
    check_owned_file,
    consumer_env,
)
from kite_connect_mcp.events.model import OrderView, order_event, session_event

# A program the trust check accepts on any Linux or macOS machine: root's, in root's folders. The interpreter running
# the tests is not used here because some CI images keep it below folders anyone can write (see the last tests).
PROG = "/bin/sh"
PREFIX = "KITE_MCP_EVENTS_CONSUMERS_FILE: "


def write(tmp_path, data, mode=0o600):
    return write_text(tmp_path, json.dumps(data), mode)


def write_text(tmp_path, text, mode=0o600):
    path = tmp_path / "consumers.json"
    path.write_text(text)
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
    refused(write_text(tmp_path, "{"), "is not valid JSON")
    not_utf8 = tmp_path / "latin1.json"
    not_utf8.write_bytes(b'{"tag_labels": {"t": "\xe9"}}')
    not_utf8.chmod(0o600)
    refused(not_utf8, "is not valid JSON")


def test_a_reason_the_system_does_not_give_is_not_printed_as_none(tmp_path, monkeypatch):
    path = write(tmp_path, {"consumers": []})
    real_stat = os.stat

    def stat_without_reason(p, *args, **kwargs):
        if os.fspath(p) == os.fspath(path):
            raise OSError("no errno")
        return real_stat(p, *args, **kwargs)

    monkeypatch.setattr(os, "stat", stat_without_reason)
    assert "None" not in refused(path, "cannot read the consumers file")


def test_only_a_regular_file_is_read(tmp_path):
    fifo = tmp_path / "consumers.fifo"
    os.mkfifo(fifo, 0o600)
    refused(fifo, "is not a regular file")  # and is not opened: reading a FIFO would wait for a writer
    refused(tmp_path, "is not a regular file")


def groups_are(monkeypatch, *, private):
    """Make every group private to its owner, or shared, whatever this machine's groups are. Returns the (gid, uid)
    of every lookup made."""
    lookups = []

    def lookup(gid, uid):
        lookups.append((gid, uid))
        return private

    monkeypatch.setattr(consumers_module, "_group_is_private", lookup)
    return lookups


def test_a_file_others_can_write_is_refused(tmp_path, monkeypatch):
    groups_are(monkeypatch, private=False)
    refused(write(tmp_path, {"consumers": []}, mode=0o666), "run chmod go-w")
    path = write(tmp_path, {"consumers": []}, mode=0o620)
    refused(path, f"is writable by its group, which is not private to its owner (mode 620); run chmod g-w {path}")


def test_a_file_its_private_group_can_write_is_accepted(tmp_path, monkeypatch):
    lookups = groups_are(monkeypatch, private=True)
    path = write(tmp_path, {"consumers": []}, mode=0o660)
    assert ConsumersConfig.load(path).consumers == ()
    info = os.stat(path)
    assert (info.st_gid, info.st_uid) in lookups  # the file's own group and owner
    refused(write(tmp_path, {"consumers": []}, mode=0o662), "is writable by group or others (mode 662)")
    refused(write(tmp_path, {"consumers": []}, mode=0o606), "is writable by group or others (mode 606)")


def test_the_sticky_bit_spares_a_folder_only(tmp_path, monkeypatch):
    groups_are(monkeypatch, private=True)
    path = write(tmp_path, {"consumers": []})
    try:
        path.chmod(0o1666)
    except OSError:  # some systems refuse the sticky bit on a file
        pytest.skip("this system does not set the sticky bit on a file")
    refused(path, "is writable by group or others (mode 1666)")


def test_a_file_in_a_folder_others_can_write_is_refused_unless_the_folder_is_sticky(tmp_path, monkeypatch):
    groups_are(monkeypatch, private=False)
    folder = tmp_path / "open"
    folder.mkdir()
    path = write(folder, {"consumers": []})
    folder.chmod(0o777)
    refused(path, f"the folder {folder} above the consumers file")
    folder.chmod(0o770)
    refused(path, f"the folder {folder} above the consumers file")
    folder.chmod(0o1777)  # like /tmp: others may add files but not replace this one
    assert ConsumersConfig.load(path).consumers == ()


def test_a_folder_its_private_group_can_write_is_accepted(tmp_path, monkeypatch):
    lookups = groups_are(monkeypatch, private=True)
    folder = tmp_path / "home"  # like a folder made with Ubuntu's umask 002 in a group of the user's own
    folder.mkdir()
    path = write(folder, {"consumers": []})
    folder.chmod(0o775)
    assert ConsumersConfig.load(path).consumers == ()
    info = os.stat(folder)
    assert (info.st_gid, info.st_uid) in lookups  # the folder's own group and owner
    folder.chmod(0o777)
    refused(path, f"the folder {folder} above the consumers file {path} is writable by group or others (mode 777)")
    folder.chmod(0o757)
    refused(path, f"the folder {folder} above the consumers file {path} is writable by group or others (mode 757)")
    folder.chmod(0o1777)
    assert ConsumersConfig.load(path).consumers == ()


def test_a_folder_a_shared_group_can_write_is_refused(tmp_path, monkeypatch):
    groups_are(monkeypatch, private=False)
    folder = tmp_path / "shared"
    folder.mkdir()
    path = write(folder, {"consumers": []})
    folder.chmod(0o775)
    refused(
        path,
        f"the folder {folder} above the consumers file {path} is writable by its group, which is not private to its "
        f"owner (mode 775); run chmod g-w {folder}",
    )


def test_a_link_counts_where_it_is_and_where_it_points(tmp_path):
    target = write(tmp_path, {"consumers": []})
    safe_link = tmp_path / "link.json"
    safe_link.symlink_to(target)
    assert ConsumersConfig.load(safe_link).consumers == ()
    folder = tmp_path / "open"
    folder.mkdir()
    folder.chmod(0o777)
    open_link = folder / "link.json"
    open_link.symlink_to(target)
    refused(open_link, f"the folder {folder} above the consumers file")  # anyone could point the link elsewhere
    open_target = folder / "consumers.json"
    open_target.write_text("{}")
    open_target.chmod(0o600)
    target.unlink()
    target.symlink_to(open_target)
    refused(target, f"the folder {folder} above the consumers file")


def test_a_file_owned_by_someone_else_is_refused(tmp_path, monkeypatch):
    me = os.geteuid()
    if me == 0:
        pytest.skip("root's files are always trusted, and root owns every file this test can make")
    path = write(tmp_path, {"consumers": []})
    monkeypatch.setattr(os, "geteuid", lambda: me + 1)
    refused(path, f"is owned by uid {me}, not by this user or root")


def notify_program(folder, mode=0o700):
    path = folder / "notify"
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(mode)
    return path


def with_program(tmp_path, program):
    return write(tmp_path, {"consumers": [{"name": "a", "command": [str(program)]}]})


def test_a_program_others_can_change_is_refused(tmp_path):
    folder = tmp_path / "bin"
    folder.mkdir()
    program = notify_program(folder)
    (c,) = ConsumersConfig.load(with_program(tmp_path, program)).consumers
    assert c.command == (str(program),)
    program.chmod(0o777)
    refused(with_program(tmp_path, program), f"consumer 'a': the program {program} is writable by group or others")
    program.chmod(0o700)
    folder.chmod(0o777)
    refused(with_program(tmp_path, program), f"consumer 'a': the folder {folder} above the program")
    folder.chmod(0o700)
    open_folder = tmp_path / "open"
    open_folder.mkdir()
    open_folder.chmod(0o777)
    link = open_folder / "notify"
    link.symlink_to(program)
    refused(with_program(tmp_path, link), f"consumer 'a': the folder {open_folder} above the program")


def test_a_program_its_private_group_can_write_is_accepted_and_a_shared_one_refused(tmp_path, monkeypatch):
    folder = tmp_path / "bin"
    folder.mkdir()
    folder.chmod(0o775)
    program = notify_program(folder, mode=0o775)
    groups_are(monkeypatch, private=True)
    (c,) = ConsumersConfig.load(with_program(tmp_path, program)).consumers
    assert c.command == (str(program),)
    groups_are(monkeypatch, private=False)
    refused(with_program(tmp_path, program), f"consumer 'a': the program {program} is writable by its group")
    program.chmod(0o755)
    refused(with_program(tmp_path, program), f"consumer 'a': the folder {folder} above the program")


def test_a_link_to_a_trusted_program_is_accepted(tmp_path):
    link = tmp_path / "python"  # like a virtualenv's bin/python, a link to a system interpreter
    link.symlink_to(PROG)
    (c,) = ConsumersConfig.load(with_program(tmp_path, link)).consumers
    assert c.command == (str(link),)


# --- whether a group is private to its owner ------------------------------------------------------------------------

ME = ("me", 1000, 1000)  # (name, uid, primary gid): a user with a group of their own


def accounts(monkeypatch, users, groups):
    """Replace this machine's accounts: `users` are (name, uid, primary gid), `groups` map a gid to its
    supplementary members. A uid or gid not listed is unknown (KeyError), as on a real machine."""
    entries = [pwd.struct_passwd((n, "x", u, g, "", "/nonexistent", "/bin/sh")) for n, u, g in users]

    def getpwuid(uid):
        for entry in entries:
            if entry.pw_uid == uid:
                return entry
        raise KeyError(uid)

    def getgrgid(gid):
        if gid not in groups:
            raise KeyError(gid)
        return grp.struct_group((f"g{gid}", "x", gid, list(groups[gid])))

    monkeypatch.setattr(pwd, "getpwall", lambda: list(entries))
    monkeypatch.setattr(pwd, "getpwuid", getpwuid)
    monkeypatch.setattr(grp, "getgrgid", getgrgid)


@pytest.mark.parametrize(
    ("users", "members", "private"),
    [
        ([ME], [], True),  # Ubuntu's user-private group: the owner's primary group, no one else in it
        ([ME], ["me"], True),  # the owner listed as a member too
        ([ME, ("me-alias", 1000, 1000)], [], True),  # a second name for the same uid is the same user
        ([ME, ("other", 1001, 1001)], ["other"], False),  # another user is a member
        ([ME, ("other", 1001, 1001)], ["me", "other"], False),
        ([ME, ("other", 1001, 1000)], [], False),  # another user has it as primary group
        ([ME], ["ghost"], False),  # a member with no account is still someone else
    ],
    ids=["own", "owner-member", "alias", "member", "members", "other-primary", "unknown-member"],
)
def test_a_group_is_private_only_when_no_one_else_is_in_it(monkeypatch, users, members, private):
    accounts(monkeypatch, users, {1000: members})
    assert consumers_module._group_is_private(1000, 1000) is private


def test_a_group_or_owner_that_cannot_be_looked_up_counts_as_shared(monkeypatch):
    accounts(monkeypatch, [ME], {1000: []})
    assert consumers_module._group_is_private(4242, 1000) is False  # no such group
    assert consumers_module._group_is_private(1000, 4242) is False  # no such owner


def test_the_folder_s_own_group_and_owner_are_looked_up(tmp_path, monkeypatch):
    folder = tmp_path / "home"
    folder.mkdir()
    path = write(folder, {"consumers": []})
    folder.chmod(0o775)
    gid, uid = folder.stat().st_gid, folder.stat().st_uid
    accounts(monkeypatch, [("me", uid, gid)], {gid: []})
    assert ConsumersConfig.load(path).consumers == ()
    accounts(monkeypatch, [("me", uid, gid), ("other", uid + 1, gid)], {gid: []})
    refused(path, f"the folder {folder} above the consumers file {path} is writable by its group, which is not private")


def _private_group(gid, uid):
    """Whether no user but `uid` is in group `gid`, as a member or by primary group (an independent reckoning)."""
    try:
        group, owner = grp.getgrgid(gid), pwd.getpwuid(uid)
    except KeyError:
        return False
    by_primary = {p.pw_name for p in pwd.getpwall() if p.pw_gid == gid and p.pw_uid != uid}
    return not by_primary and set(group.gr_mem) <= {owner.pw_name}


def test_the_real_lookup_of_my_own_primary_group():
    uid = os.geteuid()
    try:
        me = pwd.getpwuid(uid)
    except KeyError:  # a container user without an account: the owner cannot be looked up
        assert consumers_module._group_is_private(os.getegid(), uid) is False
        return
    assert consumers_module._group_is_private(me.pw_gid, uid) is _private_group(me.pw_gid, uid)
    if uid != 0:
        root_group = pwd.getpwuid(0).pw_gid  # root's primary group: root is in it, so it is not private to me
        assert consumers_module._group_is_private(root_group, uid) is False


def _open_folders(path):
    """The folders above `path`, as written and resolved, that someone else could change (an independent reckoning)."""
    found = []
    for start in (os.path.abspath(path), os.path.realpath(path)):
        folder = os.path.dirname(start)
        while True:
            info = os.stat(folder)
            writable = info.st_mode & 0o002 or (info.st_mode & 0o020 and not _private_group(info.st_gid, info.st_uid))
            open_mode = writable and not info.st_mode & stat.S_ISVTX
            if open_mode or info.st_uid not in (os.geteuid(), 0):
                found.append(folder)
            if os.path.dirname(folder) == folder:
                break
            folder = os.path.dirname(folder)
    return found


@pytest.mark.parametrize("resolve", [False, True], ids=["as-written", "resolved"])
def test_the_interpreter_running_the_tests_is_accepted(resolve):
    interpreter = os.path.realpath(sys.executable) if resolve else sys.executable
    open_folders = _open_folders(interpreter)
    if open_folders:
        pytest.skip(f"this machine keeps the interpreter below folders others can change: {open_folders}")
    assert check_owned_file(interpreter, "the program") is None


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
