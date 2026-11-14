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


def site_block(text: str) -> str:
    return text[text.index("kite.example.com {") :]


def test_caddy_caps_each_path_and_not_the_whole_site():
    site = site_block(CADDY.read_text())
    top_level = re.sub(r"handle @\w+ \{.*?\n\t\}", "", site, flags=re.S)
    assert "request_body" not in top_level  # no site-wide cap: it would override the per-path ones
    # 64KiB, not 64KB: Caddy reads KB as 1000 bytes, and the server's own cap is 64 KiB (65,536 bytes)
    assert re.search(r"handle @postback \{.*?max_size 64KiB\n", site, flags=re.S)
    assert re.search(r"handle @callback \{.*?max_size \d+KB", site, flags=re.S)
    assert re.search(r"handle @health \{.*?max_size \d+KB", site, flags=re.S)
    assert re.search(r"handle @mcp \{.*?max_size 1MB", site, flags=re.S)


@pytest.mark.skipif(shutil.which("caddy") is None, reason="caddy is not installed")
def test_caddy_accepts_the_sample():
    result = subprocess.run(
        ["caddy", "adapt", "--config", str(CADDY), "--adapter", "caddyfile"], capture_output=True, timeout=60
    )
    assert result.returncode == 0, result.stderr.decode()
    assert b'"max_size":65536' in result.stdout  # the postback cap is exactly the server's


def test_nginx_caps_each_location():
    text = NGINX.read_text()
    assert re.search(r"location = /postback \{.*?client_max_body_size 64k;", text, flags=re.S)
    assert re.search(r"location = /callback \{.*?client_max_body_size \d+k;", text, flags=re.S)


def test_the_systemd_drop_in_turns_events_on():
    text = DROPIN.read_text()
    for line in (
        "[Service]",
        "Environment=KITE_MCP_EVENTS=true",
        "Environment=KITE_MCP_POSTBACK_PATH=/postback",
        "Restart=always",
    ):
        assert line in text
