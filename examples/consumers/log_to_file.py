#!/usr/bin/env python3
"""Example consumer: append each event as one JSON line to a file (EVENT_LOG_FILE, default ~/kite-events.jsonl).

Deliveries are at least once, so it skips an event_id it has already written: a consumer must be idempotent.
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
    with os.fdopen(fd, "a+", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        handle.seek(0)
        seen = {json.loads(line).get("event_id") for line in handle if line.strip()}
        if event.get("event_id") not in seen:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
