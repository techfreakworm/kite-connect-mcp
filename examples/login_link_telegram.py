#!/usr/bin/env python3
"""Example login link command: send a Kite login link to a Telegram chat through the Bot API.

Point KITE_MCP_LOGIN_LINK_COMMAND at this file. The server runs it, with no arguments, at each reminder time when no
valid token is stored, after Kite rejected the stored token, and on login(deliver=true). Its input on stdin is one
JSON object and a newline:

    {"url": "https://kite.zerodha.com/connect/login?v=3&api_key=...", "expires_at": "2026-11-17T05:59:00+05:30",
     "reason": "scheduled"}

where "reason" is "scheduled", "token_rejected" or "requested". Exit 0 means the link was sent; anything else means
it was not. The server does not retry a failed link: its next scheduled time is the retry.

Settings come from the environment, so list them in KITE_MCP_LOGIN_LINK_ENV_PASS (the server passes the command
only PATH, HOME, LANG and the variables named there):

    TELEGRAM_BOT_TOKEN   the bot token from @BotFather (keep it in the server's settings file)
    TELEGRAM_CHAT_ID     the chat to write to: your own
    TELEGRAM_API_BASE    optional, default https://api.telegram.org

The link logs in to your Kite account, once, until it expires: whoever holds it can use it. Send it only to your own
chat. This program never prints it (the server sends its output nowhere, but you may run it by hand), and asks
Telegram not to fetch a preview of it.
"""

from __future__ import annotations

import datetime as dt
import http.client
import json
import os
import sys
import urllib.error
import urllib.request

NAME = "login_link_telegram"
IST = dt.timezone(dt.timedelta(hours=5, minutes=30), "IST")
FIRST_LINES = {
    "scheduled": "Time to log in to Kite.",
    "token_rejected": "Kite rejected the stored session: log in again.",
    "requested": "Here is the Kite login link you asked for.",
}


def fail(message: str) -> int:
    print(f"{NAME}: {message}", file=sys.stderr)
    return 1


def login_link(url: object) -> str | None:
    """The link, or None when `url` is not one: a string starting with https://, without whitespace or control
    characters."""
    if not isinstance(url, str) or not url.startswith("https://") or url == "https://":
        return None
    if not url.isprintable() or any(ch.isspace() for ch in url):
        return None
    return url


def until(expires_at: object) -> str | None:
    """HH:MM in IST of the link's expiry, or None when it does not parse."""
    if not isinstance(expires_at, str):
        return None
    try:
        moment = dt.datetime.fromisoformat(expires_at)
        if moment.tzinfo is None:  # the server gives an offset; a moment without one is taken as IST
            moment = moment.replace(tzinfo=IST)
        return moment.astimezone(IST).strftime("%H:%M")
    except (ValueError, OverflowError):
        return None


def message(url: str, data: dict) -> str:
    lines = [FIRST_LINES.get(str(data.get("reason")), "Kite login link.")]
    expiry = until(data.get("expires_at"))
    if expiry is not None:
        lines.append(f"It works once, until {expiry} IST.")
    lines.append(url)  # on a line of its own, so Telegram makes it a link to tap
    return "\n".join(lines)


def main() -> int:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat:
        return fail("set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID (and list them in KITE_MCP_LOGIN_LINK_ENV_PASS)")
    base = os.environ.get("TELEGRAM_API_BASE", "https://api.telegram.org").rstrip("/")
    if not base.startswith(("https://", "http://")):
        return fail("TELEGRAM_API_BASE must start with https:// or http://")
    try:
        data = json.load(sys.stdin.buffer)  # the input is UTF-8 whatever the locale (LANG) says
    except ValueError:  # not JSON, or not UTF-8
        data = None
    if not isinstance(data, dict) or (url := login_link(data.get("url"))) is None:
        return fail("the input holds no login link")
    body = json.dumps({"chat_id": chat, "text": message(url, data), "disable_web_page_preview": True}).encode()
    # The URL holds the bot token, and Python's HTTP client quotes the URL in some of its errors: print an
    # exception's type only, never its message, and let no exception out as a traceback. Every failure exits 1:
    # the server does not retry, its next scheduled time does.
    try:
        request = urllib.request.Request(  # noqa: S310 (an http(s) URL: the configured Bot API)
            f"{base}/bot{token}/sendMessage", data=body, method="POST", headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(request, timeout=15):  # noqa: S310 (as above)
            return 0
    except urllib.error.HTTPError as exc:
        return fail(f"Telegram answered HTTP {exc.code}")
    except (http.client.InvalidURL, ValueError) as exc:
        # no request can be made of these settings (a space in the token, say)
        return fail(f"check TELEGRAM_API_BASE and TELEGRAM_BOT_TOKEN ({type(exc).__name__})")
    except (urllib.error.URLError, http.client.HTTPException, OSError) as exc:
        return fail(f"could not reach Telegram ({type(exc).__name__})")
    except Exception as exc:
        return fail(f"could not send ({type(exc).__name__})")


if __name__ == "__main__":
    sys.exit(main())
