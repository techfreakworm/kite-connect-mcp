# Changelog

All notable changes are listed here. Versions follow [semantic versioning](https://semver.org); while the version
is 0.x, minor releases may change tool arguments.

## Unreleased

### Added

- **The account pin by hash.** `KITE_MCP_ALLOWED_USER_ID_SHA256` pins the account by the SHA-256 of its user ID in
  upper case, so the plain ID need not be in any file. With it, the server's own output (the audit log, the session
  tools, `kite-connect-mcp status --verify` and the `login` command) names the account by the first 12 hex digits of
  the digest instead of its user ID. With both pins set they must name the same account. Every account check (the
  login and the postback route) compares SHA-256 digests in constant time, and still ignores case and surrounding
  spaces in the user ID Kite returns, as 0.2.0 did.
- **Logout off.** With `KITE_MCP_ALLOW_LOGOUT=false` the `logout` tool is not offered and the logout command refuses
  (exit 2). Ending a refused account's fresh session still happens.
- **Forcing off.** With `KITE_MCP_LOGIN_ALLOW_FORCE=false` no login link is issued while a valid token exists, whoever
  asks (the `login` tool, even with force=true, and the `login` command, which first asks Kite once with a read-only
  GET /user/profile and issues the link if Kite rejected the token). Nor is a request token pasted without the state
  of a login link exchanged (`complete_login`, `kite-connect-mcp login --request-token`, which exits 2), before Kite
  is asked anything; the whole URL Kite redirected to after a login from this server's link carries that state and
  goes through.
- **Session events** `login` (a new token was stored), `login_refused` (another account's login was refused by the
  pin) and `login_failed` (a login on a link of this server's did not complete; `reason` is `exchange_failed` or
  `token_not_stored`), each raised once, with the order events on ([docs/EVENTS.md](docs/EVENTS.md)).
- **Scheduled login links** ([docs/EVENTS.md](docs/EVENTS.md#scheduled-login-links)). At the times you set
  (`KITE_MCP_LOGIN_REMINDER_TIMES`, `KITE_MCP_LOGIN_REMINDER_DAYS`, `KITE_MCP_LOGIN_REMINDER_SKIP_DATES_FILE`), when
  no valid token is stored, the server hands a fresh login link to a program of yours that sends it to you
  (`KITE_MCP_LOGIN_LINK_COMMAND`, `KITE_MCP_LOGIN_LINK_ENV_PASS`, `KITE_MCP_LOGIN_LINK_TIMEOUT_S`). A time missed while
  the server was down or the machine slept is sent when it comes back within 3 hours: the latest such time, once.
  With the order events on, a link also goes out after `token_rejected`, at most one every two hours. Managed mode,
  `KITE_MCP_PUBLIC_URL` and the HTTP transport only; one serving process per state folder.
- `login(deliver=true)`: the link goes to the person through the link command, and the answer says whether it was
  sent, never the link.
- The login links' status, `login_links`, in `session_status`, `kite-connect-mcp status`,
  `kite-connect-mcp events status` and, in the serving process, `get_event_status`.
- `kite-connect-mcp login --json`: the link and its expiry as one JSON line, `{"url", "expires_at"}`.
- Example: a link command that sends the link to a Telegram chat
  ([examples/login_link_telegram.py](examples/login_link_telegram.py)).
- Deploy samples: the systemd unit shows the login settings, commented out, for the settings file; the logout unit
  says not to install its timer with logout off.

### Changed

What a 0.2.0 setup sees without changing a setting:

- With the order events on, a consumer whose `match` takes the `session` kind now also receives `login`,
  `login_refused` and `login_failed`. `match` has no filter by session name: ignore the names you do not want (exit 0).
- `token_rejected` is now raised also when a tool call is the first to meet Kite's rejection of the stored token
  (0.2.0: only the poll and the doorbell). It is still raised once per token, and never by a logout.
- With the order events on, in managed mode, a token that another process stores beside the serving process
  (`kite-connect-mcp login` run by hand, say) raises `login`, seen by the poll within seconds.
- With the order events on, in managed mode, a token Kite rejected stays rejected across a restart: the rejection is
  kept by the token's fingerprint in the event store (0.2.0 forgot it). A new login that Kite answers with the same
  token string clears it. External mode keeps the rejections in memory, as 0.2.0 did.
- With the order events on, in managed mode, `kite-connect-mcp status`, `login` and `logout` read those rejections
  from the event store: `status` shows a token the service saw rejected as not logged in, and a store that exists but
  cannot be read earns a warning on stderr.
- The `login` tool's note when no account pin is set now reads "no account pin is set (KITE_MCP_ALLOWED_USER_ID or
  KITE_MCP_ALLOWED_USER_ID_SHA256): any Kite account that completes this login is accepted" (0.2.0:
  "KITE_MCP_ALLOWED_USER_ID is not set: ...").
