# Decision: build fresh in Python rather than fork `zerodha/kite-mcp-server`

*Recorded 2026-11-03. Evaluated upstream commit `5e044dc` (2026-07-10), the head of `main` that day.*

## Summary

Zerodha's official [kite-mcp-server](https://github.com/zerodha/kite-mcp-server) is a clean, MIT-licensed Go
server, and its tests pass. It was built to run Zerodha's hosted multi-user service at `mcp.kite.trade`. That
design doesn't fit what this project needs:

- **Coverage.** It exposes 22 tools. Kite Connect v3 has around 50 operations, plus the WebSocket ticker and
  postbacks.
- **Sessions.** It keeps one in-memory token per MCP session. We need one daily token, stored on disk, that
  several clients share.
- **Safety.** It has no dry-run, confirmation step, value caps, audit log or rate limiting. Read-only mode
  exists only as a denylist of tool names.

Closing those gaps would mean rewriting the session layer and every write handler, and adding more code than
upstream has today. The result would be a fork in name only, carrying the cost of a fork without being able to
merge upstream changes. **So we build fresh, in Python, on the official MCP Python SDK v2.** We keep upstream's
tool names where they exist, so prompts written for `mcp.kite.trade` still work here.

## How the evaluation was done

- Read the whole upstream tree: `app/`, `kc/`, `mcp/`, the README and `.env.example`.
- Ran `go vet ./...` and `GOEXPERIMENT=synctest go test ./...` with Go 1.24.13 in a container. Vet is clean and
  every package passes.
- Built the endpoint list from Kite Connect v3's documentation (user, orders, GTT, alerts, portfolio, market
  quotes, historical, mutual funds, margins, WebSocket, postbacks and exceptions pages, read 2026-11-03). Checked
  it against the route table of the official Python client (`kiteconnect` 5.2.2), which also lists a few legacy
  routes the docs no longer show.

## Coverage against Kite Connect v3

| Area | Kite Connect v3 operation | Upstream tool | This project |
|---|---|---|---|
| Session | login URL | `login` (per MCP session) | `login` |
| | request_token → access_token (`POST /session/token`) | callback only | callback endpoint + `complete_login` |
| | logout (`DELETE /session/token`) | — (called implicitly on session cleanup) | `logout` (managed mode only, confirmed) |
| | daily expiry at 06:00 IST | — | tracked, reported by `session_status` |
| User | profile | `get_profile` | `get_profile` |
| | margins (all / by segment) | `get_margins` (all only) | `get_margins` (all, `equity`, `commodity`) |
| Orders | place: regular / amo / co / iceberg / auction | `place_order` (no `auction_number`, `market_protection`, `autoslice`; no CDS/BCD exchanges) | `place_order` with every documented parameter |
| | modify / cancel | `modify_order`, `cancel_order` | yes, including CO second legs (`parent_order_id`) |
| | order book, order history, trades, order trades | yes | yes |
| GTT | list / get / create / modify / delete | list, create, modify, delete (no get) | all five |
| Alerts | create (simple, ATO) / list / get / modify / delete / history | — | all six |
| Portfolio | holdings, positions | yes | yes |
| | position conversion (`PUT /portfolio/positions`) | — | `convert_position` |
| | holdings auctions | — | `get_auctions` |
| | holdings authorisation (CDSL TPIN, HTTP 428) | — | `authorise_holdings` |
| Quotes | full quote, OHLC, LTP | yes | yes, with Kite's 500 / 1000 instrument limits |
| | instruments dump (all / by exchange) | used internally for search | cached daily in SQLite; search and lookup tools |
| Historical | candles, every interval, `continuous`, `oi` | `get_historical_data` | same, plus automatic chunking of long ranges |
| Margins | order margins, basket margins | — | `get_order_margins`, `get_basket_margins` |
| | charges (virtual contract note) | — | `get_order_charges` |
| Mutual funds | holdings | `get_mf_holdings` | yes |
| | orders, order info, SIPs, SIP info | — | yes |
| | instruments | — | cached, searchable |
| | place/cancel order, place/modify/cancel SIP | — | yes, opt-in (see note) |
| Streaming | WebSocket ticker (ltp / quote / full, order updates) | — | managed connection, subscriptions, latest-tick cache, order-update buffer |
| | postbacks (webhook, SHA-256 checksum) | — | optional verified endpoint, fed into the same order-update buffer |

Note on mutual-fund writes: Kite's current mutual-fund page says *"Order placement can't be done, as order
placement needs payment from the user's bank account"* and documents only the read endpoints. The official
client libraries still ship the write routes (`POST /mf/orders`, `POST /mf/sips` and so on). We implement them
behind a separate opt-in flag that is off by default, run them through the same safety pipeline, and document
that Kite may reject them.

