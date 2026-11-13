"""Which files the service trusts to read or run: only this user or root may be able to change them, or change where
their path leads. The path is followed one entry at a time, as the system follows it."""

from __future__ import annotations

import errno
import grp
import os
import pwd
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from events_helpers import folders
from kite_connect_mcp.events import trust
from kite_connect_mcp.events.trust import UntrustedFileError, check_owned_file, read_owned_file

WHAT = "the file"
OTHER = os.geteuid() + 1  # a uid that is neither this user nor root
NOT_A_FOLDER = os.strerror(errno.ENOTDIR)


def make_file(folder, name="consumers.json", mode=0o600, text="{}"):
    path = folder / name
    path.write_text(text)
    path.chmod(mode)
    return path


def untrusted(path, phrase):
    """check_owned_file refuses `path` with a reason that holds `phrase`; returns the reason."""
    reason = check_owned_file(path, WHAT)
    assert reason is not None, f"{path} is trusted"
    assert phrase in reason
    return reason


def trusted(path):
    return check_owned_file(path, WHAT) is None


def groups_are(monkeypatch, *, private):
    """Make every group private to its owner, or shared, whatever this machine's groups are. Returns the (gid, uid)
    of every lookup made."""
    lookups = []

    def lookup(gid, uid):
        lookups.append((gid, uid))
        return private

    monkeypatch.setattr(trust, "_group_is_private", lookup)
    return lookups


def acls_are(monkeypatch, *, present):
    """Make every file and folder have a POSIX access ACL, or none. Returns the paths probed."""
    probed = []

    def probe(path):
        probed.append(path)
        return present

    monkeypatch.setattr(trust, "_has_access_acl", probe)
    return probed


def owned_by(monkeypatch, path, uid):
    """Make the system report the entry at `path` itself (a link not followed) as owned by `uid`."""
    real_lstat = os.lstat
    faked = os.fspath(path)

    def lstat(p, *args, **kwargs):
        info = real_lstat(p, *args, **kwargs)
        if os.fspath(p) != faked:
            return info
        fields = list(info[:10])
        fields[4] = uid  # st_uid
        return os.stat_result(fields)

    monkeypatch.setattr(os, "lstat", lstat)


# --- reading the file that was checked ------------------------------------------------------------------------------


def test_the_file_checked_is_the_file_read(tmp_path):
    path = make_file(tmp_path, text='{"x": 1}')
    assert trusted(path)
    assert read_owned_file(path, WHAT) == b'{"x": 1}'
    path.chmod(0o666)
    with pytest.raises(UntrustedFileError, match=re.escape(f"the file {path} is writable by group or others")):
        read_owned_file(path, WHAT)


def test_a_file_trusted_but_unreadable_names_the_path_as_given(tmp_path, monkeypatch):
    path = make_file(tmp_path)

    def read_bytes(self):
        raise OSError(errno.EACCES, os.strerror(errno.EACCES))

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    with pytest.raises(UntrustedFileError) as caught:
        read_owned_file(path, WHAT)
    assert str(caught.value) == f"cannot read the file {path}: {os.strerror(errno.EACCES)}"


def test_a_reason_the_system_does_not_give_is_not_printed_as_none(tmp_path, monkeypatch):
    path = make_file(tmp_path)
    real_lstat = os.lstat

    def lstat_without_reason(p, *args, **kwargs):
        if os.fspath(p) == os.fspath(path):
            raise OSError("no errno")
        return real_lstat(p, *args, **kwargs)

    monkeypatch.setattr(os, "lstat", lstat_without_reason)
    assert check_owned_file(path, WHAT) == f"cannot read the file {path}: OSError"


def test_only_a_regular_file_is_trusted_and_a_fifo_is_not_opened(tmp_path):
    fifo = tmp_path / "consumers.fifo"
    os.mkfifo(fifo, 0o600)
    with pytest.raises(UntrustedFileError, match="is not a regular file"):
        read_owned_file(fifo, WHAT)  # and is not opened: reading a FIFO would wait for a writer
    untrusted(tmp_path, f"the file {tmp_path} is not a regular file")


