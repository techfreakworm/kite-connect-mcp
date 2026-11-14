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


# a 408 (timed out) or a 429 (rate limited) is worth retrying, as in webhook.sh; another 4xx is not
@pytest.mark.parametrize(("status", "code"), [(429, 75), (408, 75), (502, 75), (400, 1), (404, 1)])
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


# --- the example login link command (KITE_MCP_LOGIN_LINK_COMMAND) ---------------------------------------------------

LINK = "https://kite.zerodha.com/connect/login?v=3&api_key=testapikey0001&redirect_params=state%3DTestState0001"
LINK_INPUT = {"url": LINK, "expires_at": "2026-11-17T05:59:00+05:30", "reason": "scheduled"}
LINK_CMD = [sys.executable, str(EXAMPLES / "login_link_telegram.py")]


def test_the_link_command_sends_the_link_to_the_chat(telegram):
    base, fake = telegram
    result = run(LINK_CMD, LINK_INPUT, {**TG_ENV, "TELEGRAM_API_BASE": base})
    assert result.returncode == 0, result.stderr
    (sent,) = fake.received
    assert sent["path"] == f"/bot{BOT}/sendMessage" and sent["body"]["chat_id"] == "123456789"
    assert LINK in sent["body"]["text"] and "05:59" in sent["body"]["text"]
    assert sent["body"]["disable_web_page_preview"] is True  # Telegram must not fetch the one-time link
    assert LINK not in (result.stdout + result.stderr).decode()


def test_the_message_has_the_reason_the_expiry_and_the_link_on_its_own_line(telegram):
    base, fake = telegram
    assert run(LINK_CMD, LINK_INPUT, {**TG_ENV, "TELEGRAM_API_BASE": base}).returncode == 0
    assert fake.received[0]["body"]["text"].splitlines() == [
        "Time to log in to Kite.",
        "It works once, until 05:59 IST.",
        LINK,
    ]


def test_the_expiry_is_given_in_ist(telegram):
    base, fake = telegram
    expires_at = "2026-11-17T00:29:00+00:00"  # 05:59 IST
    assert (
        run(LINK_CMD, {**LINK_INPUT, "expires_at": expires_at}, {**TG_ENV, "TELEGRAM_API_BASE": base}).returncode == 0
    )
    assert "It works once, until 05:59 IST." in fake.received[0]["body"]["text"]


@pytest.mark.parametrize("expires_at", [None, "", "tomorrow", 5])
def test_an_expiry_that_does_not_parse_is_left_out(telegram, expires_at):
    base, fake = telegram
    link_input = {**LINK_INPUT, "expires_at": expires_at}
    assert run(LINK_CMD, link_input, {**TG_ENV, "TELEGRAM_API_BASE": base}).returncode == 0
    assert fake.received[0]["body"]["text"].splitlines() == ["Time to log in to Kite.", LINK]


@pytest.mark.parametrize(
    ("reason", "words"),
    [("scheduled", "log in to Kite"), ("token_rejected", "rejected"), ("requested", "you asked for")],
)
def test_the_message_says_why(telegram, reason, words):
    base, fake = telegram
    assert run(LINK_CMD, {**LINK_INPUT, "reason": reason}, {**TG_ENV, "TELEGRAM_API_BASE": base}).returncode == 0
    assert words in fake.received[0]["body"]["text"]


@pytest.mark.parametrize("reason", ["something_new", None])
def test_a_reason_it_does_not_know_still_sends_the_link(telegram, reason):
    base, fake = telegram
    link_input = {**LINK_INPUT, "reason": reason}
    assert run(LINK_CMD, link_input, {**TG_ENV, "TELEGRAM_API_BASE": base}).returncode == 0
    assert fake.received[0]["body"]["text"].splitlines()[0] == "Kite login link."


