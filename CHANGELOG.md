# Changelog

All notable changes are listed here. Versions follow [semantic versioning](https://semver.org); while the version
is 0.x, minor releases may change tool arguments.

## Unreleased

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
  once the postback is on disk and 503 when the store cannot write, and counts its refusals. With `KITE_MCP_EVENTS`
  off it is unchanged, except that a signed selftest is answered 404 and never reaches the ticker's buffer.
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
