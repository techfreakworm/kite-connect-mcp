"""The example consumers follow the contract: the event on stdin, exit 0 done, 75 retry, other codes permanent."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from conftest import FIXED_NOW
from events_helpers import PY, filled_row
from kite_connect_mcp.events.consumers import ConsumersConfig
from kite_connect_mcp.events.model import OrderView, order_event, selftest_event

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"
EVENT = {
    **order_event(
        OrderView.from_row(filled_row()),
        change="filled",
        source="postback",
        received_at=FIXED_NOW,
        tag_labels={},
        previous_event_id="",
    ),
    "seq": 7,
}
BOT = "123456:ABC-placeholder"  # the shape of a bot's credential, a placeholder


def run(program: list[str], event: dict, env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        program,
        input=json.dumps(event).encode(),
        env={"PATH": os.environ["PATH"], **env},
        capture_output=True,
        timeout=30,
    )


class FakeTelegram(BaseHTTPRequestHandler):
    status = 200
    received: list[dict] = []

    def do_POST(self):
        length = int(self.headers.get("content-length", "0"))
        FakeTelegram.received.append({"path": self.path, "body": json.loads(self.rfile.read(length))})
        self.send_response(FakeTelegram.status)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok": true}')

    def log_message(self, *args):
        pass


@pytest.fixture
def telegram():
    FakeTelegram.received = []
    FakeTelegram.status = 200
    server = HTTPServer(("127.0.0.1", 0), FakeTelegram)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", FakeTelegram
    server.shutdown()
    server.server_close()


TG = [sys.executable, str(EXAMPLES / "consumers" / "telegram_notify.py")]
TG_ENV = {"TELEGRAM_BOT_TOKEN": BOT, "TELEGRAM_CHAT_ID": "123456789"}
WEBHOOK = ["sh", str(EXAMPLES / "consumers" / "webhook.sh")]


def test_telegram_sends_the_text(telegram):
    base, fake = telegram
    result = run(TG, EVENT, {**TG_ENV, "TELEGRAM_API_BASE": base})
    assert result.returncode == 0, result.stderr
    (sent,) = fake.received
    assert sent["path"] == f"/bot{BOT}/sendMessage"
    assert sent["body"] == {"chat_id": "123456789", "text": EVENT["text"], "disable_web_page_preview": True}


def test_telegram_marks_a_selftest(telegram):
    base, fake = telegram
    run(TG, selftest_event("selftest-0a1b2c3d4e5f", received_at=FIXED_NOW), {**TG_ENV, "TELEGRAM_API_BASE": base})
    assert fake.received[0]["body"]["text"].startswith("[test] ")


@pytest.mark.parametrize(("status", "code"), [(429, 75), (502, 75), (400, 1)])
def test_telegram_exit_codes(telegram, status, code):
    base, fake = telegram
    fake.status = status
    assert run(TG, EVENT, {**TG_ENV, "TELEGRAM_API_BASE": base}).returncode == code


def test_telegram_without_its_settings_is_a_permanent_failure():
    result = run(TG, EVENT, {})
    assert result.returncode == 1 and b"TELEGRAM_BOT_TOKEN" in result.stderr


def test_telegram_unreachable_is_temporary():
    assert run(TG, EVENT, {**TG_ENV, "TELEGRAM_API_BASE": "http://127.0.0.1:9"}).returncode == 75


def test_log_to_file_appends_once_per_event(tmp_path):
    log = tmp_path / "events.jsonl"
    program = [sys.executable, str(EXAMPLES / "consumers" / "log_to_file.py")]
    for _ in range(2):  # delivered twice (at least once): written once
        assert run(program, EVENT, {"EVENT_LOG_FILE": str(log)}).returncode == 0
    (line,) = log.read_text().splitlines()
    assert json.loads(line)["event_id"] == EVENT["event_id"]
    assert stat.S_IMODE(log.stat().st_mode) == 0o600  # the events are private, as the store is


@pytest.mark.skipif(shutil.which("curl") is None, reason="curl is not installed")
def test_webhook_posts_the_event(telegram):
    base, fake = telegram
    result = run(WEBHOOK, EVENT, {"WEBHOOK_URL": f"{base}/hook"})
    assert result.returncode == 0, result.stderr
    assert fake.received[0]["path"] == "/hook" and fake.received[0]["body"]["event_id"] == EVENT["event_id"]
    fake.status = 503
    assert run(WEBHOOK, EVENT, {"WEBHOOK_URL": f"{base}/hook"}).returncode == 75


def test_the_sample_consumers_file_is_valid(tmp_path):
    text = (EXAMPLES / "events.consumers.json").read_text()
    data = json.loads(text)
    for consumer in data["consumers"]:  # point the placeholders at real programs so the loader can check them
        consumer["command"][0] = PY
    path = tmp_path / "consumers.json"
    path.write_text(json.dumps(data))
    path.chmod(0o600)
    config = ConsumersConfig.load(path)
    assert {c.name for c in config.consumers} == {"telegram", "log"}
    assert "mytag" in config.tag_labels
