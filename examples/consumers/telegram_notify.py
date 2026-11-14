#!/usr/bin/env python3
"""Example consumer: send each event's text to a Telegram chat through the Bot API.

The consumer contract (docs/EVENTS.md): one event as JSON on stdin; exit 0 when handled, 75 when it should be retried,
anything else for a permanent failure. Settings come from the environment, so list them in the consumer's `env_pass`:

    TELEGRAM_BOT_TOKEN   the bot token from @BotFather (keep it out of the consumers file)
    TELEGRAM_CHAT_ID     the chat to write to
    TELEGRAM_API_BASE    optional, default https://api.telegram.org

Only the event's `text` is sent: it never holds an order id, an account or a link. A selftest is marked "[test]".
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

RETRY = 75


def main() -> int:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat:
        print(
            "telegram_notify: set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID (and list them in env_pass)", file=sys.stderr
        )
        return 1
    event = json.load(sys.stdin)
    text = str(event.get("text") or "")
    if not text:
        return 0  # nothing to say
    if event.get("kind") == "selftest":
        text = "[test] " + text
    base = os.environ.get("TELEGRAM_API_BASE", "https://api.telegram.org").rstrip("/")
    body = json.dumps({"chat_id": chat, "text": text, "disable_web_page_preview": True}).encode()
    request = urllib.request.Request(  # noqa: S310 (the URL is the configured Bot API)
        f"{base}/bot{token}/sendMessage", data=body, method="POST", headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=15):  # noqa: S310 (as above)
            return 0
    except urllib.error.HTTPError as exc:
        # 429 (rate limited) and 5xx are worth retrying; any other refusal will not change by itself
        print(f"telegram_notify: Telegram answered HTTP {exc.code}", file=sys.stderr)
        return RETRY if exc.code == 429 or exc.code >= 500 else 1
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        print(f"telegram_notify: could not reach Telegram ({type(exc).__name__})", file=sys.stderr)
        return RETRY


if __name__ == "__main__":
    sys.exit(main())