- The audit log has two new events: `logout_refused` and `login_link_refused`.

### Fixed

- docs/CONFIGURATION.md said that an empty boolean setting counts as false. It means the default, as the server has
  always read it.
- docs/SELF_HOSTING.md said that a variable only in the `--env-file` file (`HTTPS_PROXY`, say) is not exported. The
  server exports the file's variables to its own process, without replacing one already set. And a login link stops
  working at 05:59 IST at the latest, not 06:00, as docs/CONFIGURATION.md and `.env.example` said.

### Security

- A login link handed to the link command is never stored or logged, and never put in an event, a status, an audit
  line, an error or the answer of `login(deliver=true)`. The command's output is discarded, it gets a minimal
  environment (never a `KITE_*` variable), and the server refuses a command that anyone but its user or root could
  change, when it starts and before each run.
- The hash pin keeps the user ID out of plain sight; it is not a secret, as a short user ID can be found from its
  SHA-256 by trying them all. Kite's own answers that the read tools pass on (the profile, an order's placed_by) still
  hold the plain ID.

## 0.2.0 (2026-11-14)

### Added

- **Order events**, off by default ([docs/EVENTS.md](docs/EVENTS.md)). With `KITE_MCP_EVENTS=true` on the HTTP
  transport, the server records each change to the orders of its account and hands it to commands you configure.
  Postbacks are a doorbell: a verified postback makes the server read the order back from Kite, and only Kite's answer
  is stored. A poll of the order book is the backup, and the only source for orders placed outside the API key. The
  events are kept in a SQLite store and delivered to the consumer commands as JSON on standard input: one command at a
  time per consumer, each order's events in order, at least once, with retries and `delivery_delayed` and
  `delivery_failed` alerts. It only reads orders: it never places or changes one.
- Settings: `KITE_MCP_EVENTS`, `KITE_MCP_EVENTS_DB`, `KITE_MCP_EVENTS_CONSUMERS_FILE`,
  `KITE_MCP_ORDER_POLL_INTERVAL_S`, `KITE_MCP_ORDER_POLL_WINDOW`, `KITE_MCP_ORDER_POLL_DAYS` and
  `KITE_MCP_EVENTS_KEEP_DAYS`.
- Tools: the `events` toolset, `get_order_events` and `get_event_status`, shown when there is an event store. Another
  process, a stdio client say, reads the server's store by naming it in `KITE_MCP_EVENTS_DB`.
- Commands: `kite-connect-mcp events status`, `events tail`, `events pause`, `events resume [--skip-backlog]`,
  `events retry` and `events selftest [--url]`.
- Examples: consumers that send a Telegram message, append to a JSON-lines log and post to a webhook, and a sample
  consumers file ([examples/](examples/README.md)).
- Deploy samples: a systemd drop-in that turns the order events on for the service
  (`deploy/systemd/kite-connect-mcp.service.d/events.conf`).

### Changed