def test_a_missing_file_or_folder_is_refused(tmp_path):
    untrusted(tmp_path / "nope.json", f"cannot read the file {tmp_path}/nope.json: {os.strerror(errno.ENOENT)}")
    untrusted(tmp_path / "nope" / "x", f"cannot read the file {tmp_path}/nope/x: {os.strerror(errno.ENOENT)}")


# --- the mode and owner of the file and of the folders it is in -----------------------------------------------------


def test_a_file_others_can_write_is_refused(tmp_path, monkeypatch):
    groups_are(monkeypatch, private=False)
    path = make_file(tmp_path, mode=0o666)
    untrusted(path, f"the file {path} is writable by group or others (mode 666); run chmod go-w {path}")
    path.chmod(0o620)
    untrusted(path, f"is writable by its group, which is not private to its owner (mode 620); run chmod g-w {path}")


def test_a_file_its_private_group_can_write_is_trusted(tmp_path, monkeypatch):
    lookups = groups_are(monkeypatch, private=True)
    path = make_file(tmp_path, mode=0o660)
    assert trusted(path)
    info = os.stat(path)
    assert (info.st_gid, info.st_uid) in lookups  # the file's own group and owner
    path.chmod(0o662)
    untrusted(path, "is writable by group or others (mode 662)")
    path.chmod(0o606)
    untrusted(path, "is writable by group or others (mode 606)")


def test_the_sticky_bit_spares_a_folder_only(tmp_path, monkeypatch):
    groups_are(monkeypatch, private=True)
    path = make_file(tmp_path)
    try:
        path.chmod(0o1666)
    except OSError:  # some systems refuse the sticky bit on a file
        pytest.skip("this system does not set the sticky bit on a file")
    untrusted(path, "is writable by group or others (mode 1666)")


def test_a_file_in_a_folder_others_can_write_is_refused_unless_the_folder_is_sticky(tmp_path, monkeypatch):
    groups_are(monkeypatch, private=False)
    (folder,) = folders(tmp_path, "open")
    path = make_file(folder)
    folder.chmod(0o777)
    untrusted(path, f"the folder {folder} on the path to the file {path}")
    folder.chmod(0o770)
    untrusted(path, f"the folder {folder} on the path to the file {path}")
    folder.chmod(0o1777)  # like /tmp: others may add files but not replace this one
    assert trusted(path)


def test_a_folder_its_private_group_can_write_is_trusted(tmp_path, monkeypatch):
    lookups = groups_are(monkeypatch, private=True)
    (folder,) = folders(tmp_path, "home")
    path = make_file(folder)
    folder.chmod(0o775)  # like a folder made with Ubuntu's umask 002 in a group of the user's own
    assert trusted(path)
    info = os.stat(folder)
    assert (info.st_gid, info.st_uid) in lookups  # the folder's own group and owner
    folder.chmod(0o777)
    untrusted(path, f"the folder {folder} on the path to the file {path} is writable by group or others (mode 777)")
    folder.chmod(0o757)
    untrusted(path, f"the folder {folder} on the path to the file {path} is writable by group or others (mode 757)")
    folder.chmod(0o1777)
    assert trusted(path)


def test_a_folder_a_shared_group_can_write_is_refused(tmp_path, monkeypatch):
    groups_are(monkeypatch, private=False)
    (folder,) = folders(tmp_path, "shared")
    path = make_file(folder)
    folder.chmod(0o775)
    untrusted(
        path,
        f"the folder {folder} on the path to the file {path} is writable by its group, which is not private "
        f"to its owner (mode 775); run chmod g-w {folder}",
    )


def test_the_folders_are_checked_before_the_file(tmp_path, monkeypatch):
    (folder,) = folders(tmp_path, "bin")
    folder.chmod(0o775)
    program = make_file(folder, "notify", mode=0o775, text="#!/bin/sh\nexit 0\n")
    groups_are(monkeypatch, private=True)
    assert trusted(program)
    groups_are(monkeypatch, private=False)
    untrusted(program, f"the folder {folder} on the path to the file {program} is writable by its group")
    folder.chmod(0o755)
    untrusted(program, f"the file {program} is writable by its group, which is not private to its owner (mode 775)")


