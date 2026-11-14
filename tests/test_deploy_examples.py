"""The deploy samples: per-path body caps, the postback route, and a systemd drop-in that turns events on."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
CADDY = DEPLOY / "caddy" / "Caddyfile.example"
NGINX = DEPLOY / "nginx" / "kite-connect-mcp.conf.example"
DROPIN = DEPLOY / "systemd" / "kite-connect-mcp.service.d" / "events.conf"

# 64KiB, not 64KB: Caddy reads KB as 1000 bytes, and the server's own cap is 64 KiB (65,536 bytes)
CADDY_CAPS = {"mcp": "1MB", "callback": "4KB", "postback": "64KiB", "health": "4KB"}
NGINX_CAPS = {"/callback": "4k", "/postback": "64k", "/health": "4k"}
CAP = {CADDY: re.compile(r"\bmax_size (\S+)"), NGINX: re.compile(r"\bclient_max_body_size (\S+);")}


def code(path: Path) -> str:
    """The sample without its comments: a comment may name a directive or hold a brace."""
    return re.sub(r"#[^\n]*", "", path.read_text())


def block(text: str, header: str) -> str:
    """What is inside the block that opens with `header` (ending in "{"), up to its matching "}"."""
    start = text.index(header) + len(header)
    depth = 1
    for i in range(start, len(text)):
        depth += {"{": 1, "}": -1}.get(text[i], 0)
        if depth == 0:
            return text[start:i]
    raise AssertionError(f"{header!r} is never closed")


def test_caddy_caps_each_path_and_not_the_whole_site():
    site = block(code(CADDY), "kite.example.com {")
    top_level = site
    for name, cap in CADDY_CAPS.items():
        handle = block(site, f"handle @{name} {{")
        assert CAP[CADDY].findall(handle) == [cap], name  # in its own block, not the next one's
        assert re.search(r"request_body \{\s*max_size \S+\s*\}", handle), name
        top_level = top_level.replace(handle, "")
    assert "request_body" not in top_level  # no site-wide cap: it would override the per-path ones


@pytest.mark.skipif(shutil.which("caddy") is None, reason="caddy is not installed")
def test_caddy_accepts_the_sample():
    result = subprocess.run(
        ["caddy", "adapt", "--config", str(CADDY), "--adapter", "caddyfile"], capture_output=True, timeout=60
    )
    assert result.returncode == 0, result.stderr.decode()
    assert b'"max_size":65536' in result.stdout  # the postback cap is exactly the server's


def test_nginx_caps_each_location():
    text = code(NGINX)
    server = block(text[text.rindex("server {") :], "server {")
    assert "listen 443 ssl;" in server
    rest = server
    for path, cap in NGINX_CAPS.items():
        location = block(server, f"location = {path} {{")
        assert CAP[NGINX].findall(location) == [cap], path  # in its own block, not the next one's
        rest = rest.replace(location, "")
    assert CAP[NGINX].findall(rest) == ["1m"]  # the server's own, for /mcp


def test_the_systemd_drop_in_turns_events_on():
    settings = [line for line in DROPIN.read_text().splitlines() if line and not line.startswith("#")]
    # only what needs the service: the store and the consumers file go in the settings file, which the commands read
    assert settings == [
        "[Service]",
        "Environment=KITE_MCP_EVENTS=true",
        "Environment=KITE_MCP_POSTBACK_PATH=/postback",
        "Restart=always",
    ]


def test_the_drop_in_says_where_the_rest_goes_and_how_to_check_it():
    text = DROPIN.read_text()
    for line in (
        "#   KITE_MCP_EVENTS_DB=/var/lib/kite-connect-mcp/events.sqlite3",
        "#   KITE_MCP_EVENTS_CONSUMERS_FILE=/etc/kite-connect-mcp/consumers.json",
        "#   kite-mcp-ctl events status",
        "#   kite-mcp-ctl events selftest --url https://kite.example.com/postback",
        "# LogsDirectory=kite-events",
        "# LogsDirectoryMode=0700",
    ):
        assert line in text
    assert "ReadWritePaths=" not in text  # LogsDirectory= makes the folder, owned by the service user, and writable


def test_the_unit_shows_the_login_settings_commented_out():
    text = (DEPLOY / "systemd" / "kite-connect-mcp.service").read_text()
    for name in (
        "KITE_MCP_ALLOWED_USER_ID_SHA256", "KITE_MCP_ALLOW_LOGOUT", "KITE_MCP_LOGIN_ALLOW_FORCE",
        "KITE_MCP_LOGIN_REMINDER_TIMES", "KITE_MCP_LOGIN_LINK_COMMAND", "KITE_MCP_LOGIN_LINK_ENV_PASS",
    ):  # fmt: skip
        assert re.search(rf"^#\s+{name}=", text, flags=re.M), name
    assert not re.search(r"^Environment=KITE_MCP_LOGIN", text, flags=re.M)  # settings file only: the commands read it
