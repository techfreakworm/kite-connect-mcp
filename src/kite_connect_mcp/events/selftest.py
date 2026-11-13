"""The selftest: a synthetic postback for a made-up order id, signed with the app's API secret, posted to a URL.

The service recognizes its own signed selftest (the `selftest-` order id with a valid checksum) and stores it as a
SelftestEvent, which only consumers that ask for selftests get. Posting it through the public URL proves the whole path:
the proxy, the checksum, the store, the router and a consumer. The command reads the secret from the configuration
itself, so whoever runs it never sees the secret."""

from __future__ import annotations

import json
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
    host = "127.0.0.1" if settings.host in ("0.0.0.0", "::", "") else settings.host  # noqa: S104
    if ":" in host:  # an IPv6 address goes in brackets in a URL
        host = f"[{host}]"
    return f"http://{host}:{settings.port}{settings.postback_path}"


async def send(url: str, payload: dict[str, Any], *, transport: httpx2.AsyncBaseTransport | None = None) -> int:
    """POST the payload to `url` and return the HTTP status. A URL that cannot be reached is a KiteMCPError that names
    it and what went wrong (no payload, no response body)."""
    try:
        async with httpx2.AsyncClient(transport=transport, timeout=15.0, follow_redirects=False) as client:
            response = await client.post(
                url, content=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}
            )
    except (httpx2.HTTPError, httpx2.InvalidURL) as exc:
        raise KiteMCPError(f"selftest: cannot post to {url}: {str(exc) or type(exc).__name__}") from None
    return response.status_code