def test_a_file_owned_by_someone_else_is_refused(tmp_path, monkeypatch):
    path = make_file(tmp_path)
    owned_by(monkeypatch, path, OTHER)
    assert check_owned_file(path, WHAT) == f"the file {path} is owned by uid {OTHER}, not by this user or root"


def test_every_folder_up_to_the_root_must_be_owned_by_this_user_or_root(tmp_path, monkeypatch):
    path = Path(os.path.realpath(make_file(tmp_path)))  # holds no link: its parents are the folders looked in
    assert path.parents[-1] == Path("/")
    for folder in path.parents:  # each folder in turn, from tmp_path up to the root, owned by someone else
        with monkeypatch.context() as patch:
            owned_by(patch, folder, OTHER)
            assert check_owned_file(path, WHAT) == (
                f"the folder {folder} on the path to the file {path} is owned by uid {OTHER}, not by this user or root"
            )
    assert trusted(path)


# --- links, followed one step at a time as the system does ----------------------------------------------------------


def test_a_link_counts_where_it_is_and_where_it_points(tmp_path):
    target = make_file(tmp_path)
    safe_link = tmp_path / "link.json"
    safe_link.symlink_to(target)
    assert trusted(safe_link)
    (folder,) = folders(tmp_path, "open")
    folder.chmod(0o777)
    open_link = folder / "link.json"
    open_link.symlink_to(target)
    # anyone could point the link elsewhere
    untrusted(open_link, f"the folder {folder} on the path to the file {open_link}")
    open_target = make_file(folder)
    target.unlink()
    target.symlink_to(open_target)
    untrusted(target, f"the folder {folder} on the path to the file {target}")


def test_a_chain_of_links_through_a_folder_others_can_write_is_refused(tmp_path):
    safe, open_ = folders(tmp_path, "safe", "open")
    target = make_file(safe)
    (open_ / "mid").symlink_to(target)
    start = safe / "start"
    start.symlink_to(open_ / "mid")  # safe/start -> open/mid -> safe/consumers.json: anyone can repoint mid
    assert trusted(start)
    open_.chmod(0o777)
    untrusted(start, f"the folder {open_} on the path to the file {start} is writable by group or others (mode 777)")


def test_dot_dot_after_a_link_goes_up_from_where_the_link_leads(tmp_path):
    safe, other, open_ = folders(tmp_path, "safe", "other", "open")
    make_file(safe, text="safe")
    make_file(other, text="other")
    folders(other, "sub")
    link = safe / "link"
    link.symlink_to(other / "sub")
    path = f"{link}/../consumers.json"  # the system reads other/consumers.json, not safe/consumers.json
    assert Path(path).read_text() == "other"
    assert read_owned_file(path, WHAT) == b"other"  # checked and read where the system goes
    folders(open_, "sub")
    make_file(open_, text="open")
    open_.chmod(0o777)
    link.unlink()
    link.symlink_to(open_ / "sub")  # now .. leads into a folder anyone can write, whatever safe/consumers.json is
    untrusted(path, f"the folder {open_} on the path to the file {path} is writable by group or others")


def test_a_link_someone_else_owns_is_refused_even_in_a_sticky_folder(tmp_path, monkeypatch):
    # In a sticky folder such as /tmp, whoever owns a link may replace it, and so decides where it leads.
    target = make_file(tmp_path)
    (sticky,) = folders(tmp_path, "sticky")
    sticky.chmod(0o1777)
    link = sticky / "consumers.json"
    link.symlink_to(target)
    folder_link = sticky / "dir"
    folder_link.symlink_to(tmp_path)
    assert trusted(link)  # links of this user's own are fine there
    assert trusted(folder_link / "consumers.json")
    owned_by(monkeypatch, link, OTHER)
    untrusted(link, f"the link {link} on the path to the file {link} is owned by uid {OTHER}, not by this user")
    owned_by(monkeypatch, folder_link, OTHER)
    untrusted(folder_link / "consumers.json", f"the link {folder_link} on the path to the file")


