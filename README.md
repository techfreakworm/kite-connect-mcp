# kite-connect-mcp

A self-hostable [Model Context Protocol](https://modelcontextprotocol.io) server for the
[Zerodha Kite Connect v3 API](https://kite.trade/docs/connect/v3/). Run it on your own machine or server with
your own Kite Connect app, and let an AI assistant read your account, market data and orders. Every write is a
**dry run by default**.

> **Status: alpha, under active development.** The core (HTTP client, token store, safety pipeline, order tools)
> is in place and tested. The remaining tool groups are being added; see [CHANGELOG.md](CHANGELOG.md).
> This is not affiliated with or endorsed by Zerodha.

## Why another Kite MCP server?

Zerodha publishes an official server ([zerodha/kite-mcp-server](https://github.com/zerodha/kite-mcp-server)) and
hosts it at `mcp.kite.trade`. This project differs in three ways:

- **It covers the whole paid Kite Connect API.** That includes alerts, GTTs, order and basket margins, charges,
  position conversion, holdings authorisation, auctions, mutual funds and the WebSocket ticker.
- **It's built for one person self-hosting.** The daily token is persisted and shared by every client you connect.
- **It has a safety layer for orders:** dry runs, two-step confirmation, value caps, allow-lists, a kill switch, an
  audit log and rate limiting.

The full evaluation and reasoning are in [docs/DECISION.md](docs/DECISION.md).

## What you need

- A Zerodha account, and a Kite Connect app from the [developer console](https://developers.kite.trade).
  - **Connect (paid, ₹500/month per API key)** includes live quotes, the WebSocket and historical candles.
  - **Personal (free)** covers orders, GTTs, alerts, portfolio and margins, but no market data. Market-data tools
    return a clear "needs the paid plan" error.
- Python 3.11+ (or Docker).
- To place **live** orders: the order requests must come from a **static IP registered in the developer console**.
  This is a SEBI/NSE rule, in force since April 2026. Reads work from any IP.

The developer-console steps are in [docs/KITE_SETUP.md](docs/KITE_SETUP.md): creating the app, the API key and
secret, the redirect and postback URLs, the subscription, and the static IP.

## Quick start (local, stdio)

```bash
# install (from GitHub; no PyPI release yet)
uv tool install git+https://github.com/techfreakworm/kite-connect-mcp
# or: pipx install git+https://github.com/techfreakworm/kite-connect-mcp

# configure: credentials from env vars or files (mode 600)
export KITE_API_KEY=your_api_key
export KITE_API_SECRET_FILE=~/.config/kite-connect-mcp/api_secret

kite-connect-mcp check-config   # validates settings, never prints credentials
kite-connect-mcp login --listen # prints the Kite login link and waits for the redirect (managed mode)
kite-connect-mcp status         # session and safety status
```

For `login --listen`, register `http://127.0.0.1:8765/callback` as your app's redirect URL. Every setting is listed in
[docs/CONFIGURATION.md](docs/CONFIGURATION.md).

Add it to an MCP client, e.g. Claude Desktop (`claude_desktop_config.json`) or any client that launches stdio servers:

```json
{
  "mcpServers": {
    "kite": {
      "command": "kite-connect-mcp",
      "args": ["serve"],
      "env": { "KITE_API_KEY": "your_api_key", "KITE_API_SECRET_FILE": "/home/you/.config/kite-connect-mcp/api_secret" }
    }
  }
}
```

To run it on a server over HTTP, see [docs/SELF_HOSTING.md](docs/SELF_HOSTING.md). It covers systemd and Docker,
a reverse proxy with TLS, bearer-token auth, and the login callback.

## The safety model, in short

| Guard | Default |
|---|---|
| Every write tool takes `dry_run` | `true`: validate, run the checks, preview the exact request and Kite's margin calculation; send nothing |
| Live trading switch | off (`KITE_MCP_TRADING_MODE=dry_run`) |
| Confirmation | a live write needs the single-use `confirmation_token` from a dry run of the *identical* call (or, if you opt in, an explicitly allow-listed instrument) |
| Value caps | ₹10,000 per order, ₹50,000 per day, 20 live orders per day |
| Allow-lists | instruments, exchanges, products, varieties, order types, sides (all optional) |
| Read-only mode | `KITE_MCP_READ_ONLY=true` hides every write tool, and the HTTP client refuses write endpoints too |
| Kill switch | a file; a tool can switch it **on**, only a person can switch it off |
| Audit log | every dry run, refusal, live send and result, as JSON lines; the intent is fsynced *before* sending |
| Rate limits | kept under Kite's 1/s (quotes), 3/s (historical), 10/s (orders and the rest), 400 orders/min |
| Retries | reads back off and retry; **writes are never retried**. A timeout is reported as an unknown outcome: check the order book before trying again |

Details: [docs/SAFETY.md](docs/SAFETY.md).

## Sessions and the daily login

Kite access tokens expire every day at about **06:00 IST**. A request token is single-use. Logging in again with the
same API key invalidates the previous access token. So one app should have exactly one program that owns the daily
login.

- **`managed` mode (default).** This server owns the login. The `login` tool (or `kite-connect-mcp login`) gives you
  the link. Kite redirects to this server's `/callback`, which checks the one-time `state`, exchanges the
  `request_token`, and stores the token with mode 600.
- **`external` mode.** Another program owns the login and writes a token file. This server only reads that file,
  picks up the new token every morning without a restart, and never writes, deletes or logs out that token.

## Tools

Tool names follow the official server where one exists, so prompts carry over: `get_profile`, `get_margins`,
`get_holdings`, `get_positions`, `place_order`, `modify_order`, `cancel_order`, `get_orders`, `get_trades`,
`get_gtts`, `place_gtt_order`, `get_quotes`, `get_ltp`, `get_ohlc`, `get_historical_data`, `search_instruments`, …
The full reference with every parameter is [docs/TOOLS.md](docs/TOOLS.md). `kite-connect-mcp tools` lists what
your configuration exposes.

## Development

```bash
git clone https://github.com/techfreakworm/kite-connect-mcp && cd kite-connect-mcp
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest            # never touches the network: Kite is faked in-process
.venv/bin/ruff check . && .venv/bin/ruff format --check .
```

See [CONTRIBUTING.md](CONTRIBUTING.md). Security reports: [SECURITY.md](SECURITY.md).

## Disclaimer

Trading involves risk. This software can place real orders on a real brokerage account when you configure it to.
AI systems are unpredictable: keep the caps low, use dry runs, and review what you confirm. The software is provided
"as is", without warranty of any kind (see [LICENSE](LICENSE)). You are responsible for complying with Zerodha's
terms, SEBI's rules (including the static-IP and algo rules) and exchange data policies. Kite Connect data may not
be redistributed.

## Licence

[MIT](LICENSE) © 2026 Mayank Gupta