- The postback route: with `KITE_MCP_EVENTS=true` it exists whenever `KITE_MCP_POSTBACK_PATH` is set (the ticker on or
  off), feeds the order events, refuses a postback for any account other than `KITE_MCP_ALLOWED_USER_ID`, answers 200
  once the postback is on disk (a postback within 2 s of its order's last write is answered at once, and its ring is
  written when those 2 s are over) and 503 when the store cannot write, and counts its refusals. With
  `KITE_MCP_EVENTS` off it works as before, except that a signed selftest is answered 404 and never reaches the
  ticker's buffer.
- The postback route's log lines, with the events on or off: a refusal is logged at most once a minute per reason, and
  so is the line for an accepted postback; the next line says how many were not logged.
- `kite-connect-mcp serve` runs uvicorn with `lifespan="on"`: a server whose start-up fails (the event service, say)
  exits instead of serving without it.
- A token file that cannot be looked at or read (a wrong owner or mode) is logged when that starts and when the error
  changes, not on every read.
- The Caddy and nginx samples cap request bodies per path: 1 MB for `/mcp`, 4 KB for `/callback` and `/health`,
  64 KiB for `/postback` (the server's own cap).

### Fixed

- The docs and the Caddy and nginx samples said that `/health` answers with the version. It answers only
  `{"status":"ok"}`, which gives nothing away.

## 0.1.1 (2026-11-04)

Checked against the live Kite API (read-only): every read tool, the calculators, and the WebSocket ticker across
NSE, BSE, NFO, CDS and MCX, with ticker prices matching REST quotes.

### Fixed

- `get_historical_data` and `get_instrument` now take an instrument token as a JSON number (`408065`) as well as a
  string. Both were documented to take a numeric token, but a number was rejected as not a string.
- `get_order_trades` describes its `order_id` argument.

## 0.1.0 (2026-11-04)

The first release: every Kite Connect v3 capability, behind a write-safety layer.

### Tools (58 in 11 toolsets)

- **session**: session status (optionally verified with one read), the managed daily login (one-time links, a
  state-checked callback, manual request-token completion) and a two-step logout.
- **account**: profile, funds and margins.
- **orders**: place, modify and cancel (regular, AMO, cover, iceberg and auction varieties, market protection,
  autoslice), the order book, order history, trades.
- **gtt**: single and two-leg (OCO) GTTs: list, get, place, modify, delete.
- **alerts**: simple and alert-triggers-order alerts: list, get, history, create, modify, delete.
- **portfolio**: holdings and positions with summaries, auctions, position conversion, CDSL holdings authorisation.
- **market**: full quotes, OHLC, LTP, historical candles for every interval with continuous and OI data (long ranges
  split automatically), and instrument search over the daily dump, cached in SQLite.
- **margins**: order margins, basket margins, and charges (the virtual contract note).
- **mf**: holdings, orders, SIPs and fund search; order and SIP writes on Kite's legacy routes, opt-in.
- **ticker**: the WebSocket stream as a managed connection with subscriptions, a latest-tick cache and order updates;
  optional verified postbacks.
- **safety**: the safety settings and today's usage, the audit log, and a kill switch a tool can only engage.

### Safety

- Every write is a dry run by default; live trading is off by default (`KITE_MCP_TRADING_MODE=dry_run`).
- A live write needs a single-use confirmation token bound to the exact request, or an explicitly allow-listed
  instrument when you opt in.
- Per-order, daily value and daily count caps (read back from the audit log, so restarts never reset them);
  modifications are held to the daily cap by their increase, and unknown outcomes count as if they happened.
- Allow-lists for instruments, exchanges, products, varieties, order types and sides; read-only mode; a kill switch.
- An append-only audit log, with each live intent fsynced before the request is sent; writes are never retried.
- An endpoint allow-list in the HTTP client, client-side rate limits under Kite's, credential scrubbing everywhere.

### Running it

- stdio for desktop clients, Streamable HTTP behind a bearer token for servers, with Host-header checks.
- `managed` auth mode (this server owns the daily login) or `external` mode (it follows a token file another program
  keeps, and never writes it or logs out).
- A two-stage Docker image, a hardened systemd unit, Caddy and nginx examples, and guides for the Kite developer
  console (including the static IP live orders need), self-hosting and the safety model.
