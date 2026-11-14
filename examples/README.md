# Example event consumers, and a login link command

With `KITE_MCP_EVENTS=true`, the server records changes to your orders and hands each event to the consumers you list
in the consumers file (`KITE_MCP_EVENTS_CONSUMERS_FILE`). A consumer is any program: it reads one event on stdin and
says by its exit code whether it is done. These examples are starting points; copy one and change it to suit you.
[A login link command](#a-login-link-command), for the scheduled login links, is at the end.

| File | What it does | Settings (environment) |
|---|---|---|
| [consumers/telegram_notify.py](consumers/telegram_notify.py) | Sends the event's `text` to a Telegram chat through the Bot API; a selftest is marked `[test]` | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, optional `TELEGRAM_API_BASE` |
| [consumers/log_to_file.py](consumers/log_to_file.py) | Appends each event as one JSON line to a file, once per `event_id`; a new file gets mode 600 | optional `EVENT_LOG_FILE` (default `~/kite-events.jsonl`) |
| [consumers/webhook.sh](consumers/webhook.sh) | POSTs each event as JSON to a URL, with curl | `WEBHOOK_URL` |
| [events.consumers.json](events.consumers.json) | A sample consumers file: the Telegram and log consumers, and a label for the tag `mytag` | |

The Telegram example sends only the event's `text`, which never holds an order id, an account or a link. The other two
pass on the whole event, order id included. The log example reads its whole file at every delivery to skip the events
it has written, so each delivery takes longer as the file grows: move the file aside now and then.

## The contract in short

- The event arrives on stdin as one JSON object, then a newline.
- The environment holds `PATH`, `HOME`, `LANG`, `KITE_EVENT_CONSUMER` (the consumer's name) and the variables named in
  its `env_pass`; never the server's `KITE_*` settings.
- Exit 0 when the event is handled, 75 when it should be retried later; any other exit is a permanent failure.
- Deliveries are at least once, so the same event can arrive twice: be idempotent on `event_id`.
- Whatever the program prints to stderr ends up, cleaned and clipped, in the server's log, in `events status` and in
  the other consumers' `delivery_failed` events: never print a secret there. An HTTP client's error can quote the
  URL, token and all, so catch its errors and print a message of your own, as the examples do.

The whole contract, with the retry schedule and the timeouts, is in [docs/EVENTS.md](../docs/EVENTS.md).

## Installing one

1. Copy the program somewhere only you (or root) can change, for example `/opt/kite-connect-mcp/examples/consumers/`,
   and make it executable: `chmod 755 telegram_notify.py`. The server refuses a consumers file or a program that
   other users could change, and the folders on the way to them too.
2. Add the consumer to your consumers file; [events.consumers.json](events.consumers.json) shows the format. The
   `command` is a list of arguments, not a shell line, and its first item is an absolute path. Other users can see a
   command's arguments, so never put a secret there: put it in the server's environment (its settings file works) and
   name it in `env_pass`.
3. Make the consumers file private to the server's user (owned by it, `chmod 600`) and point
   `KITE_MCP_EVENTS_CONSUMERS_FILE` at it.
4. Restart the server: it reads the consumers file when it starts. Then send a selftest with
   `kite-connect-mcp events selftest` and check `kite-connect-mcp events status`. On a systemd server set up as in
   [SELF_HOSTING.md](../docs/SELF_HOSTING.md#order-events-on-a-server):
   `kite-mcp-ctl events selftest --url https://kite.example.com/postback`, then `kite-mcp-ctl events status`.

A script given as an argument, as in `["/usr/bin/python3", "/path/to/telegram_notify.py"]`, is not checked by the
server: keep it owned by you or root and writable by no one else. In the Docker image, Python is
`/usr/local/bin/python3` and curl is not installed.

## Trying one by hand

Feed a consumer the newest stored event and look at its exit code. Run it with the server's settings, or name the
store with `KITE_MCP_EVENTS_DB`:

```bash
kite-connect-mcp events tail --limit 1 | python3 examples/consumers/log_to_file.py; echo "exit $?"
```

## A login link command

With `KITE_MCP_LOGIN_LINK_COMMAND` set (in managed mode, with `KITE_MCP_PUBLIC_URL`, on the HTTP transport), the
server hands a fresh Kite login link to a program of yours: at each reminder time (`KITE_MCP_LOGIN_REMINDER_TIMES`)
when no valid token is stored, after Kite rejected the stored token (with `KITE_MCP_EVENTS=true`; at most one such
link every two hours), and when you call `login(deliver=true)`. You tap the link, log in to Kite, and Kite sends you
back to the server, which stores the new token.

| File | Input (stdin) | Exit | Settings (environment) |
|---|---|---|---|
| [login_link_telegram.py](login_link_telegram.py) | One JSON object: `{"url", "expires_at", "reason"}`, where `reason` is `scheduled`, `token_rejected` or `requested` | 0 sent; anything else not sent | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, optional `TELEGRAM_API_BASE` |

The example sends a line saying why (time to log in, the stored session was rejected, or the link you asked for),
the time the link expires, and the link on a line of its own. The link logs in to your account, once, until it
expires: send it only to your own chat. The example never prints it and asks Telegram not to fetch a preview of it.
The server does not retry a link that was not sent: its next scheduled time is the retry. It sends the command's
output nowhere and notes only how it ended (`exit 1`), so run the command by hand to see its message.

### Installing it

1. Install it, executable, somewhere only root (or the server's user) can change, for example
   `/usr/local/bin/send-kite-link`. From a checkout of this repository:

   ```bash
   sudo install -m 0755 examples/login_link_telegram.py /usr/local/bin/send-kite-link
   ```

   It must be owned by the server's user or root and writable by no one else, and so must the folders on the way to
   it: the server checks it when it starts and before each run. The server runs it with no arguments, through its
   first line (`#!/usr/bin/env python3`), so `python3` must be on the server's `PATH`. Under systemd, as in
   [SELF_HOSTING.md](../docs/SELF_HOSTING.md), keep it out of `/home`, which `ProtectHome=true` hides.
2. Put the Telegram variables in the server's settings file, with your own values, and name them in
   `KITE_MCP_LOGIN_LINK_ENV_PASS`: the command gets only `PATH`, `HOME`, `LANG` and the variables named there,
   never the server's `KITE_*` settings.

   ```bash
   KITE_MCP_LOGIN_LINK_COMMAND=/usr/local/bin/send-kite-link
   KITE_MCP_LOGIN_LINK_ENV_PASS=TELEGRAM_BOT_TOKEN,TELEGRAM_CHAT_ID
   KITE_MCP_LOGIN_REMINDER_TIMES=07:30,08:30
   TELEGRAM_BOT_TOKEN=123456:ABC-placeholder
   TELEGRAM_CHAT_ID=123456789
   ```

3. Restart the server. `kite-connect-mcp status` then shows `login_links`: the times, the next one and how the last
   link went. To try the command, ask your AI client for `login(deliver=true)`, or feed it a made-up link by hand.
   Paste the bot token at `read`'s silent prompt, not on the command line, so that it lands neither on the screen nor
   in your shell history:

   ```bash
   read -rs TELEGRAM_BOT_TOKEN && export TELEGRAM_BOT_TOKEN    # paste the token, then Enter
   export TELEGRAM_CHAT_ID=123456789
   echo '{"url": "https://kite.zerodha.com/connect/login?v=3&api_key=...", "reason": "requested"}' \
       | /usr/local/bin/send-kite-link; echo "exit $?"
   ```
