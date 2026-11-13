"""Whether to trust a file the service reads or runs: only this user or root may be able to change it, or change where
its path leads.

The event router runs the commands that the consumers file names, as the service's user, who can read the Kite
token. Whoever could change that file, or a program it names, or a folder or link on the way to either, could run code
as that user. So, like OpenSSH's StrictModes, the file and each program must be regular files that only this user or
root can change, reached through folders and links only they can change: see check_owned_file. A sticky folder such
as /tmp is fine, and so is group write by a group of the owner's own when no ACL is set.
"""

from __future__ import annotations

import errno
import grp
import os
import pwd
import stat
from collections.abc import Sequence
from pathlib import Path

from ..errors import KiteMCPError

MAX_LINKS = 40  # links followed on one path, as Linux allows (MAXSYMLINKS); one more is ELOOP

_ACL_ATTRIBUTE = "system.posix_acl_access"  # where Linux keeps a POSIX access ACL
_NO_ATTRIBUTE_ERRNOS = frozenset({errno.ENODATA, errno.ENOTSUP, errno.EOPNOTSUPP})  # no ACL, or none possible here


class UntrustedFileError(KiteMCPError):
    """Why a file cannot be trusted, or cannot be read once trusted (see check_owned_file and read_owned_file)."""


def check_owned_file(path: str | os.PathLike[str], what: str) -> str | None:
    """The reason not to trust the file at `path` (named `what` in the reason), or None when only this user or root
    can change it or change where `path` leads. Nothing is opened, so a FIFO cannot make the caller wait.

    The path is followed one entry at a time, as the system follows it, from the root (or the current folder):
    - every folder an entry is looked up in, including the folders a link leads through, must be owned by this user
      or root and writable by no one else, unless it is sticky (like /tmp, where others cannot replace an entry they
      do not own);
    - every link followed must be owned by this user or root: in a sticky folder its owner may replace it;
    - at most MAX_LINKS links are followed, and `..` goes up from where the links before it led, not from the path
      as written;
    - a name, `.` or `..` after something that is not a folder is refused, as the system refuses it (ENOTDIR), and
      so is a `/` at the end of the path or of a link's target that leads to a file;
    - the file must be a regular file, owned by this user or root and writable by no one else.

    Group write on the file or a folder is accepted when that one's group is private to its owner and it has no POSIX
    ACL, as Debian's OpenSSH relaxes StrictModes: Ubuntu and Debian give each user a group of their own, and their
    umask 002 makes the user's folders group-writable (mode 775). See _group_is_private and _has_access_acl. Write by
    others is refused, unless the folder is sticky.

    NFSv4 ACLs and macOS ACLs are not checked: they can let others write whatever the mode bits say, but only the
    owner of the file or folder, or root, can add one."""
    try:
        _resolve_owned_file(path, what)
    except UntrustedFileError as exc:
        return str(exc)
    return None


def read_owned_file(path: str | os.PathLike[str], what: str) -> bytes:
    """The bytes of the file at `path` (named `what` in an error) when check_owned_file trusts it, else
    UntrustedFileError with the reason. The real file that was checked is read: where the system leads `path`, which
    only this user or root can change."""
    real = _resolve_owned_file(path, what)
    try:
        return Path(real).read_bytes()
    except OSError as exc:
        raise UntrustedFileError(f"cannot read {what} {os.fspath(path)}: {_reason(exc)}") from None


def _resolve_owned_file(path: str | os.PathLike[str], what: str) -> str:
    """The real path of the file at `path` when check_owned_file trusts it, else UntrustedFileError with the reason."""
    given = os.fspath(path)
    if not os.path.isabs(given):  # not os.path.abspath: it takes `..` before the links in front of it are followed
        try:
            given = os.path.join(os.getcwd(), given)
        except OSError as exc:
            raise UntrustedFileError(f"cannot read {what} {given}: {_reason(exc)}") from None
    name = f"{what} {given}"
    todo = _steps(given)  # a stack: the next entry to look up is last
    current = "/"  # the real folder (or at the end the file) reached so far: it holds no link
    is_folder = True  # whether `current` is a folder
    links = 0
    trusted: set[str] = set()
    while todo:
        part = todo.pop()
        if not is_folder:  # a name, `.` or `..` after a file: the system refuses them all
            raise UntrustedFileError(f"cannot read {name}: {os.strerror(errno.ENOTDIR)}")
        if part == ".":
            continue
        if part == "..":
            current = os.path.dirname(current)  # only who can change the folder above `current` could move it
            continue
        if current not in trusted:
            _check_folder(current, name)
            trusted.add(current)
        entry = os.path.join(current, part)
        info = _lstat(entry, f"cannot read {name}")
        if not stat.S_ISLNK(info.st_mode):
            current, is_folder = entry, stat.S_ISDIR(info.st_mode)
            continue
        links += 1
        if links > MAX_LINKS:
            raise UntrustedFileError(f"cannot read {name}: {os.strerror(errno.ELOOP)}")
        _raise(_owner_problem(info, f"the link {entry} on the path to {name}"))
        try:
            target = os.readlink(entry)
        except OSError as exc:
            raise UntrustedFileError(f"cannot read {name}: {_reason(exc)}") from None
        if target.startswith("/"):
            current = "/"
        todo.extend(_steps(target))  # `current` is still a folder: the link's own, or the root
    info = _lstat(current, f"cannot read {name}")
    if not stat.S_ISREG(info.st_mode):
        raise UntrustedFileError(f"{name} is not a regular file")
    _raise(_owner_problem(info, name) or _write_problem(info, name, current))
    return current


