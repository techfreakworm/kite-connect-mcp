# Example event consumers

With `KITE_MCP_EVENTS=true`, the server records changes to your orders and hands each event to the consumers you list
in the consumers file (`KITE_MCP_EVENTS_CONSUMERS_FILE`). A consumer is any program: it reads one event on stdin and
says by its exit code whether it is done. These examples are starting points; copy one and change it to suit you.

| File | What it does | Settings (environment) |
|---|---|---|
| [consumers/telegram_notify.py](consumers/telegram_notify.py) | Sends the event's `text` to a Telegram chat through the Bot API; a selftest is marked `[test]` | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, optional `TELEGRAM_API_BASE` |
| [consumers/log_to_file.py](consumers/log_to_file.py) | Appends each event as one JSON line to a file, once per `event_id`; a new file gets mode 600 | optional `EVENT_LOG_FILE` (default `~/kite-events.jsonl`) |
| [consumers/webhook.sh](consumers/webhook.sh) | POSTs each event as JSON to a URL, with curl | `WEBHOOK_URL` |
| [events.consumers.json](events.consumers.json) | A sample consumers file: the Telegram and log consumers, and a label for the tag `mytag` | |

The Telegram example sends only the event's `text`, which never holds an order id, an account or a link. The other two
pass on the whole event, order id included.

## The contract in short

- The event arrives on stdin as one JSON object, then a newline.
- The environment holds `PATH`, `HOME`, `LANG`, `KITE_EVENT_CONSUMER` (the consumer's name) and the variables named in
  its `env_pass`; never the server's `KITE_*` settings.
- Exit 0 when the event is handled, 75 when it should be retried later; any other exit is a permanent failure.
- Deliveries are at least once, so the same event can arrive twice: be idempotent on `event_id`.
- Whatever the program prints to stderr ends up, cleaned and clipped, in the server's log, in `events status` and in
  the other consumers' `delivery_failed` events: never print a secret there.

The whole contract, with the retry schedule and the timeouts, is in [docs/EVENTS.md](../docs/EVENTS.md).

## Installing one

1. Copy the program somewhere only you (or root) can change, for example `/opt/kite-connect-mcp/examples/consumers/`,
   and make it executable: `chmod 755 telegram_notify.py`. The server refuses a consumers file or a program that
   other users could change, and the folders on the way to them too.
2. Add the consumer to your consumers file; [events.consumers.json](events.consumers.json) shows the format. The
   `command` is a list of arguments, not a shell line, and its first item is an absolute path. Other users can see a
   command's arguments, so never put a secret there: put it in the server's environment (its settings file works) and
   name it in `env_pass`.
3. Make the consumers file private (`chmod 600`) and point `KITE_MCP_EVENTS_CONSUMERS_FILE` at it.
4. Restart the server: it reads the consumers file when it starts. Then send a selftest with
   `kite-connect-mcp events selftest` and check `kite-connect-mcp events status`.

A script given as an argument, as in `["/usr/bin/python3", "/path/to/telegram_notify.py"]`, is not checked by the
server: keep it owned by you or root and writable by no one else. In the Docker image, Python is
`/usr/local/bin/python3` and curl is not installed.

## Trying one by hand

Feed a consumer the newest stored event and look at its exit code. Run it with the server's settings, or name the
store with `KITE_MCP_EVENTS_DB`:

```bash
kite-connect-mcp events tail --limit 1 | python3 examples/consumers/log_to_file.py; echo "exit $?"
```
