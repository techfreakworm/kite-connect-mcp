"""The selftest: a synthetic postback for a made-up order id, signed with the app's API secret, posted to a URL.

The service recognizes its own signed selftest (the `selftest-` order id with a valid checksum) and stores it as a
SelftestEvent, which only consumers that ask for selftests get. A 200 through the public URL proves the path up to the
store: the proxy, the route, the checksum and the store's write. The command checks only that answer; whether a consumer
that asks for selftests got it is for the operator to see (`events tail`, `events status`, the consumer's own output).

A server without the event service answers a selftest 404 (postback.py), so the command fails there instead of
reporting a test of nothing; without --url it refuses to run when this configuration has the service off. The command
reads the secret from the configuration itself, so whoever runs it never sees the secret, and it prints a URL without
the user name and password it may hold."""

from __future__ import annotations

import json
import re
import secrets
from typing import Any

import httpx2

from ..config import Settings
from ..errors import ConfigError, KiteMCPError
from ..postback import compute_checksum
from ..timeutil import now_ist
from .model import SELFTEST_PREFIX


def build_payload(api_secret: str) -> dict[str, Any]:
    order_id = SELFTEST_PREFIX + secrets.token_hex(6)
    stamp = now_ist().strftime("%Y-%m-%d %H:%M:%S")
    return {
        "order_id": order_id,
        "order_timestamp": stamp,
        "status": "COMPLETE",
        "checksum": compute_checksum(order_id, stamp, api_secret),
    }


def default_url(settings: Settings) -> str:
    """This server's own postback route, on loopback when it listens on every address."""
    if not settings.postback_path:
        raise ConfigError("KITE_MCP_POSTBACK_PATH is not set: give the postback URL with --url")
    host = "127.0.0.1" if settings.host in ("0.0.0.0", "::", "[::]", "") else settings.host  # noqa: S104
    if ":" in host and not host.startswith("["):  # an IPv6 address goes in brackets in a URL (once)
        host = f"[{host}]"
    return f"http://{host}:{settings.port}{settings.postback_path}"


# the user name and password of a URL: what comes before the first "@" ahead of any "/", "?" or "#", after the scheme
_USERINFO = re.compile(r"^(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*://)?(?P<userinfo>[^/?#@]*)@")


def shown_url(url: str) -> str:
    """`url` fit to print: a user name and password in it become [redacted]."""
    return _USERINFO.sub(lambda m: f"{m['scheme'] or ''}[redacted]@", url, count=1)


async def send(url: str, payload: dict[str, Any], *, transport: httpx2.AsyncBaseTransport | None = None) -> int:
    """POST the payload to `url` and return the HTTP status. A URL that cannot be reached is a KiteMCPError that names
    it (shown_url) and what went wrong (no payload, no response body)."""
    try:
        async with httpx2.AsyncClient(transport=transport, timeout=15.0, follow_redirects=False) as client:
            response = await client.post(
                url, content=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}
            )
    except (httpx2.HTTPError, httpx2.InvalidURL) as exc:
        detail = str(exc) or type(exc).__name__
        if (found := _USERINFO.match(url)) and found["userinfo"]:
            detail = detail.replace(found["userinfo"], "[redacted]")
        raise KiteMCPError(f"selftest: cannot post to {shown_url(url)}: {detail}") from None
    return response.status_code


_HINTS = {
    403: "the server refused the checksum: does it have the same KITE_API_SECRET?",
    404: "no postback route at this URL, or the event service is off there",
    503: "the server's event store cannot write (see its log)",
}


def explain(status: int) -> str:
    """The line's tail for an answer other than 200: what it most likely means."""
    if status == 200:
        return ""
    if 300 <= status < 400:
        return " (expected 200: a redirect; give the final URL with --url)"
    hint = _HINTS.get(status)
    return f" (expected 200: {hint})" if hint else " (expected 200)"