def _steps(path: str) -> list[str]:
    """The entries of `path` to look up, as a stack (the first one last). A `/` at the end asks for a folder, as a
    final `.` does, so it becomes one: after a file the system refuses either (ENOTDIR)."""
    steps = [part for part in path.split("/") if part]
    if path.endswith("/"):
        steps.append(".")
    steps.reverse()
    return steps


def _check_folder(folder: str, name: str) -> None:
    """Refuse `folder`, where an entry on the path to `name` is looked up, unless only this user or root can change
    what it holds."""
    info = _lstat(folder, f"cannot check the folder {folder} on the path to {name}")
    if not stat.S_ISDIR(info.st_mode):
        raise UntrustedFileError(f"cannot read {name}: {os.strerror(errno.ENOTDIR)}")
    described = f"the folder {folder} on the path to {name}"
    _raise(_owner_problem(info, described) or _write_problem(info, described, folder))


def _lstat(path: str, failure: str) -> os.stat_result:
    try:
        return os.lstat(path)
    except OSError as exc:
        raise UntrustedFileError(f"{failure}: {_reason(exc)}") from None


def _raise(problem: str | None) -> None:
    if problem is not None:
        raise UntrustedFileError(problem)


def _owner_problem(info: os.stat_result, name: str) -> str | None:
    if info.st_uid in (os.geteuid(), 0):
        return None
    return f"{name} is owned by uid {info.st_uid}, not by this user or root"


def _write_problem(info: os.stat_result, name: str, path: str) -> str | None:
    """Why someone other than its owner could change `path` (described as `name`) through its mode, or None."""
    mode = stat.S_IMODE(info.st_mode)
    if stat.S_ISDIR(info.st_mode) and mode & stat.S_ISVTX:
        return None  # a sticky folder: others may add files, not replace one they do not own
    if mode & 0o002:
        return f"{name} is writable by group or others (mode {mode:o}); run chmod go-w {path}"
    if mode & 0o020:
        if not _group_is_private(info.st_gid, info.st_uid):
            return (
                f"{name} is writable by its group, which is not private to its owner (mode {mode:o}); "
                f"run chmod g-w {path}"
            )
        if _has_access_acl(path):
            return (
                f"{name} has an access ACL, so the users and groups it names may write it (mode {mode:o}); "
                f"run chmod g-w {path}"
            )
    return None


def _group_is_private(gid: int, owner_uid: int) -> bool:
    """Whether group `gid` is private to user `owner_uid`, as Ubuntu's and Debian's user-private groups are: the
    owner is in it (as primary group or as its member), its members are none or only the owner's name, and no user of
    another uid has it as primary group. A group no user is in is shared, as Debian's OpenSSH holds: a setgid program
    of that group could write. A group or owner that cannot be looked up counts as shared.

    Not seen, a limit Debian's OpenSSH has too: users the system does not list (a directory service set not to
    enumerate them), and the members of a second /etc/group line with the same gid (getgrgid reads the first)."""
    try:
        members = grp.getgrgid(gid).gr_mem
        owner = pwd.getpwuid(owner_uid)
    except KeyError:
        return False
    if any(member != owner.pw_name for member in members):
        return False
    owner_in_group = owner.pw_gid == gid or owner.pw_name in members
    for user in pwd.getpwall():
        if user.pw_gid == gid:
            if user.pw_uid != owner_uid:
                return False
            owner_in_group = True  # another name for the owner's uid
    return owner_in_group


def _has_access_acl(path: str) -> bool:
    """Whether `path` (not a link) has a POSIX access ACL. Its group mode bits are then the ACL's mask, the most that
    any user or group the ACL names may do, so group write no longer means the group alone. Any doubt counts as an
    ACL: an error other than "no such attribute" or "not supported here", or a system without getxattr (macOS)."""
    getxattr = getattr(os, "getxattr", None)
    if getxattr is None:
        return True
    try:
        getxattr(path, _ACL_ATTRIBUTE, follow_symlinks=False)
    except OSError as exc:
        return exc.errno not in _NO_ATTRIBUTE_ERRNOS
    return True


def _reason(exc: OSError) -> str:
    return exc.strerror or type(exc).__name__


__all__: Sequence[str] = ("MAX_LINKS", "UntrustedFileError", "check_owned_file", "read_owned_file")