def test_at_most_40_links_are_followed(tmp_path):
    target = make_file(tmp_path)
    previous = target.name
    for n in range(41):  # link<n> is n + 1 links away from the file
        (tmp_path / f"link{n}").symlink_to(previous)
        previous = f"link{n}"
    assert trusted(tmp_path / "link39")
    too_many = os.strerror(errno.ELOOP)
    assert check_owned_file(tmp_path / "link40", WHAT) == f"cannot read the file {tmp_path}/link40: {too_many}"
    if sys.platform == "linux":  # the same limit as the system's
        os.stat(tmp_path / "link39")
        with pytest.raises(OSError, match=too_many):
            os.stat(tmp_path / "link40")
    (tmp_path / "loop").symlink_to("loop")
    assert check_owned_file(tmp_path / "loop", WHAT) == f"cannot read the file {tmp_path}/loop: {too_many}"


def test_a_relative_path_starts_from_the_current_folder(tmp_path, monkeypatch):
    make_file(tmp_path)
    (tmp_path / "link").symlink_to("consumers.json")
    monkeypatch.chdir(tmp_path)
    assert trusted("link")
    (open_,) = folders(tmp_path, "open")
    make_file(open_)
    open_.chmod(0o777)
    untrusted("open/consumers.json", f"the folder {open_} on the path to the file {open_}/consumers.json")
    monkeypatch.chdir(open_)
    # '..' is no entry anyone could replace, so nothing is looked up in the open folder on the way back out of it
    assert trusted("../consumers.json")


# --- what the system refuses with "Not a directory" ------------------------------------------------------------------


def files_and_links(tmp_path):
    """consumers.json, f and g (files), sub (a folder), and links to them: one ending in '/', one to a folder."""
    for name in ("consumers.json", "f", "g"):
        make_file(tmp_path, name)
    folders(tmp_path, "sub")
    (tmp_path / "slash-link").symlink_to("consumers.json/")  # a '/' at the end of a link asks for a folder too
    (tmp_path / "folder-link").symlink_to("sub/")


@pytest.mark.parametrize(
    "written",
    [
        "consumers.json/",
        "consumers.json/.",
        "consumers.json/..",
        "consumers.json/../consumers.json",
        "f/..//g",
        "f/./g",
        "f/g",
        "slash-link",
        "slash-link/..",
        "folder-link/../f/",
    ],
)
def test_dot_dot_dot_or_a_slash_after_a_file_is_refused_as_the_system_refuses_it(tmp_path, written):
    files_and_links(tmp_path)
    path = f"{tmp_path}/{written}"
    with pytest.raises(NotADirectoryError):
        os.stat(path)  # the system's own answer
    assert check_owned_file(path, WHAT) == f"cannot read the file {path}: {NOT_A_FOLDER}"


@pytest.mark.parametrize(
    "written",
    ["./consumers.json", "sub/../consumers.json", "sub/./../f", "sub//..//g", "folder-link/../consumers.json"],
)
def test_dot_and_dot_dot_after_a_folder_are_followed(tmp_path, written):
    files_and_links(tmp_path)
    path = f"{tmp_path}/{written}"
    assert stat.S_ISREG(os.stat(path).st_mode)  # the system finds the file
    assert trusted(path)


def test_a_slash_after_a_folder_still_leads_to_no_file(tmp_path):
    files_and_links(tmp_path)
    untrusted(f"{tmp_path}/sub/", f"the file {tmp_path}/sub/ is not a regular file")
    untrusted(f"{tmp_path}/folder-link", f"the file {tmp_path}/folder-link is not a regular file")


# --- POSIX ACLs ------------------------------------------------------------------------------------------------------


