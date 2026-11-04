# Changelog

All notable changes are listed here. Versions follow [semantic versioning](https://semver.org); while the version
is 0.x, minor releases may change tool arguments.

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