@pytest.mark.parametrize("status", [400, 429, 502])
def test_any_refusal_is_a_failure_without_retry(telegram, status):
    base, fake = telegram
    fake.status = status
    assert (
        run(LINK_CMD, LINK_INPUT, {**TG_ENV, "TELEGRAM_API_BASE": base}).returncode == 1
    )  # the next time is the retry


@pytest.mark.parametrize("bad", [{}, {"url": "ftp://x"}, {"url": "https://kite.example.com/x y"}])
def test_input_without_a_link_is_refused(telegram, bad):
    base, fake = telegram
    assert run(LINK_CMD, bad, {**TG_ENV, "TELEGRAM_API_BASE": base}).returncode == 1 and fake.received == []


@pytest.mark.parametrize(
    "payload",
    [
        b"not json\n",
        b"",
        b'["https://kite.example.com/x"]\n',  # not an object
        b'{"url": 5}\n',
        b'{"url": "https://kite.example.com/x\\ny"}\n',  # a line break in the link
        b'{"url": "https://"}\n',
    ],
)
def test_input_that_is_not_a_link_object_is_refused_in_one_line(telegram, payload):
    base, fake = telegram
    result = subprocess.run(
        LINK_CMD,
        input=payload,
        env={"PATH": os.environ["PATH"], **TG_ENV, "TELEGRAM_API_BASE": base},
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 1 and fake.received == []
    assert result.stderr == b"login_link_telegram: the input holds no login link\n"


@pytest.mark.parametrize(
    "env",
    [{}, {"TELEGRAM_BOT_TOKEN": BOT}, {"TELEGRAM_CHAT_ID": "123456789"}, {**TG_ENV, "TELEGRAM_CHAT_ID": " "}],
)
def test_the_link_command_without_its_settings_fails_naming_them(env):
    result = run(LINK_CMD, LINK_INPUT, env)
    assert result.returncode == 1
    assert b"TELEGRAM_BOT_TOKEN" in result.stderr and b"TELEGRAM_CHAT_ID" in result.stderr
    assert b"KITE_MCP_LOGIN_LINK_ENV_PASS" in result.stderr
    assert LINK.encode() not in result.stdout + result.stderr


def test_the_link_command_with_telegram_unreachable_fails():
    result = run(LINK_CMD, LINK_INPUT, {**TG_ENV, "TELEGRAM_API_BASE": "http://127.0.0.1:9"})
    assert result.returncode == 1  # no retry here: the next scheduled time is the retry
    printed = result.stdout + result.stderr
    assert LINK.encode() not in printed and b"127.0.0.1" not in printed and b"Traceback" not in printed
    assert result.stderr.startswith(b"login_link_telegram: ") and result.stderr.count(b"\n") == 1


@pytest.mark.parametrize(
    ("base", "bot"),
    [
        ("127.0.0.1", BOT),  # no scheme: refused before any request is made
        (None, "123456:ABC placeholder"),  # a space: http.client's error would quote the path, token and all
        (None, "123456:ABCé"),  # not ASCII: the encoder's error would quote the request line, token and all
    ],
)
def test_the_link_command_never_prints_the_token_or_the_link(telegram, base, bot):
    url, fake = telegram
    result = run(LINK_CMD, LINK_INPUT, {**TG_ENV, "TELEGRAM_BOT_TOKEN": bot, "TELEGRAM_API_BASE": base or url})
    assert result.returncode == 1
    printed = result.stdout + result.stderr
    assert bot.encode() not in printed and b"ABC" not in printed and b"Traceback" not in printed
    assert LINK.encode() not in printed and b"TestState0001" not in printed
    assert result.stderr.startswith(b"login_link_telegram: ") and result.stderr.count(b"\n") == 1
    assert fake.received == []


def test_the_link_command_is_one_executable_program():
    path = EXAMPLES / "login_link_telegram.py"
    assert os.access(path, os.X_OK) and path.read_text().startswith("#!/usr/bin/env python3\n")
