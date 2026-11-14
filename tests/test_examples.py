"""The example consumers follow the contract: the event on stdin, exit 0 done, 75 retry, other codes permanent."""

from __future__ import annotations

import errno
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
RUPEE_TEXT = "BUY 25 ABCD (NSE) filled at \u20b91234.50 [my strategy]"  # not ASCII: a rupee sign


def run(program: list[str], event: dict, env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        program,
        input=(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n").encode(),  # as the router writes it
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
LOG = [sys.executable, str(EXAMPLES / "consumers" / "log_to_file.py")]
NEEDS_CURL = pytest.mark.skipif(shutil.which("curl") is None, reason="curl is not installed")
# Not UTF-8, as on a server whose LANG names another encoding (no such locale need be installed to run this)
LATIN_1_LOCALE = {"PYTHONIOENCODING": "latin-1"}


def test_telegram_sends_the_text(telegram):
    base, fake = telegram
    result = run(TG, EVENT, {**TG_ENV, "TELEGRAM_API_BASE": base})
    assert result.returncode == 0, result.stderr
    (sent,) = fake.received
    assert sent["path"] == f"/bot{BOT}/sendMessage"
    assert sent["body"] == {"chat_id": "123456789", "text": EVENT["text"], "disable_web_page_preview": True}


def test_telegram_marks_a_selftest(telegram):
    base, fake = telegram
    selftest = selftest_event("selftest-0a1b2c3d4e5f", received_at=FIXED_NOW)
    result = run(TG, selftest, {**TG_ENV, "TELEGRAM_API_BASE": base})
    assert result.returncode == 0, result.stderr
    assert fake.received[0]["body"]["text"].startswith("[test] ")


def test_telegram_reads_the_event_as_utf8_whatever_the_locale(telegram):
    base, fake = telegram
    result = run(TG, {**EVENT, "text": RUPEE_TEXT}, {**TG_ENV, **LATIN_1_LOCALE, "TELEGRAM_API_BASE": base})
    assert result.returncode == 0, result.stderr
    assert fake.received[0]["body"]["text"] == RUPEE_TEXT


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


@pytest.mark.parametrize(
    ("base", "bot"),
    [
        ("127.0.0.1", BOT),  # no scheme: refused before any request is made
        (None, "123456:ABC placeholder"),  # a space: http.client's error would quote the path, token and all
        (None, "123456:ABC\u00e9"),  # not ASCII: the encoder's error would quote the request line, token and all
    ],
)
def test_telegram_never_prints_the_token(telegram, base, bot):
    url, fake = telegram
    result = run(TG, EVENT, {**TG_ENV, "TELEGRAM_BOT_TOKEN": bot, "TELEGRAM_API_BASE": base or url})
    assert result.returncode == 1  # a setting that cannot work: no retry
    printed = result.stdout + result.stderr
    assert bot.encode() not in printed and b"ABC" not in printed and b"Traceback" not in printed
    assert result.stderr.startswith(b"telegram_notify: ") and result.stderr.count(b"\n") == 1
    assert fake.received == []


def test_log_to_file_appends_once_per_event(tmp_path):
    log = tmp_path / "events.jsonl"
    for _ in range(2):  # delivered twice (at least once): written once
        assert run(LOG, EVENT, {"EVENT_LOG_FILE": str(log)}).returncode == 0
    (line,) = log.read_text().splitlines()
    assert json.loads(line)["event_id"] == EVENT["event_id"]
    assert stat.S_IMODE(log.stat().st_mode) == 0o600  # the events are private, as the store is


def test_log_to_file_skips_a_corrupt_line(tmp_path):
    log = tmp_path / "events.jsonl"
    # a line edited by hand, then one cut short by a crash: mid-character and without its newline
    log.write_bytes(b'not json\n{"event_id": "cut-short", "text": "\xe2\x82')
    for _ in range(2):  # neither line fails the delivery, and the event is still written once
        result = run(LOG, EVENT, {"EVENT_LOG_FILE": str(log)})
        assert result.returncode == 0, result.stderr
    lines = log.read_bytes().split(b"\n")
    assert lines[:2] == [b"not json", b'{"event_id": "cut-short", "text": "\xe2\x82']  # left as they were
    assert [json.loads(line)["event_id"] for line in lines[2:] if line] == [EVENT["event_id"]]  # on a line of its own


def test_log_to_file_reads_the_event_as_utf8_whatever_the_locale(tmp_path):
    log = tmp_path / "events.jsonl"
    result = run(LOG, {**EVENT, "text": RUPEE_TEXT}, {**LATIN_1_LOCALE, "EVENT_LOG_FILE": str(log)})
    assert result.returncode == 0, result.stderr
    assert json.loads(log.read_bytes())["text"] == RUPEE_TEXT


# Runs log_to_file.py with os.open failing as on a full disk, an I/O error or a file it may not open
FAILING_OPEN = """
import errno, os, runpy, sys
def failing_open(path, *args, **kwargs):
    raise OSError(getattr(errno, os.environ["FAIL_WITH"]), "simulated", path)
os.open = failing_open
runpy.run_path(sys.argv[1], run_name="__main__")
"""


@pytest.mark.parametrize(
    ("failure", "code"), [("ENOSPC", 75), ("EDQUOT", 75), ("EIO", 75), ("EACCES", 1), ("EROFS", 1)]
)
def test_log_to_file_cannot_write(tmp_path, failure, code):
    # a full disk or an I/O error can be temporary, so the delivery is retried; a file it may not open will not change
    log = tmp_path / "private-folder" / "events.jsonl"
    program = [sys.executable, "-c", FAILING_OPEN, LOG[1]]
    result = run(program, EVENT, {"EVENT_LOG_FILE": str(log), "FAIL_WITH": failure})
    assert result.returncode == code
    assert result.stderr == f"log_to_file: cannot write EVENT_LOG_FILE ({failure})\n".encode()  # no path, no traceback


def test_log_to_file_given_a_folder_is_a_permanent_failure(tmp_path):
    result = run(LOG, EVENT, {"EVENT_LOG_FILE": str(tmp_path)})
    assert result.returncode == 1
    assert result.stderr == f"log_to_file: cannot write EVENT_LOG_FILE ({errno.errorcode[errno.EISDIR]})\n".encode()


@NEEDS_CURL
def test_webhook_posts_the_event(telegram):
    base, fake = telegram
    result = run(WEBHOOK, EVENT, {"WEBHOOK_URL": f"{base}/hook"})
    assert result.returncode == 0, result.stderr
    assert fake.received[0]["path"] == "/hook" and fake.received[0]["body"]["event_id"] == EVENT["event_id"]


# a 408 (timed out) or a 429 (rate limited, as in a burst of fills) is worth retrying; another 4xx is not
@NEEDS_CURL
@pytest.mark.parametrize(("status", "code"), [(429, 75), (408, 75), (503, 75), (400, 1), (404, 1)])
def test_webhook_exit_codes(telegram, status, code):
    base, fake = telegram
    fake.status = status
    result = run(WEBHOOK, EVENT, {"WEBHOOK_URL": f"{base}/hook"})
    assert result.returncode == code
    assert result.stderr == f"webhook.sh: HTTP {status}\n".encode()


@NEEDS_CURL
def test_webhook_ignores_a_curlrc(telegram, tmp_path):
    # consumers get HOME, and curl reads ~/.curlrc: "verbose" would print the URL, "fail" would change the exit codes
    base, fake = telegram
    (tmp_path / ".curlrc").write_text("verbose\nfail\n")
    fake.status = 400
    result = run(WEBHOOK, EVENT, {"HOME": str(tmp_path), "WEBHOOK_URL": f"{base}/hook/ABCD-placeholder"})
    assert result.returncode == 1
    assert result.stdout == b"" and result.stderr == b"webhook.sh: HTTP 400\n"


@NEEDS_CURL
@pytest.mark.parametrize(
    "url",
    [
        "htp://127.0.0.1/hook/ABCD-placeholder",  # a scheme curl does not know (curl exit 1)
        "http://127.0.0.1:99999/hook/ABCD-placeholder",  # a malformed URL (curl exit 3)
    ],
)
def test_webhook_with_a_bad_url_is_a_permanent_failure(url):
    result = run(WEBHOOK, EVENT, {"WEBHOOK_URL": url})
    assert result.returncode == 1  # the setting will not fix itself: no retry
    printed = result.stdout + result.stderr
    assert b"/hook" not in printed and b"ABCD-placeholder" not in printed
    assert result.stderr.startswith(b"webhook.sh: check WEBHOOK_URL") and result.stderr.count(b"\n") == 1


def test_webhook_without_curl_is_a_permanent_failure(tmp_path):
    shell = shutil.which("sh")
    assert shell is not None
    result = run([shell, WEBHOOK[1]], EVENT, {"PATH": str(tmp_path), "WEBHOOK_URL": "http://127.0.0.1:9/hook"})
    assert result.returncode == 1  # as in the Docker image: retrying will not install it
    assert result.stderr == b"webhook.sh: curl is not installed\n"


@NEEDS_CURL
def test_webhook_never_prints_the_url_or_the_event():
    # a webhook URL often is the secret, and curl's own messages name parts of it (its host), so none are printed
    result = run(WEBHOOK, EVENT, {"WEBHOOK_URL": "http://127.0.0.1:9/hook/ABCD-placeholder"})
    assert result.returncode == 75  # no answer: retry
    printed = result.stdout + result.stderr
    for part in (b"127.0.0.1", b"/hook", b"ABCD-placeholder", EVENT["event_id"].encode()):
        assert part not in printed
    assert result.stderr.startswith(b"webhook.sh: ") and result.stderr.count(b"\n") == 1


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
