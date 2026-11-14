#!/usr/bin/env python3
"""Example consumer: append each event as one JSON line to a file (EVENT_LOG_FILE, default ~/kite-events.jsonl).

Deliveries are at least once, so it skips an event_id it has already written: a consumer must be idempotent.
A line it cannot read (cut short by a crash, or edited by hand) is skipped and left as it is.
A new file is made readable by you only (mode 600), as the event store is: the events describe your orders.
To find what it has written, it reads the whole file at every delivery, so each one takes longer as the file grows:
move the file aside now and then (an event delivered again just after that would be written again).

A full disk or an I/O error can be temporary, so the event is retried (exit 75); a file it may not open will not
change by itself (exit 1). Either way it prints the error's name only: Python's message would name the file."""

from __future__ import annotations

import errno
import fcntl
import json
import os
import sys
from pathlib import Path

RETRY = 75
TEMPORARY = {errno.ENOSPC, errno.EDQUOT, errno.EIO}  # a full disk, a full quota, an I/O error


def main() -> int:
    event = json.load(sys.stdin.buffer)  # the event is UTF-8 whatever the locale (LANG) says
    path = Path(os.environ.get("EVENT_LOG_FILE") or Path.home() / "kite-events.jsonl").expanduser()
    try:
        append_once(path, event)
    except OSError as exc:
        name = errno.errorcode.get(exc.errno or 0, type(exc).__name__)
        print(f"log_to_file: cannot write EVENT_LOG_FILE ({name})", file=sys.stderr)
        return RETRY if exc.errno in TEMPORARY else 1
    return 0


def append_once(path: Path, event: dict[str, object]) -> None:
    """Append the event to the file unless a line there already has its event_id."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(fd, "a+b") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        handle.seek(0)
        written = handle.read()
        seen: set[object] = set()
        for line in written.split(b"\n"):
            try:
                record = json.loads(line)
            except ValueError:  # cut short by a crash or edited by hand: skip it, or it would fail every delivery
                continue
            if isinstance(record, dict):
                seen.add(record.get("event_id"))
        if event.get("event_id") not in seen:
            start = b"\n" if written and not written.endswith(b"\n") else b""  # never append to a cut-short line
            handle.write(start + json.dumps(event, ensure_ascii=False).encode() + b"\n")


if __name__ == "__main__":
    sys.exit(main())
