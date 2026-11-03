# Setting up Kite Connect

This guide takes you from "I have a Zerodha account" to "kite-connect-mcp can read my account and, if I choose, send
orders". It covers Zerodha's side only: the plan, the app, the URLs, the daily login, the limits and the rules that
affect this server. To install and run the server, see [SELF_HOSTING.md](SELF_HOSTING.md). To see how orders are
guarded, see [SAFETY.md](SAFETY.md).

> Verified on 2026-11-03 against the Zerodha pages listed under [Sources](#sources). Zerodha changes plans, prices and
> rules; check those pages before you pay for anything or rely on a number here.

You need a Zerodha trading account with two-factor (TOTP) login enabled, which Kite Connect requires, and a few
minutes with the [developer console](https://developers.kite.trade).

## Static IP for live orders

> **Warning: live orders need a registered static IP.** Since 1 April 2026, an order placed through the API must come
> from a static IP address that you have registered in the Kite developer console (NSE/SEBI algorithmic trading
> rules). Orders from any other address are rejected. **Only order placement is restricted.** Reading orders,
> positions, holdings and funds, streaming over the WebSocket, and every other API call work from any IP.
>
> If your home IP changes, you can still use everything in this server except live order placement. Dry runs, reads
> and research all work.

**Register the address**

1. Open the [developer console](https://developers.kite.trade) and go to Profile, then IP Whitelist.
2. Enter the primary IP (mandatory). Optionally add a secondary IP. One address per line; IPv4 and IPv6 are both
   accepted; two addresses at most.
3. Choose Update.

**Rules to know before you press Update**

| Rule | What it means |
|---|---|
| One change per calendar week | You cannot fix a typo or move to another server for up to a week. Check the address twice. |
| Takes effect immediately | There is no waiting period after an update. |
| Per developer account | The list is shared by every app under that developer account. |
| Your own address, or your family's | Updating confirms a declaration that the addresses are used exclusively by you and/or your immediate family (spouse, dependent children, dependent parents). Do not register a shared VPN exit, a shared NAT gateway or a machine other people use. Family members who trade through the API should use separate apps under one developer account. |
| Register what Kite sees | Kite sees the address your traffic leaves from. Behind NAT or a shared gateway that is not the address on the machine's interface. See the options below. |

**Good to know**

- Forum reports put the SEBI threshold for exchange registration and an algo ID at 10 orders per second; strategies
  at or below it do not need registration. Treat this as reported, not as confirmed by Zerodha's pages. This server's
  rate limiter keeps orders at no more than 10 per second.
- Kite's published limits cap orders at 10/s, 400/min and 5000/day per user. See [Rate limits](#8-rate-limits).

**Ways to get a static IP**

| Option | Notes |
|---|---|
| A VPS with a static public IP | The simplest route. Most providers give a fixed public IPv4 address (some charge a small fee for a reserved one). Run the server there and register that address. Check which address your outbound traffic uses: if the machine has IPv6 and its outbound connections use it, register the IPv6 address as well, or make outbound traffic prefer IPv4. |
| A home connection with a static IP from your ISP | Ask your ISP for a static public IP. Many home connections sit behind carrier-grade NAT, where the public address is shared and can change; a static IP usually needs a specific plan or add-on. Confirm that the address you register is the one a web service sees when you connect from home. |
| A static-IP egress proxy | Run the server anywhere and send its requests to Kite through a small forward proxy on a machine with a static IP. Set the standard `HTTPS_PROXY` variable (for example `HTTPS_PROXY=http://203.0.113.10:3128`) in the server process's environment; its HTTP client honours it. Register the proxy's address. A normal CONNECT proxy relays the encrypted traffic without being able to read it, but you must still control and secure the proxy so that nobody else can use it. |
| A dynamic IP | Everything except live order placement works. Use dry runs, and place orders in the Kite app or web terminal. |

How to run the server on a VPS is in [SELF_HOSTING.md](SELF_HOSTING.md).

## 1. Choose a plan

Sign up at [developers.kite.trade/signup](https://developers.kite.trade/signup) and choose a plan. Plans apply per app,
that is per API key.

| | Personal | Connect |
|---|---|---|
| Price | Free | ₹500 per month per API key |
| Orders: place, modify, cancel, order book, trades | Yes | Yes |
| GTT and alerts | Yes | Yes |
| Portfolio: holdings and positions | Yes | Yes |
| Margin calculation | Yes | Yes |
| Live market data: quotes, LTP, OHLC | No: `PermissionException` ("Insufficient permission") | Yes |
| WebSocket streaming | No | Yes |
| Historical candles | No: `PermissionException` | Yes, at no separate fee |

- Startups building mass retail products may be able to get Connect free; contact Zerodha.
- Zerodha has announced a credit scheme for paid users: from October 2026, a user with at least ₹2,000 brokerage in a
  calendar month receives 500 developer credits, usable towards the next month's subscription. Check the current
  terms on Zerodha's pages before relying on it.
- There is no separate fee for historical data on Connect.

### What this server does on each plan

| Tool group | Personal | Connect |
|---|---|---|
| `session`: `session_status`, `login`, `complete_login`, `logout` | Works | Works |
| `account`: `get_profile`, `get_margins` | Works | Works |
| `orders`: order book, history, trades, `place_order`, `modify_order`, `cancel_order` | Works. LIMIT, SL and SL-M orders can go live; MARKET orders cannot (see below) | Works |
| `gtt`: `get_gtts`, `place_gtt_order`, and so on | Works | Works |
| `alerts`: `get_alerts`, `create_alert`, and so on | Works | Works |
| `portfolio`: holdings, positions, `convert_position`, `get_auctions`, `authorise_holdings` | Works | Works |
| `margins`: `get_order_margins`, `get_basket_margins`, `get_order_charges` | Works | Works |
| `market`: `get_quotes`, `get_ohlc`, `get_ltp`, `get_historical_data` | Return a clear "needs the paid plan" error | Works |
| `market`: `search_instruments`, `get_instrument`, `get_instruments_status`, `refresh_instruments` | Expected to work: they use the daily instrument list, which is not among the calls Zerodha says are blocked. Confirm on your account | Works |
| `ticker`: WebSocket subscriptions, ticks and order updates | Not available: Personal apps cannot stream WebSocket data | Works |
| `mf`: mutual-fund reads | Not covered by Zerodha's plan summary; try `get_mf_holdings` | Works |
| `safety`: `get_safety_status`, `get_audit_log`, `engage_kill_switch` | Works | Works |

Placing a **live** order on either plan also needs the [static IP](#static-ip-for-live-orders).

**MARKET orders on Personal.** The server values a MARKET order from the last traded price (see
[SAFETY.md](SAFETY.md#6-value-rules-and-caps)), and the last traded price needs the quote API. Without it the value
cannot be verified, so the order passes as a dry run but is refused live. Use LIMIT (or SL, or SL-M, whose value comes
from the trigger price) on Personal.

**Mutual-fund writes.** Kite's current mutual-fund page lists read endpoints only and says: "Order placement can't be
done, as order placement needs payment from the user's bank account". This server's mutual-fund write tools
(`place_mf_order`, `cancel_mf_order`, `place_mf_sip`, `modify_mf_sip`, `cancel_mf_sip`) are therefore off unless you
set `KITE_MCP_ALLOW_MF_WRITES=true`, and Kite may reject them.

## 2. Create the app

1. Sign in to the [developer console](https://developers.kite.trade) and open My Apps, then Create New App.
2. Fill in the form:

   | Field | What to enter |
   |---|---|
   | App name | Any name you will recognise, for example `kite-connect-mcp`. |
   | Zerodha client ID | Your own client ID, the one you log in to Kite with (for example `AB1234`). |
   | Redirect URL | Where Kite sends your browser after you log in. See [Redirect URL](#4-redirect-url). |
   | Postback URL | Optional. Where Kite posts order updates. See [Postback URL](#5-postback-url-optional). Leave it empty if you are not sure. |

3. Create the app. The console shows its **API key** and **API secret**.

Use one app per way of running the server. A Kite app has exactly one redirect URL, so a copy on your laptop and a copy
on a server cannot share an app. A second app on the free Personal plan is a cheap way to experiment.

## 3. API key and secret

- The **API key** identifies your app. It is not secret, but there is no reason to publish it.
- The **API secret** proves that a login exchange comes from you. Keep it private: never commit it, never paste it into
  a chat or an issue, and never put it in a client-side app.
- Give both to the server as `KITE_API_KEY` and `KITE_API_SECRET_FILE` (a file with mode 600). See
  [SELF_HOSTING.md](SELF_HOSTING.md#configure) and [CONFIGURATION.md](CONFIGURATION.md).
- Your Zerodha password and TOTP code never reach this server. You enter them on `kite.zerodha.com` in your browser.

If you think the secret has leaked, rotate it in the developer console. If you cannot, delete the app and create a new
one; deleting immediately invalidates the key, the secret and all tokens (see [Billing](#6-subscription-and-billing)).

## 4. Redirect URL

The redirect URL is where Kite sends your browser after a successful login. It adds `request_token` to the URL, along
with `action=login`, `status=success` and any `redirect_params` that the login link carried. A Kite app has one
redirect URL.

For this server, use one of these:

| Where the server runs | Redirect URL | How the login completes |
|---|---|---|
| A server behind a TLS reverse proxy | `https://<host>/callback`, for example `https://kite.example.com/callback` | The running server handles `GET /callback`. Set `KITE_MCP_PUBLIC_URL=https://<host>`. |
| Your own machine | `http://127.0.0.1:8765/callback` | Run `kite-connect-mcp login --listen`: it starts a temporary local callback and waits for the redirect. |

- The path comes from `KITE_MCP_CALLBACK_PATH` (default `/callback`). If you change it, change the registered URL too.
- The registered URL and the one your server answers on must match exactly: scheme, host, port and path.
- Use HTTPS for anything that is not loopback. The URL carries one-time values.
- If you also run another program that logs in with the same API key, only one program can own the redirect URL. See
  [Why one program owns the login](#why-one-program-owns-the-login).

## 5. Postback URL (optional)

Kite sends a POST with a JSON body to the postback URL whenever the status of an order changes (COMPLETE, CANCEL,
REJECTED, or UPDATE for a modification or partial fill). It is optional. Without it you can poll `get_orders`, or on the
Connect plan read order updates from the WebSocket.

- For this server: `https://<host>/postback`, plus `KITE_MCP_POSTBACK_PATH=/postback` in the server's settings. Without
  that setting the endpoint does not exist.
- The endpoint accepts a postback only if its checksum is right. The checksum is SHA-256 of
  `order_id + order_timestamp + api_secret`; only Kite and you know the secret.
- Accepted updates go into the same buffer as WebSocket order updates, which `ticker_order_updates` reads.
- Postbacks are sent only for orders placed with this app's API key. Zerodha recommends WebSocket order updates for
  individual developers, because those cover orders placed anywhere (web, mobile, other apps). Personal apps cannot
  stream WebSocket data, so there the postback URL is the only way to be told about order changes without polling
  `get_orders`.
- Postbacks arrive even when you are not logged in.

## 6. Subscription and billing

- Pay in the Billing section of the developer console, either by linking your Kite account for automatic renewal or
  through Razorpay.
- Cancelling the subscription keeps the app and its configuration.
- Deleting the app asks you to type `I UNDERSTAND`. It immediately invalidates the API key, the secret and every token.
- The Connect fee is per API key. See [Choose a plan](#1-choose-a-plan) for the plans and the credit scheme.

## 7. The daily login

Kite does not give out long-lived tokens. You log in once per trading day, in a browser, and the server keeps the
result.

**The flow, step by step** (managed mode, where this server owns the login)

1. You ask for a login link: tell the assistant to call the `login` tool, or run `kite-connect-mcp login`.
2. The server answers with a link to `kite.zerodha.com` that carries your API key and a one-time `state` that only this
   server knows.
3. You open the link in a browser (your phone is fine) and log in with your Zerodha user ID, password and TOTP.
4. Kite redirects the browser to your [redirect URL](#4-redirect-url) with a `request_token`.
5. The server's `/callback` checks the `state`, then exchanges the `request_token` for an access token by calling
   `POST /session/token` with a checksum of `api_key + request_token + api_secret`.
6. If you set `KITE_MCP_ALLOWED_USER_ID`, the server refuses a login from any other Kite account.
7. The server stores the access token in its state directory with mode 600. Every client connected to the server now
   uses it, and the server picks it up again after a restart.
8. The token stops working at about 06:00 IST the next morning, before the market opens. Repeat from step 1.

**Facts that shape this**

- The **request token** is single-use and valid for a few minutes. Exchange it immediately.
- The **access token** is valid for the trading day and expires the next morning, around 06:00 IST. It also stops
  working if it is invalidated through the logout call or by a master logout from the Kite web terminal.
- **A new session with the same API key invalidates the previous access token.** Two programs that each log in with
  your key will keep killing each other's sessions.
- The login must be done by you, manually, once a day. Zerodha discourages automating it, and this server does not try:
  it never handles your password or TOTP.
- Times are IST throughout. `session_status` (and `kite-connect-mcp status`) tell you when the current token expires.

### Why one program owns the login

Because a new login kills the old token, exactly one program should perform the daily login for an API key. This server
supports both arrangements:

| Mode | Who logs in | What this server does |
|---|---|---|
| `managed` (default) | This server | Creates the login link, handles the callback, stores and (optionally) ends the session. |
| `external` | Another program you already run | Only reads the token file that program writes. It picks up the new token each morning without a restart, and it never writes, deletes or logs out that token. |

Set `KITE_MCP_AUTH_MODE=external` and point `KITE_TOKEN_FILE` at the file. See
[SELF_HOSTING.md](SELF_HOSTING.md#sharing-a-token-with-another-program-external-mode). In `external` mode the redirect
URL belongs to the other program.

## 8. Rate limits

| Endpoint group | Kite's limit |
|---|---|
| Quote (`/quote`, `/quote/ohlc`, `/quote/ltp`) | 1 request per second |
| Historical candles | 3 requests per second |
| Order placement | 10 requests per second |
| Everything else | 10 requests per second |
| Orders, all segments and varieties | 400 per minute, 10 per second and 5000 per day per user |
| Modifications | 25 per order; after that cancel and place a new order |

- The 10 orders-per-second limit applies per client trading account, not per app. Requests above it get HTTP 429, and
  invalid requests count too. An iceberg order counts as one order.
- If another program trades on the same account, you share that budget, and this server cannot see the other program's
  requests.
- **What this server does.** It keeps its own requests under these limits with sliding windows that are 10% longer than
  Kite's. Reads that still get a 429 (or a 502, 503 or 504) are retried a few times with backoff, honouring
  `Retry-After`. Writes are never retried; see [SAFETY.md](SAFETY.md#13-rate-limiting-retries-and-outcome-unknown).
- The server's own daily order cap (`KITE_MCP_MAX_ORDERS_PER_DAY`, default 20) is a safety setting. It is far below
  Kite's 5000.

## 9. Enabling segments on your trading account

An API app does not enable trading segments. The orders you place can only succeed in segments that are active on your
Zerodha trading account:

- Equity (NSE, BSE) is on for a normal account.
- F&O (NFO, BFO), currency (CDS, BCD) and commodity (MCX) are switched on separately in your Zerodha account settings,
  usually after submitting income proof. Follow the current steps on [support.zerodha.com](https://support.zerodha.com).
- Check what is active with `get_profile`: it lists the exchanges, products and order types enabled for your account.
  An order in a segment that is not listed will be rejected by Kite.

Order values for MCX, CDS and BCD cannot be verified against this server's caps, so those orders are dry-run only; see
[SAFETY.md](SAFETY.md#6-value-rules-and-caps).

## 10. Data redistribution

Kite Connect data comes from the exchanges under data policies that do not allow redistribution. Use quotes, ticks and
candles for yourself. Do not publish them, serve them to other people, sell them, or put them in a public dashboard,
dataset or bot. Giving other people access to your server, for example by sharing its bearer token, can amount to the
same thing, so keep that access to yourself; the server is built for one person's account. You are responsible for
following Zerodha's terms and the exchange data policies.

## 11. Common errors

| What you see | Cause | Fix |
|---|---|---|
| `TokenException` (HTTP 403), "Kite rejected the session", or `session_status` says expired or not logged in | The token expired at 06:00 IST, you or something else logged out, or another login with the same API key replaced it | Log in again with the `login` tool or `kite-connect-mcp login`. If it recurs during the day, another program is logging in with your key: find it, or use `external` mode with that program as the owner. |
| `PermissionException` "Insufficient permission" on quotes, LTP, OHLC or historical data; no WebSocket | The app is on the free Personal plan | Move the app to Connect, or avoid the market-data tools and use LIMIT orders. See [Choose a plan](#1-choose-a-plan). |
| An order is refused with HTTP 403 and a message about the IP address | The request did not come from a whitelisted IP | Register the address Kite sees under Profile, then IP Whitelist (one change per calendar week). Check IPv4 against IPv6 and any NAT gateway. A 403 with a session message is a `TokenException` instead; read the text. See [Static IP](#static-ip-for-live-orders). |
| HTTP 429 "Too many requests" | Over a rate limit, possibly shared with another program on the same account; invalid requests count | Slow down. Reads are retried by the server. A write is never retried: check the order book before sending it again. |
| `InputException` | Missing or bad parameters: a LIMIT order without a price, a price off the tick size, a quantity that is not a multiple of the lot size, a bad `market_protection`, an unknown symbol | Read the message. The server pre-checks most of these and says so in the dry run. Find the exact symbol and lot size with `search_instruments`. |
| `MarginException` | Not enough funds for the order | Check `get_margins` and `get_order_margins`. |
| HTTP 428, "needs a CDSL authorisation" | A sell of delivery holdings needs your TPIN authorisation first | Call `authorise_holdings` and open the link it returns. |
| "no answer from Kite ... the request may have been executed" | A write timed out or the connection dropped after it was sent | Do not repeat it. Check `get_orders` (or `get_gtts` or `get_alerts`) first. The audit log has a `live_unknown` entry for it. |

## Sources

Verified on 2026-11-03; check the linked pages for changes.

- [Static IP](https://support.zerodha.com/category/trading-and-markets/general-kite/kite-api/articles/static-ip):
  the whitelist rules.
- [Kite Connect API FAQs](https://support.zerodha.com/category/trading-and-markets/general-kite/kite-api/articles/kite-connect-api-faqs):
  plans, billing, limits, tokens.
- [Historical data and live market data payment plan](https://support.zerodha.com/category/trading-and-markets/general-kite/kite-api/articles/historical-data-and-live-market-data-payment-plan):
  what each plan includes.
- [Kite Connect v3 documentation](https://kite.trade/docs/connect/v3/): the API itself, including
  [user and login](https://kite.trade/docs/connect/v3/user/), [orders](https://kite.trade/docs/connect/v3/orders/),
  [postbacks](https://kite.trade/docs/connect/v3/postbacks/), [WebSocket](https://kite.trade/docs/connect/v3/websocket/),
  [mutual funds](https://kite.trade/docs/connect/v3/mutual-funds/) and
  [exceptions and rate limits](https://kite.trade/docs/connect/v3/exceptions/).

This project is not affiliated with or endorsed by Zerodha.