Legacy routes we leave out on purpose: `POST /session/refresh_token` (only certain approved platforms get refresh
tokens), `GET /margins/{segment}` and `GET /instruments/trigger_range` (both superseded and gone from the current
docs).

## Self-hosting with your own API key

Upstream does run with your own key (`KITE_API_KEY`, `KITE_API_SECRET`, `APP_MODE`, `PUBLIC_BASE_URL`), so it is
not tied to `mcp.kite.trade`. But its session model assumes one browser login per MCP session:

- The access token lives only in process memory, keyed by MCP session ID. A restart, a new client or a new
  session means a new browser login. You can't hand it an existing token: there's no token file and no
  `KITE_ACCESS_TOKEN`.
- Two clients (say, a desktop assistant and a scheduled agent) can't share the day's login.
- When an MCP session expires, `kc/manager.go` (`kiteSessionCleanupHook`) calls `InvalidateAccessToken()`, which
  logs that token out at Kite. If upstream were given a token another process depends on, it would kill that
  token. That alone rules out plugging it into an existing daily-login setup without changes.
- A Kite app has exactly one registered redirect URL, so upstream's `/callback` has to own it. That clashes with
  any callback service you already run.

This project supports both models. In `managed` mode the server owns the daily login: a login link, a
state-checked callback, the token written atomically with mode 600, and an evening logout if you want one. In
`external` mode it only reads a token file kept by some other process. It picks up the new token each morning
without a restart, and it never writes, deletes or logs out that token. The file format is a small JSON record
(`access_token`, `login_date`, `logged_in_at`, `expires_at`) that existing setups already use.

## Login handling

Upstream's login is careful in places: HMAC-signed redirect parameters, validated session IDs, a warning about AI
risk before the link. The weaker points:

- The `request_token` is logged at INFO: the full callback URL in `HandleKiteCallback`, and again in
  `CompleteSession`. It's single-use and short-lived, but it shouldn't be in logs.
- Nothing is persisted, so the login must be repeated per session.
- Logout happens implicitly on session cleanup.
- There's no check that the login came from the expected Kite account.

## Code quality

Good overall. It's idiomatic Go with sensible package boundaries (`app`, `kc`, `kc/instruments`, `mcp`). It has
deterministic time-based tests (`synctest`), CI and release workflows, and passes `go vet` and its own tests. The
weaknesses that matter here:

- **Tool handlers are thin and mostly untested.** The `mcp` package tests cover the helpers (argument coercion,
  pagination, exclusion), not the tools.
- **Kite's error is hidden.** For example `place_order` returns only `"Failed to place order"`, whether the cause
  was a margin shortfall, a bad tick size or an expired session. An agent can't correct a request it doesn't
  understand.
- **The parameter set is behind the API.** It lacks `market_protection`, which the docs now pass with every MARKET
  order, plus `autoslice` and `auction_number`. Without `auction_number`, auction orders can't be placed.
- **No rate limiting or backoff.** Kite allows 1 quote request/s, 3 historical requests/s and 10/s elsewhere. It
  also caps orders at 10/s, 400/min and 5000/day, and allows at most 25 modifications per order.
- **Safety is a denylist (`EXCLUDED_TOOLS`).** A write tool added upstream would be enabled by default.
- It pins `mcp-go` v0.31.0, which is far behind the current MCP spec.

## Why Python and not a Go rewrite inside the fork

1. **Who uses it.** The first consumer runs on Python and already serves its own read-only MCP server on the
   official Python SDK v2 (`mcp` 2.3.0), using the same Streamable-HTTP-plus-bearer pattern this server offers.
   Its token file can be read as it is.
2. **What we depend on.** The MCP SDK, the HTTP client it already brings (`httpx2`) and `websockets`. That's it.
   We don't use `kiteconnect` (pykiteconnect). It's synchronous, has no Alerts or holdings-authorisation calls,
   and its ticker pulls in Twisted and Autobahn. Kite's REST API is small and well documented, and a thin async
   client gives us exact control over rate limits, retries and redaction, which a trading tool needs.
3. **Testability.** Every Kite call goes through one transport, which the tests replace with a fake (no network).
   The suite proves that no write request leaves the process unless the safety pipeline says so.
4. **The host.** The deployment host has no Go toolchain. Python is already managed there.

What we give up: we don't get upstream fixes for free, and we ship a Python package instead of one static binary.
A Dockerfile and `uv tool install` keep installation to one command.

## What we borrowed from upstream

Ideas only. No code was copied.

- Tool names (`get_holdings`, `place_order`, `get_gtts`, `place_gtt_order`, …) so prompts carry over.
- Pagination for large lists (holdings, orders, trades).
- Instrument search over the daily dump.
- Showing an AI-risk warning alongside the login link.

## When to revisit

Re-evaluate if upstream gains a persistent token store and a write-safety layer, or if Zerodha's hosted server
starts exposing trading tools under per-user safety settings. At that point contributing upstream may beat
maintaining a separate server.