def test_group_write_is_refused_where_an_acl_could_let_others_write(tmp_path, monkeypatch):
    # With an access ACL the group bits are its mask: users and groups the ACL names may write, whatever the group.
    groups_are(monkeypatch, private=True)
    probed = acls_are(monkeypatch, present=True)
    path = make_file(tmp_path, mode=0o660)
    untrusted(
        path,
        f"the file {path} has an access ACL, so the users and groups it names may write it (mode 660); "
        f"run chmod g-w {path}",
    )
    assert probed == [str(path)]
    path.chmod(0o640)  # the mask then keeps every entry of the ACL from writing
    assert trusted(path)
    (folder,) = folders(tmp_path, "home")
    inner = make_file(folder)
    folder.chmod(0o775)
    untrusted(inner, f"the folder {folder} on the path to the file {inner} has an access ACL")
    folder.chmod(0o1777)  # sticky: others may add entries but not replace this one, ACL or not
    assert trusted(inner)


def test_the_acl_is_probed_only_when_the_group_is_private(tmp_path, monkeypatch):
    groups_are(monkeypatch, private=False)
    probed = acls_are(monkeypatch, present=False)
    path = make_file(tmp_path, mode=0o660)
    untrusted(path, "is writable by its group, which is not private to its owner")
    assert probed == []


@pytest.mark.parametrize(
    ("outcome", "has_acl"),
    [
        (b"\x02\x00\x00\x00", True),
        (errno.ENODATA, False),
        (errno.ENOTSUP, False),
        (errno.EOPNOTSUPP, False),
        (errno.EACCES, True),
        (errno.EIO, True),
    ],
    ids=["present", "absent", "not-supported", "op-not-supported", "access-denied", "io-error"],
)
def test_the_acl_probe_counts_any_doubt_as_an_acl(monkeypatch, outcome, has_acl):
    def getxattr(path, attribute, *, follow_symlinks=True):
        assert (path, attribute, follow_symlinks) == ("/x", "system.posix_acl_access", False)
        if isinstance(outcome, int):
            raise OSError(outcome, os.strerror(outcome))
        return outcome

    monkeypatch.setattr(os, "getxattr", getxattr, raising=False)
    assert trust._has_access_acl("/x") is has_acl


def test_a_system_that_cannot_read_extended_attributes_counts_as_having_an_acl(monkeypatch):
    monkeypatch.delattr(os, "getxattr", raising=False)  # as on macOS
    assert trust._has_access_acl("/x") is True


@pytest.mark.skipif(shutil.which("setfacl") is None, reason="needs setfacl")
def test_a_real_acl_is_seen(tmp_path, monkeypatch):
    groups_are(monkeypatch, private=True)
    path = make_file(tmp_path, mode=0o660)
    assert trust._has_access_acl(str(path)) is False
    assert trusted(path)
    setfacl = subprocess.run(["setfacl", "-m", f"u:{OTHER}:rw", str(path)], capture_output=True, check=False)
    if setfacl.returncode != 0:
        pytest.skip("this filesystem keeps no ACLs")
    assert stat.S_IMODE(path.stat().st_mode) == 0o660  # the mode looks the same: the group bits are now the mask
    assert trust._has_access_acl(str(path)) is True
    untrusted(path, f"the file {path} has an access ACL")


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
    assert trust._group_is_private(1000, 1000) is private


@pytest.mark.parametrize(
    ("members", "private"),
    [([], False), (["me"], True), (["root"], False)],
    ids=["no-one", "owner-member", "other-member"],
)
def test_a_group_is_private_only_when_its_owner_is_in_it(monkeypatch, members, private):
    accounts(monkeypatch, [ME, ("root", 0, 0)], {50: members})  # 50 is no one's primary group
    assert trust._group_is_private(50, 1000) is private


def test_an_empty_group_on_a_folder_of_root_is_shared(monkeypatch):
    # A root-owned folder of mode 775 in an empty group: a setgid program of that group could write it.
    accounts(monkeypatch, [("root", 0, 0), ME], {50: []})
    assert trust._group_is_private(50, 0) is False


def test_a_group_or_owner_that_cannot_be_looked_up_counts_as_shared(monkeypatch):
    accounts(monkeypatch, [ME], {1000: []})
    assert trust._group_is_private(4242, 1000) is False  # no such group
    assert trust._group_is_private(1000, 4242) is False  # no such owner


