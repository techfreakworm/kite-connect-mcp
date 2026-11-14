#!/usr/bin/env python3
"""Example consumer: append each event as one JSON line to a file (EVENT_LOG_FILE, default ~/kite-events.jsonl).

Deliveries are at least once, so it skips an event_id it has already written: a consumer must be idempotent.
A line it cannot read (cut short by a crash, or edited by hand) is skipped and left as it is.
A new file is made readable by you only (mode 600), as the event store is: the events describe your orders."""

from __future__ import annotations

import fcntl
import json
import os
import sys
from pathlib import Path


def main() -> int:
    event = json.load(sys.stdin)
    path = Path(os.environ.get("EVENT_LOG_FILE") or Path.home() / "kite-events.jsonl").expanduser()
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
