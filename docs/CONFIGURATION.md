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
   - *Booleans:* `true`, `false`, `1`, `0`, `yes`, `no`, `on`, `off`, in any case. An **empty** value counts as
     false, even for a setting whose default is true; delete the line to get the default.
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
| `KITE_MCP_ALLOWED_USER_ID` | unset | Refuse a login from any Kite account other than this user ID (for example `AB1234`). | Upper-cased. Recommended in managed mode. |
| `KITE_MCP_LOGIN_LINK_TTL_S` | `3600` | How long a login link stays usable, in seconds. Integer, at least 60. | Capped at the next 06:00 IST token reset. |

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
| `KITE_MCP_POSTBACK_PATH` | unset | Turns on the order-postback endpoint at this path (`POST`), for example `/postback`. | Same rules as the callback path. The postback URL in the developer console must match. Without it the endpoint does not exist. |

Fixed paths: `/mcp` (the MCP endpoint, bearer-protected, as is anything below `/mcp`) and `/health` (public; answers
`{"status":"ok","version":...}`).

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
| `KITE_MCP_TOOLSETS` | `all` | The tool groups to expose, comma-separated, or `all`. | Known groups: `session`, `account`, `orders`, `gtt`, `alerts`, `portfolio`, `market`, `margins`, `mf`, `ticker`, `safety`. An unknown name is an error. Case-insensitive. |
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
| `safety` | `get_safety_status`, `get_audit_log`, `engage_kill_switch` | none |

## WebSocket ticker

| Variable | Default | Meaning | Notes |
|---|---|---|---|
| `KITE_MCP_TICKER_ENABLED` | `true` | Allow the WebSocket ticker tools to connect. | Streaming needs the Connect plan. |
| `KITE_MCP_TICKER_MAX_SUBSCRIPTIONS` | `1000` | The most instruments the ticker will subscribe to. Integer from 1 to 3000. | 3000 is Kite's limit per connection. |

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

## Command-line options

| Command | Options |
|---|---|
| `kite-connect-mcp` (any command) | `--env-file FILE` (before the subcommand), `--version`, `-h` |
| `serve` (default) | `--transport stdio\|http`, `--host ADDRESS`, `--port N` |
| `status` | `--verify`: also make one read-only call to Kite to check the token |
| `tools` | `--markdown`: print a Markdown reference |
| `check-config` | none: validate and print the settings |
| `audit` | `--date YYYY-MM-DD` (IST day, default today), `--limit N` (default 50) |
| `login` | `--listen` (wait for the redirect on a local callback), `--request-token TOKEN` (exchange a token you pasted) |
| `logout` | `--yes` (really log out; managed mode only) |

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