def test_the_folder_s_own_group_and_owner_are_looked_up(tmp_path, monkeypatch):
    (folder,) = folders(tmp_path, "home")
    path = make_file(folder)
    folder.chmod(0o775)
    gid, uid = folder.stat().st_gid, folder.stat().st_uid
    accounts(monkeypatch, [("me", uid, gid)], {gid: []})
    assert trusted(path)
    accounts(monkeypatch, [("me", uid, gid), ("other", uid + 1, gid)], {gid: []})
    untrusted(path, f"the folder {folder} on the path to the file {path} is writable by its group, which is not")


def _private_group(gid, uid):
    """Whether `uid` is in group `gid` and no other user is, as a member or by primary group (an independent
    reckoning)."""
    try:
        group, owner = grp.getgrgid(gid), pwd.getpwuid(uid)
    except KeyError:
        return False
    by_primary = {p.pw_uid for p in pwd.getpwall() if p.pw_gid == gid}
    owner_in = uid in by_primary or owner.pw_gid == gid or owner.pw_name in group.gr_mem
    return owner_in and by_primary <= {uid} and set(group.gr_mem) <= {owner.pw_name}


def test_the_real_lookup_of_my_own_primary_group():
    uid = os.geteuid()
    try:
        me = pwd.getpwuid(uid)
    except KeyError:  # a container user without an account: the owner cannot be looked up
        assert trust._group_is_private(os.getegid(), uid) is False
        return
    assert trust._group_is_private(me.pw_gid, uid) is _private_group(me.pw_gid, uid)
    if uid != 0:
        root_group = pwd.getpwuid(0).pw_gid  # root's primary group: root is in it, so it is not private to me
        assert trust._group_is_private(root_group, uid) is False


# --- the real interpreter -------------------------------------------------------------------------------------------


def _lookup_folders(path):
    """The folders the system looks an entry up in to reach `path`, and the links it follows (an independent
    reckoning, from os.path.realpath of every prefix)."""
    found_folders, links = [], []
    parts = [part for part in path.split("/") if part]
    for n, part in enumerate(parts):
        if part in (".", ".."):
            continue
        folder = os.path.realpath("/" + "/".join(parts[:n]))
        found_folders.append(folder)
        entry = os.path.join(folder, part)
        if os.path.islink(entry):
            links.append(entry)
            more_folders, more_links = _lookup_folders(os.path.join(folder, os.readlink(entry)))
            found_folders += more_folders
            links += more_links
    return found_folders, links


def _may_have_acl(path):
    try:
        os.getxattr(path, "system.posix_acl_access")
    except AttributeError:
        return True
    except OSError as exc:
        return exc.errno not in (errno.ENODATA, errno.ENOTSUP, errno.EOPNOTSUPP)
    return True


def _open_folders(path):
    """The folders and links on the way to `path` that someone else could change (an independent reckoning)."""
    lookup_folders, links = _lookup_folders(path)
    found = []
    for folder in dict.fromkeys(lookup_folders):
        info = os.stat(folder)
        group_open = info.st_mode & 0o020 and (not _private_group(info.st_gid, info.st_uid) or _may_have_acl(folder))
        open_mode = (info.st_mode & 0o002 or group_open) and not info.st_mode & stat.S_ISVTX
        if open_mode or info.st_uid not in (os.geteuid(), 0):
            found.append(folder)
    return found + [link for link in links if os.lstat(link).st_uid not in (os.geteuid(), 0)]


@pytest.mark.parametrize("resolve", [False, True], ids=["as-written", "resolved"])
def test_the_interpreter_running_the_tests_is_trusted(resolve):
    interpreter = os.path.realpath(sys.executable) if resolve else sys.executable
    open_folders = _open_folders(interpreter)
    if open_folders:
        pytest.skip(f"this machine keeps the interpreter below folders others can change: {open_folders}")
    assert check_owned_file(interpreter, "the program") is None
