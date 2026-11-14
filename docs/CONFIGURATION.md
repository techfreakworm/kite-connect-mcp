# Configuration reference

kite-connect-mcp is configured with environment variables, optionally loaded from a file. Every setting has a safe
default: dry-run trading, a ₹10,000 per-order cap, HTTP on the loopback address only. The annotated template is
[`.env.example`](../.env.example). This page is the complete list, with the rules the server enforces when it starts.

- [How settings are read](#how-settings-are-read)
- [Kite app and endpoints](#kite-app-and-endpoints)
- [Session and token store](#session-and-token-store)
- [Local state](#local-state)
- [Transport and HTTP](#transport-and-http)
- [Safety](#safety)
- [Tools](#tools)
- [WebSocket ticker](#websocket-ticker)
- [Order events](#order-events)
- [Scheduled login links](#scheduled-login-links)
- [Logging](#logging)
- [Other variables that matter](#other-variables-that-matter)
- [Validation rules](#validation-rules)
- [Command-line options](#command-line-options)
- [Example settings files](#example-settings-files)

## How settings are read

1. **Precedence.** Command-line options (`serve --transport`, `--host`, `--port`) beat the process environment, which
   beats the `--env-file` file, which beats the defaults.
2. **The env file.** Pass it with the global option `--env-file`, placed **before** the subcommand:
   `kite-connect-mcp --env-file /path/to/env serve`. Placed after the subcommand it is rejected as an unrecognised
   argument. The format is one `KEY=VALUE` per line. Blank lines and lines starting with `#` are ignored, `export ` in
   front of a key is allowed, and matching single or double quotes around a value are removed. The server does not check
   the file's mode, so `chmod 600` it yourself if it holds credentials. For a file that systemd (`EnvironmentFile=`),
   Docker (`env_file:`) and `--env-file` must all read the same way, write plain `KEY=VALUE` lines with no quotes and no
   comment after a value: they treat quotes and trailing comments differently.
3. **Secrets can come from files.** `KITE_API_KEY`, `KITE_API_SECRET`, `KITE_ACCESS_TOKEN` and
   `KITE_MCP_BEARER_TOKEN` each have a `_FILE` twin (`KITE_API_SECRET_FILE=/etc/kite-connect-mcp/api_secret`). The
   file holds only the value. Rules:
   - Set the variable **or** its `_FILE`, not both.
   - The file must not be readable by group or others (`chmod 600`); the server refuses it otherwise.
   - It must exist, be readable by the server's user, and hold exactly one value with no whitespace in it.
   - `~` in the path is expanded. A value given directly must not contain whitespace either.
4. **Value types.**
   - *Booleans:* `true`, `false`, `1`, `0`, `yes`, `no`, `on`, `off`, in any case. An **empty** value means the
     default, so `KITE_MCP_ALLOW_LOGOUT=` leaves logout on: write `false` to turn a switch off.
   - *Integers:* whole numbers, with the minimum shown in the tables. An empty value means the default.
   - *Amounts (INR):* non-negative numbers; `10000`, `10,000`, `10_000` and `2500.50` all work.
   - *Choices:* case-insensitive, and `-` counts as `_` (`dry-run` is `dry_run`).
   - *Lists:* comma-separated; spaces around items are trimmed and empty items dropped.
5. **Unknown names are ignored.** A mistyped variable name raises no error and has no effect. After editing, run
   `kite-connect-mcp check-config` and read the output.
6. **All problems at once.** On a bad configuration the server prints every problem and exits with status 2.
   `check-config` prints the effective settings as JSON; credentials show only as `set` or `unset`.

## Kite app and endpoints

| Variable | Default | Meaning | Notes |
|---|---|---|---|
| `KITE_API_KEY`, `KITE_API_KEY_FILE` | unset | The API key of your Kite Connect app. | Needed to log in and to call Kite. `check-config` does not insist on it: look for `"api_key": "set"`. |
| `KITE_API_SECRET`, `KITE_API_SECRET_FILE` | unset | The API secret. | Needed for the login exchange (managed mode) and to verify postback checksums. Prefer the `_FILE` form. |
| `KITE_ACCESS_TOKEN`, `KITE_ACCESS_TOKEN_FILE` | unset | A fixed access token taken from the environment instead of the token file. | Its expiry is unknown, so Kite's answer decides. It wins over the token file. Usually leave unset. |
| `KITE_MCP_HTTP_TIMEOUT_S` | `30` | Seconds to wait for Kite to answer. Integer, at least 1. | The connect timeout is fixed at 10 seconds. A write that times out is reported as "outcome unknown". |
| `KITE_API_BASE_URL` | `https://api.kite.trade` | The Kite REST API. | Advanced; for tests. Your API key and access token are sent here, so leave it alone. |
| `KITE_LOGIN_URL` | `https://kite.zerodha.com/connect/login` | The Kite login page that the login link points to. | Advanced; leave unset. |
| `KITE_TICKER_URL` | `wss://ws.kite.trade` | The Kite WebSocket. | Advanced; leave unset. |

## Session and token store

| Variable | Default | Meaning | Notes |
|---|---|---|---|
| `KITE_MCP_AUTH_MODE` | `managed` | `managed`: this server owns the daily login (login link, callback, token file, optional logout). `external`: another program writes the token file; this server only reads it. | In `external` mode it never writes, deletes or logs out the token. See [KITE_SETUP.md](KITE_SETUP.md#7-the-daily-login). |
| `KITE_TOKEN_FILE` | `<state dir>/access_token.json` | The token file. | JSON `{"access_token", "login_date", "logged_in_at", "expires_at"}` (ISO 8601 times with offsets), or a bare token. A bare token, or JSON without `expires_at`, expires at the first 06:00 IST after the file's time. Re-read whenever it changes. A warning is logged if group or others can read it. |
| `KITE_MCP_ALLOWED_USER_ID` | unset | Refuse a login from any Kite account other than this user ID (for example `AB1234`). | Upper-cased. Recommended in managed mode (this pin, or the next one). |
| `KITE_MCP_ALLOWED_USER_ID_SHA256` | unset | The same pin, given as the SHA-256 of the user ID, so the plain ID need not be in any file: 64 hexadecimal digits, the digest of the user ID in upper case. Make it with `read -r id && printf %s "$id" \| tr '[:lower:]' '[:upper:]' \| sha256sum \| cut -d' ' -f1`, then type the user ID and Enter: the ID stays out of the shell's history file, and only the 64 digits are printed. | Any case; never echoed in an error. With both pins set they must name the same account. The server's own output (the audit log, the session tools, `status --verify`, the `login` command) then names the account by the first 12 digits of the digest instead of its user ID; Kite's answers that the read tools pass on (the profile, an order's placed_by) still hold the plain ID. The digest is not a secret: a user ID is short, and its digest can be found by trying every ID. See [SAFETY.md](SAFETY.md#14-sessions-credentials-and-external-mode). |
| `KITE_MCP_LOGIN_LINK_TTL_S` | `3600` | How long a login link stays usable, in seconds. Integer, at least 60. | Never past 05:59 IST, the minute before the 06:00 token reset. |
| `KITE_MCP_ALLOW_LOGOUT` | `true` | `false`: the `logout` tool is not offered, and the logout command refuses (exit 2, with a `logout_refused` audit line). | The stored session then lapses at 06:00 IST (a new login still ends it, unless `KITE_MCP_LOGIN_ALLOW_FORCE` is `false` too). Ending the fresh session of an account the pin refused is not a logout, and still happens. |
| `KITE_MCP_LOGIN_ALLOW_FORCE` | `true` | `false`: no login link is issued while a valid token exists, whoever asks (the `login` tool, even with force=true and with deliver=true, and the `login` command), as a login from it would end that token at Kite. Nor is a request token pasted without the state of a login link exchanged (`complete_login`, the `login` command's `--request-token`), before Kite is asked anything; the whole URL Kite redirected to after a login from this server's link carries that state and goes through. Each refusal is audited (`login_link_refused`). | Valid means: not expired, and not rejected by Kite. A rejection is remembered by the token's fingerprint, across a restart when the order events are on (in the event store), else until the server stops. An agent asks Kite with `session_status` and verify=true; the `login` command asks Kite once itself (GET /user/profile, read-only) and issues the link if Kite rejected the token. |

These switches, the pins and the [scheduled login links](#scheduled-login-links) belong in the settings file that the
commands read too (`--env-file`, or the `EnvironmentFile=` that `kite-mcp-ctl` reads on a server), not only in the
service's own environment: the logout command must see `KITE_MCP_ALLOW_LOGOUT=false` to refuse, and `status` must see
the link command to show the links.

## Local state

| Variable | Default | Meaning | Notes |
|---|---|---|---|
| `KITE_MCP_STATE_DIR` | `$XDG_STATE_HOME/kite-connect-mcp`, else `~/.local/state/kite-connect-mcp` | Where the server keeps its state: the token file (default location), the `audit/` folder and the kill-switch file (default location). | `~` is expanded. Created with mode 700 when first written. Back it up (see [SELF_HOSTING.md](SELF_HOSTING.md#backups)). |
| `KITE_MCP_CACHE_DIR` | `$XDG_CACHE_HOME/kite-connect-mcp`, else `~/.cache/kite-connect-mcp` | Cached downloads such as the instrument lists. | Safe to delete; it is rebuilt. |

What the defaults give you (the kill-switch file has its own setting, under [Safety](#safety)):

| What | Where |
|---|---|
| Token file | `<state dir>/access_token.json` |
| Audit log | `<state dir>/audit/audit-YYYY-MM-DD.jsonl` (IST day) |
| Kill switch | `<state dir>/KILL_SWITCH` |
| Event store (with `KITE_MCP_EVENTS=true`) | `<state dir>/events.sqlite3` |
| Server install (systemd guide) | state `/var/lib/kite-connect-mcp`, cache `/var/cache/kite-connect-mcp` |
| Docker image | state `/var/lib/kite-connect-mcp/state`, cache `/var/lib/kite-connect-mcp/cache` |

## Transport and HTTP

| Variable | Default | Meaning | Notes |
|---|---|---|---|
| `KITE_MCP_TRANSPORT` | `stdio` | `stdio` for clients that launch the server; `http` for Streamable HTTP at `/mcp`. | `serve --transport` overrides it. The Docker image sets `http`. |
| `KITE_MCP_HOST` | `127.0.0.1` | The address the HTTP server binds to. | `serve --host` overrides it. Keep it on loopback and put a reverse proxy in front. The Docker image sets `0.0.0.0` inside the container. |
| `KITE_MCP_PORT` | `8765` | The HTTP port. Integer, at least 1. | `serve --port` overrides it. |
| `KITE_MCP_BEARER_TOKEN`, `KITE_MCP_BEARER_TOKEN_FILE` | unset | The secret that clients send as `Authorization: Bearer <token>` on `/mcp`. | At least 32 characters, no whitespace (`openssl rand -hex 32`). Required for `http` unless `KITE_MCP_ALLOW_UNAUTHENTICATED_HTTP=true`. Checked whenever it is set. |
| `KITE_MCP_ALLOW_UNAUTHENTICATED_HTTP` | `false` | Serve `/mcp` without a bearer token. | Allowed only when `KITE_MCP_HOST` is `127.0.0.1`, `localhost` or `::1`. For throwaway local tests. |
| `KITE_MCP_PUBLIC_URL` | unset | The public URL behind a reverse proxy, for example `https://kite.example.com`. | Include the scheme; a trailing slash is removed. Its host and origin are added to the Host and Origin checks. Without a scheme it is silently ignored by those checks. The Kite redirect URL is this plus the callback path. |
| `KITE_MCP_ALLOWED_HOSTS` | empty | Extra `Host` header values to accept, comma-separated. | An entry without a port also matches any port. `127.0.0.1`, `localhost` and `[::1]` are always accepted, and so is the host of `KITE_MCP_PUBLIC_URL`. A request with another Host gets 421. |
| `KITE_MCP_CALLBACK_PATH` | `/callback` | Path of the login redirect endpoint (`GET`). | Must start with `/` and must not be under `/mcp`. Your Kite app's redirect URL must be the public URL plus this path. |
| `KITE_MCP_POSTBACK_PATH` | unset | Turns on the order-postback endpoint at this path (`POST`), for example `/postback`. | Same rules as the callback path. The postback URL in the developer console must match. Without it the endpoint does not exist. With `KITE_MCP_EVENTS=true` postbacks feed the [order events](EVENTS.md). |

Fixed paths: `/mcp` (the MCP endpoint, bearer-protected, as is anything below `/mcp`) and `/health` (public; answers
`{"status":"ok"}` and nothing more, so it gives away no version).

## Safety

How these behave is explained in [SAFETY.md](SAFETY.md).

| Variable | Default | Meaning | Notes |
|---|---|---|---|
| `KITE_MCP_READ_ONLY` | `false` | `true`: every write tool is hidden and the HTTP client refuses write endpoints. | Overrides `KITE_MCP_TRADING_MODE=live`. |
| `KITE_MCP_TRADING_MODE` | `dry_run` | `dry_run`: writes can only be previewed. `live`: writes can be sent after confirmation and every check. | |
| `KITE_MCP_LIVE_CONFIRMATION` | `token` | `token`: a live write needs the `confirmation_token` from a dry run of the identical call. `allowlist`: a write whose every leg is in `KITE_MCP_ALLOWED_INSTRUMENTS` needs no token. | `allowlist` requires an explicit instrument list (not `*`). |
| `KITE_MCP_CONFIRMATION_TTL_S` | `300` | How long a confirmation token is valid, in seconds. Integer, at least 10. | |
| `KITE_MCP_MAX_ORDER_VALUE` | `10000` | Per-order cap, in INR of notional value. | `0` refuses every order that is not a cancellation. |
| `KITE_MCP_MAX_DAILY_VALUE` | `50000` | Cap on the value of live orders placed in one IST day. | Computed from the audit log. |
| `KITE_MCP_MAX_ORDERS_PER_DAY` | `20` | Cap on the number of live orders in one IST day. Integer, at least 0. | `autoslice` counts as 10. `0` blocks new live orders. |
| `KITE_MCP_MAX_QUANTITY` | unset | Cap on the quantity of any single order. Integer, at least 1. | Unset means no cap. |
| `KITE_MCP_ALLOWED_INSTRUMENTS` | any | Allowed `EXCHANGE:TRADINGSYMBOL` values, for example `NSE:INFY,NSE:TCS`. | Unset, empty or `*` means any. Case-insensitive. |
| `KITE_MCP_ALLOWED_EXCHANGES` | any | Allowed exchanges: `NSE`, `BSE`, `NFO`, `BFO`, `CDS`, `BCD`, `MCX`. | |
| `KITE_MCP_ALLOWED_PRODUCTS` | any | Allowed products: `CNC`, `NRML`, `MIS`, `MTF`. | |
| `KITE_MCP_ALLOWED_VARIETIES` | any | Allowed varieties: `regular`, `amo`, `co`, `iceberg`, `auction`. | Compared in lower case. |
| `KITE_MCP_ALLOWED_ORDER_TYPES` | any | Allowed order types: `MARKET`, `LIMIT`, `SL`, `SL-M`. | Applies to cancellations as well. |
| `KITE_MCP_ALLOWED_TRANSACTION_TYPES` | any | Allowed sides: `BUY`, `SELL`. | |
| `KITE_MCP_ALLOW_MF_WRITES` | `false` | Enable the mutual-fund order and SIP write tools. | They use routes Kite no longer documents and may be rejected. See [KITE_SETUP.md](KITE_SETUP.md#1-choose-a-plan). |
| `KITE_MCP_KILL_SWITCH_FILE` | `<state dir>/KILL_SWITCH` | While this file exists, every write is refused. | A tool can create it; only a person deletes it. |

## Tools

| Variable | Default | Meaning | Notes |
|---|---|---|---|
| `KITE_MCP_TOOLSETS` | `all` | The tool groups to expose, comma-separated, or `all`. | Known groups: `session`, `account`, `orders`, `gtt`, `alerts`, `portfolio`, `market`, `margins`, `mf`, `ticker`, `events`, `safety`. An unknown name is an error. Case-insensitive. |
| `KITE_MCP_EXCLUDED_TOOLS` | empty | Individual tool names to hide, comma-separated (for example `modify_order`). | Names are lower-cased; an unknown name is ignored without a warning. |

`kite-connect-mcp tools` lists what your configuration exposes, and what is hidden and why; it is the authoritative list
for your version, and the full reference with every parameter is `docs/TOOLS.md`. The groups and their tools:

| Group | Reads and local tools | Writes (go through the [safety pipeline](SAFETY.md)) |
|---|---|---|
| `session` | `session_status`, `login`, `complete_login`, `logout` | none (the session calls are not account writes) |
| `account` | `get_profile`, `get_margins` | none |
| `orders` | `get_orders`, `get_order_history`, `get_trades`, `get_order_trades` | `place_order`, `modify_order`, `cancel_order` |
| `gtt` | `get_gtts`, `get_gtt` | `place_gtt_order`, `modify_gtt_order`, `delete_gtt_order` |
| `alerts` | `get_alerts`, `get_alert`, `get_alert_history` | `create_alert`, `modify_alert`, `delete_alerts` |
| `portfolio` | `get_holdings`, `get_positions`, `get_auctions` | `convert_position`, `authorise_holdings` |
| `market` | `get_quotes`, `get_ohlc`, `get_ltp`, `get_historical_data`, `search_instruments`, `get_instrument`, `get_instruments_status`, `refresh_instruments` | none |
| `margins` | `get_order_margins`, `get_basket_margins`, `get_order_charges` (calculators: allowed in read-only mode) | none |
| `mf` | `get_mf_holdings`, `get_mf_orders`, `get_mf_sips`, `search_mf_instruments` | `place_mf_order`, `cancel_mf_order`, `place_mf_sip`, `modify_mf_sip`, `cancel_mf_sip` (only with `KITE_MCP_ALLOW_MF_WRITES=true`) |
| `ticker` | `ticker_subscribe`, `ticker_unsubscribe`, `ticker_get_ticks`, `ticker_status`, `ticker_order_updates`, `ticker_stop` | none |
| `events` | `get_order_events`, `get_event_status` (only with an event store: `KITE_MCP_EVENTS=true`, or `KITE_MCP_EVENTS_DB`; see [EVENTS.md](EVENTS.md)) | none |
| `safety` | `get_safety_status`, `get_audit_log`, `engage_kill_switch` | none |

## WebSocket ticker

| Variable | Default | Meaning | Notes |
|---|---|---|---|
| `KITE_MCP_TICKER_ENABLED` | `true` | Allow the WebSocket ticker tools to connect. | Streaming needs the Connect plan. |
| `KITE_MCP_TICKER_MAX_SUBSCRIPTIONS` | `1000` | The most instruments the ticker will subscribe to. Integer from 1 to 3000. | 3000 is Kite's limit per connection. |

## Order events

Off by default. What they are and how to run them: [EVENTS.md](EVENTS.md).

| Variable | Default | Meaning | Notes |
|---|---|---|---|
| `KITE_MCP_EVENTS` | `false` | Run the event service in this server: postbacks as a doorbell, the order-book poll, the event store and the consumers. | Needs the HTTP transport (`KITE_MCP_TRANSPORT=http`, or `serve --transport http`). Read-only: it only reads orders from Kite. |
| `KITE_MCP_EVENTS_DB` | `<state dir>/events.sqlite3` with `KITE_MCP_EVENTS=true`, else unset | The event store, a SQLite file (mode 600). | `~` is expanded. The server needs it only to move the store. In any other process (a stdio client, the `events` commands) it names the server's store to read; that process must run as a user that can read the file. |
| `KITE_MCP_EVENTS_CONSUMERS_FILE` | unset | The consumers file (JSON): the commands that get each event. | `~` is expanded. Without it events are recorded and nobody is called. Read when the server starts: a bad file, or one that someone other than the server's user or root could change, stops the server. |
| `KITE_MCP_ORDER_POLL_INTERVAL_S` | `60` | Seconds between two polls of the order book. Integer, at least 10. | |
| `KITE_MCP_ORDER_POLL_WINDOW` | `09:00-16:00` | When the poll runs, in IST: `HH:MM-HH:MM`. | The start must be before the end. The poll also runs at start-up, at once after a login, and after the window until one poll succeeds. |
| `KITE_MCP_ORDER_POLL_DAYS` | `Mon-Fri` | The days the poll runs. | A range (`Mon-Fri`), a list (`mon,wed,fri`), one day (`Tue`) or `*` for every day. A day is three or more letters of its name, in any case. A range runs forward from Monday to Sunday, so `Sat-Mon` is refused: write `sat,sun,mon`. |
| `KITE_MCP_EVENTS_KEEP_DAYS` | `30` | Days to keep events. Integer from 1 to 3650. | Older events are deleted with their deliveries. |

The postback route is `KITE_MCP_POSTBACK_PATH` (under [Transport and HTTP](#transport-and-http)); without it the poll
alone feeds the events. With `KITE_MCP_ALLOWED_USER_ID` or `KITE_MCP_ALLOWED_USER_ID_SHA256` set, a postback for any
other account is refused, and `KITE_API_SECRET` is needed to check postbacks at all.

## Scheduled login links

Off by default. At the times you set, when no valid token is stored, the server issues a login link and hands it to a
program of yours, the link command, which sends it to you (a Telegram message, say); you tap it and log in at Kite.
The command also gets a link after Kite rejects the stored token (with `KITE_MCP_EVENTS=true`; at most one every two
hours) and when an agent calls `login` with deliver=true. When a link goes out, the catch-up of a missed time and the
command's contract: [EVENTS.md](EVENTS.md#scheduled-login-links). An example command for Telegram:
[examples/README.md](../examples/README.md#a-login-link-command).

| Variable | Default | Meaning | Notes |
|---|---|---|---|
| `KITE_MCP_LOGIN_REMINDER_TIMES` | unset | The times of the scheduled links, in IST: `HH:MM`, comma-separated, for example `07:30,08:30`. | Sorted, repeats dropped. Needs `KITE_MCP_LOGIN_LINK_COMMAND`. A time when a valid token is stored sends nothing. |
| `KITE_MCP_LOGIN_REMINDER_DAYS` | `Mon-Fri` | The days of the scheduled links. | The syntax of `KITE_MCP_ORDER_POLL_DAYS`: `Mon-Fri`, `mon,wed,fri`, `Tue` or `*`. |
| `KITE_MCP_LOGIN_REMINDER_SKIP_DATES_FILE` | unset | A file of dates with no scheduled link (exchange holidays): one `YYYY-MM-DD` per line. | `#` starts a comment; blank lines are fine. A regular file of at most 64 KiB, UTF-8 with or without a byte-order mark. Read again at each use, so a new list needs no restart. A file that cannot be read, or a line that is not a date, is a warning in the log and skips nothing (the other dates still apply). `~` is expanded. |
| `KITE_MCP_LOGIN_LINK_COMMAND` | unset | The link command: the absolute path of one executable program, run with no arguments. | No arguments and no whitespace: wrap arguments in a script. It must be a regular file owned by the server's user or root and writable by no one else, and so must the folders on the way to it; checked when the server starts and before each run. |
| `KITE_MCP_LOGIN_LINK_ENV_PASS` | empty | Names of environment variables to pass to the command, comma-separated, for example `TELEGRAM_BOT_TOKEN,TELEGRAM_CHAT_ID`. | The command gets only `PATH`, `HOME`, `LANG` and these. A `KITE_*` name is refused: the command never gets the server's settings. |
| `KITE_MCP_LOGIN_LINK_TIMEOUT_S` | `30` | How long the command may run, in seconds. Integer from 1 to 600. | Past it, the command and whatever it started are killed, and the link counts as not sent. |

Times or a command need `KITE_MCP_AUTH_MODE=managed` and `KITE_MCP_PUBLIC_URL` (a link is of use only when Kite's
redirect reaches this server's callback); `check-config` checks that. They also need the HTTP transport: that is
checked when `serve` starts, since the systemd sample gives the transport on the command line and the other commands
read the same settings without it. One serving process sends the links of a state folder: a second one on the same
`KITE_MCP_STATE_DIR` does not start. With a command set, `session_status`, `kite-connect-mcp status`,
`kite-connect-mcp events status` and, in the serving process, `get_event_status` show `login_links`: the times, the
next one, the last time handled and how the last link went. It is kept in `<state dir>/login_links.json` (mode 600),
which never holds a link.

## Logging

| Variable | Default | Meaning | Notes |
|---|---|---|---|
| `KITE_MCP_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` or `CRITICAL`, in any case. | Logs go to stderr (the journal under systemd, `docker logs` under Docker), never to stdout, which carries the stdio protocol. There is no HTTP access log, because the login callback's URL holds one-time values. Credentials are never logged. |

## Other variables that matter

| Variable | Meaning |
|---|---|
| `XDG_STATE_HOME`, `XDG_CACHE_HOME` | Where the default state and cache folders go (see above). |
| `HTTPS_PROXY`, `HTTP_PROXY`, `ALL_PROXY`, `NO_PROXY` | The HTTP client honours the standard proxy variables. Use `HTTPS_PROXY` to send Kite requests through a static-IP proxy (see [KITE_SETUP.md](KITE_SETUP.md#static-ip-for-live-orders)). They must be in the **process environment** (systemd `EnvironmentFile=`, Docker `env_file`, a client's `env` block, or your shell). A file given to `--env-file` only feeds this server's own settings and is not exported, so a proxy variable that is only there has no effect. |

## Validation rules

The server checks these at start-up and when you run `check-config`. Each failure is listed, and the exit status is 2.

| Rule | Message (shortened) |
|---|---|
| A boolean must be a recognised word | `NAME must be true or false` |
| An integer must be a whole number at or above its minimum | `NAME must be a whole number` / `must be at least N` |
| An amount must be a non-negative number | `NAME must be a non-negative number` |
| A choice must be one of its values | `NAME must be one of ...` |
| Set a secret or its `_FILE`, not both | `set NAME or NAME_FILE, not both` |
| A secret file must exist, be readable, and hold one value | `file not found` / `cannot read` / `is empty or holds more than one value` |
| A secret file must not be group- or other-readable | `is readable by group/others (mode 644); run chmod 600` |
| A directly given secret has no whitespace | `NAME contains whitespace` |
| Toolsets must be known | `KITE_MCP_TOOLSETS has unknown toolsets [...]` |
| The callback and postback paths start with `/` and are not under `/mcp` | `must start with / and must not be under /mcp` |
| The ticker limit is at most 3000 | `cannot exceed 3000 (Kite's per-connection limit)` |
| `allowlist` confirmation needs an explicit instrument list | `needs an explicit KITE_MCP_ALLOWED_INSTRUMENTS list (not '*')` |
| HTTP transport needs a bearer token | `HTTP transport needs KITE_MCP_BEARER_TOKEN (or KITE_MCP_BEARER_TOKEN_FILE)` |
| Unauthenticated HTTP only on loopback | `KITE_MCP_ALLOW_UNAUTHENTICATED_HTTP=true is only allowed with a loopback KITE_MCP_HOST` |
| The bearer token is at least 32 characters | `KITE_MCP_BEARER_TOKEN must be at least 32 characters (try: openssl rand -hex 32)` |
| The log level is known | `KITE_MCP_LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR or CRITICAL` |
| Order events need the HTTP transport | `KITE_MCP_EVENTS=true needs KITE_MCP_TRANSPORT=http` |
| The poll window is `HH:MM-HH:MM`, the start before the end | `must look like 09:00-16:00` / `the start must be before the end` |
| The poll days and the reminder days are days, and a range runs forward | `must look like Mon-Fri or mon,wed,fri` / `runs backwards (ranges run forward from Mon to Sun)` |
| Events are kept 3650 days at most | `KITE_MCP_EVENTS_KEEP_DAYS cannot exceed 3650 (ten years)` |
| The pin's digest is 64 hexadecimal digits (the value is never shown) | `KITE_MCP_ALLOWED_USER_ID_SHA256 must be 64 hexadecimal digits: the SHA-256 of the user id in upper case` |
| The two pins name the same account | `KITE_MCP_ALLOWED_USER_ID and KITE_MCP_ALLOWED_USER_ID_SHA256 name different accounts` |
| The reminder times are `HH:MM` | `KITE_MCP_LOGIN_REMINDER_TIMES must be a comma list of HH:MM times, such as 07:30,08:30` |
| Reminder times need a link command | `KITE_MCP_LOGIN_REMINDER_TIMES needs KITE_MCP_LOGIN_LINK_COMMAND` |
| Times or a link command need managed mode | `KITE_MCP_LOGIN_REMINDER_TIMES and KITE_MCP_LOGIN_LINK_COMMAND need KITE_MCP_AUTH_MODE=managed` |
| Times or a link command need the public URL | `KITE_MCP_LOGIN_REMINDER_TIMES and KITE_MCP_LOGIN_LINK_COMMAND need KITE_MCP_PUBLIC_URL` |
| The link command is an absolute path to one program (the value is never shown) | `KITE_MCP_LOGIN_LINK_COMMAND must be an absolute path to one program, with no arguments and no spaces` |
| A path's `~` has a home folder | `NAME starts with a ~ whose home folder cannot be found: give the full path` |
| The variables passed to the link command are names, and none is a `KITE_*` name | `KITE_MCP_LOGIN_LINK_ENV_PASS must list environment variable names, separated by commas` / `must not pass KITE_* variables` |
| The link command's timeout is at most 600 seconds | `KITE_MCP_LOGIN_LINK_TIMEOUT_S cannot exceed 600` |

`check-config` does not read the consumers file: the server checks it when it starts (see
[EVENTS.md](EVENTS.md#consumers)). Nor does it check the login links' serving needs: `serve` refuses to start when
times or a link command are set and the transport is not HTTP (`need the HTTP transport (serve --transport http)`),
when the link command is not an executable regular file that only the server's user or root can change, or when
another process already sends the links of the same state folder.

## Command-line options

| Command | Options |
|---|---|
| `kite-connect-mcp` (any command) | `--env-file FILE` (before the subcommand), `--version`, `-h` |
| `serve` (default) | `--transport stdio\|http`, `--host ADDRESS`, `--port N` |
| `status` | `--verify`: also make one read-only call to Kite to check the token |
| `tools` | `--markdown`: print a Markdown reference |
| `check-config` | none: validate and print the settings |
| `audit` | `--date YYYY-MM-DD` (IST day, default today), `--limit N` (default 50) |
| `login` | `--listen` (wait for the redirect on a local callback), `--request-token TOKEN` (exchange a token you pasted), `--json` (print the link and its expiry as one JSON line, `{"url", "expires_at"}`, instead of the bare link; not with `--request-token`). Exit 2 without a link while forcing is off and Kite still accepts the stored token, or Kite could not be asked (run it again) |
| `logout` | `--yes` (really log out; managed mode only). Exit 2 with `KITE_MCP_ALLOW_LOGOUT=false` |
| `events status` | none: the health of the order events, as JSON |
| `events tail` | `--limit N` (the newest N events, 1 to 1000, default 20), `--order ID` (one order's events) |
| `events pause`, `events resume`, `events retry` | a consumer's name; `resume --skip-backlog` marks its waiting deliveries skipped |
| `events selftest` | `--url URL` (default: this server's own postback route on loopback) |

## Example settings files

**A laptop, stdio** (`~/.config/kite-connect-mcp/env`, mode 600):

```bash
KITE_API_KEY=your_api_key
KITE_API_SECRET_FILE=/home/you/.config/kite-connect-mcp/api_secret
KITE_MCP_ALLOWED_USER_ID=AB1234
```

**A server behind a reverse proxy** (`/etc/kite-connect-mcp/kite-connect-mcp.env`, mode 600):

```bash
KITE_API_KEY=your_api_key
KITE_API_SECRET_FILE=/etc/kite-connect-mcp/api_secret
KITE_MCP_BEARER_TOKEN_FILE=/etc/kite-connect-mcp/bearer_token
KITE_MCP_PUBLIC_URL=https://kite.example.com
KITE_MCP_ALLOWED_USER_ID=AB1234
KITE_MCP_STATE_DIR=/var/lib/kite-connect-mcp
KITE_MCP_CACHE_DIR=/var/cache/kite-connect-mcp
```

**Docker** (`kite-connect-mcp.env` next to the compose file, mode 600). Do not set `KITE_MCP_TRANSPORT`,
`KITE_MCP_HOST` or the two folders: the image sets them.

```bash
KITE_API_KEY=your_api_key
KITE_API_SECRET=your_api_secret
# replace the next value with the output of: openssl rand -hex 32
KITE_MCP_BEARER_TOKEN=openssl-rand-hex-32
KITE_MCP_PUBLIC_URL=https://kite.example.com
KITE_MCP_ALLOWED_USER_ID=AB1234
```
