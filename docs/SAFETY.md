# The safety model

kite-connect-mcp can place real orders on a real brokerage account, and the thing calling it is an AI model that can
misread, be misled, or simply be wrong. The safety layer assumes that. Every rule below is enforced by the server's
code, not by instructions to the model, and the key invariants are pinned by tests (no write request leaves the process
unless the write pipeline approves it).

Read this with [CONFIGURATION.md](CONFIGURATION.md), which lists every setting, and
[SELF_HOSTING.md](SELF_HOSTING.md), which covers protecting the machine and the HTTP endpoint.

## 1. At a glance

| Guard | Default | Setting |
|---|---|---|
| Every write tool takes `dry_run` | `true`: validate, check, preview; send nothing | tool argument |
| Live trading switch | off | `KITE_MCP_TRADING_MODE=dry_run` |
| Confirmation of a live write | `token`: a single-use token from a dry run of the identical call, valid 300 s | `KITE_MCP_LIVE_CONFIRMATION`, `KITE_MCP_CONFIRMATION_TTL_S` |
| Per-order value cap | ₹10,000 | `KITE_MCP_MAX_ORDER_VALUE` |
| Daily value cap | ₹50,000 | `KITE_MCP_MAX_DAILY_VALUE` |
| Daily live order count | 20 | `KITE_MCP_MAX_ORDERS_PER_DAY` |
| Quantity cap | none | `KITE_MCP_MAX_QUANTITY` |
| Allow-lists | none (anything goes) | `KITE_MCP_ALLOWED_INSTRUMENTS`, `_EXCHANGES`, `_PRODUCTS`, `_VARIETIES`, `_ORDER_TYPES`, `_TRANSACTION_TYPES` |
| Read-only mode | off | `KITE_MCP_READ_ONLY` |
| Kill switch | off (no file) | `KITE_MCP_KILL_SWITCH_FILE` |
| Mutual-fund writes | off | `KITE_MCP_ALLOW_MF_WRITES` |
| Audit log | always on | `<state dir>/audit/` |
| Rate limits, no write retries | always on | none |

## 2. How a write is processed

Every tool that changes the account (orders, GTTs, alerts, position conversions, holdings authorisations, and the
opt-in mutual-fund writes) builds a request and hands it to one pipeline. Nothing else can reach Kite's write
endpoints.

1. **Validate.** The tool checks what it can locally (required prices, tick and lot sizes, `market_protection`, iceberg
   and auction fields, `CNC` on derivatives) and fails before anything is sent or logged if Kite would certainly
   reject the request.
2. **Estimate the value** of each order the write would create or change (see
   [Value rules and caps](#6-value-rules-and-caps)).
3. **Policy.** Allow-lists, caps, the kill switch and (for modifications) the 25-modification limit are evaluated
   against today's usage, which is read back from the audit log. The result is a list of violations (they block) and
   warnings (they do not).
4. **Dry run?** With `dry_run=true` (the default) the pipeline answers with the exact request it would send, the
   checks, the value of each leg, today's usage, the limits, Kite's own margin and charges calculation where the tool
   provides one, and, only if live trading is on and every check passed, a `confirmation_token`. Then it stops.
5. **Live gate.** With `dry_run=false`, the pipeline refuses unless live trading is enabled, there are no violations,
   and a valid confirmation is present (or the instrument is pre-authorised in `allowlist` mode).
6. **Audit the intent**, fsynced to disk **before** anything is sent.
7. **Send once** with a single-use write ticket bound to exactly this method, path and payload. Never retried.
8. **Audit the outcome** (`live_result`, `live_error` or `live_unknown`).

Live writes are serialised: one at a time, so two concurrent calls cannot both slip under a cap.

## 3. Dry run is the default, and the trading mode

There are two independent switches, and a live write needs both.

- **The tool argument.** Every write tool defaults to `dry_run=true`. The model has to say `dry_run=false` explicitly.
- **The server's trading mode.** `KITE_MCP_TRADING_MODE` is `dry_run` unless you change it to `live`. In `dry_run` mode
  a call with `dry_run=false` is refused (and audited as `refused`) with "live trading is disabled", whatever the model
  does. Setting `KITE_MCP_READ_ONLY=true` overrides `live`.

A dry run is not harmless noise: it is a real preview. It may call Kite for read-only information (the order margin
calculator, the last price for a MARKET order, the current state of an order you are modifying or cancelling), but it
never calls a write endpoint. Each dry run is audited as `dry_run` or `confirmation_issued`.

## 4. Two-step confirmation

In the default `token` mode, a live write needs a confirmation token from a dry run of **the identical call**.

1. The model calls the write tool with `dry_run=true`. If live trading is on and all checks pass, the answer contains
   `confirmation_token` and `confirmation_expires_at`.
2. The model (or you) reviews the preview. Then the model repeats the **same arguments** with `dry_run=false` and
   `confirmation_token=<the token>`.

What the token guarantees:

- **Bound to the exact request.** It stores the tool name and a SHA-256 digest of the method, path, query, form and
  JSON payload that would be sent. Change the price, the quantity, the symbol or any other field and the digest differs;
  the call is refused with "the confirmation token was issued for a different request".
- **Single use.** It is consumed when the live call is accepted by the policy, whether or not Kite then accepts the
  order. A replay is refused with "unknown, used or expired". After a failed send, run a new dry run for a new token.
- **Short lived.** `KITE_MCP_CONFIRMATION_TTL_S` (default 300 seconds, minimum 10).
- **In memory only.** Restarting the server forgets every token. At most 256 are outstanding; the oldest are dropped.
- **Re-checked.** The policy runs again at send time, with the usage as it is then. A token does not carry approval
  past a cap, a changed allow-list or a newly engaged kill switch.

The token proves that the model saw a preview of this exact request. It does not prove that a human did. See
[What is not protected](#15-what-is-not-protected).

## 5. The allowlist confirmation mode

`KITE_MCP_LIVE_CONFIRMATION=allowlist` removes the token step for chosen instruments.

- It requires an explicit `KITE_MCP_ALLOWED_INSTRUMENTS` list (not `*`); the server refuses to start otherwise.
- A live write whose **every** leg names an instrument on that list is sent straight away with `dry_run=false`, with no
  token and no prior dry run. The dry run, if the model does one, says "every leg is allow-listed, so no confirmation
  token is needed".
- Everything else still applies to it: the caps, all the allow-lists, the kill switch, read-only mode and the audit log.
- The list is also the allow-list of instruments, so an instrument outside it is refused outright, not sent for
  confirmation. The token step remains only for writes that name no instrument at all.

This mode is for unattended agents. It means the model can send orders in those instruments, within the caps, without
anyone looking. Use it only with a short list, low caps and tight allow-lists (see
[Recommended configurations](#16-recommended-configurations)).

## 6. Value rules and caps

The caps are measured in rupees of notional value: how much market value the order would put in play. For each order
the server computes a value like this (from `tools/_orders_common.py`):

| Order type | Value used | Notes |
|---|---|---|
| `LIMIT` | `quantity × price` | |
| `SL` | `quantity × price` | The limit price, not the trigger price. |
| `SL-M` | `quantity × trigger_price × (1 + buffer)` | |
| `MARKET` | `quantity × LTP × (1 + buffer)` | The last traded price comes from Kite's quote API, which needs the Connect plan. |

The **buffer** is `market_protection` per cent when you give a value above 0, and **5%** otherwise (when
`market_protection` is `-1`, meaning automatic protection, or is omitted). Values are rounded to the paisa.

| Example | Value |
|---|---|
| BUY 2 INFY, LIMIT at 1500 | 2 × 1500 = ₹3,000.00 |
| BUY 10, SL-M, trigger 100, `market_protection=-1` | 10 × 100 × 1.05 = ₹1,050.00 |
| BUY 10, SL-M, trigger 100, `market_protection=2` | 10 × 100 × 1.02 = ₹1,020.00 |
| BUY 2, MARKET, LTP 1500, `market_protection=2` | 2 × 1500 × 1.02 = ₹3,060.00 |

**When the value cannot be known, the order cannot go live.** It can still be dry-run, and the preview says why:

- **MCX, CDS and BCD orders.** Their quantities are lots, and the contract multiplier is not in the instrument dump, so
  the value is unverifiable ("MCX quantities are lots with a contract multiplier that is not in the instrument dump").
- **MARKET orders without a last price.** On the Personal plan, or if the quote call fails or the symbol is unknown.
  Use LIMIT.
- **A modification of an order not found in today's order book.**

**The checks** (each produces a violation that blocks the write; a value exactly equal to a cap passes):

| Check | Setting | Applies to |
|---|---|---|
| Order value above the per-order cap | `KITE_MCP_MAX_ORDER_VALUE` (₹10,000) | Each order, including a modification's new value |
| Quantity above the cap | `KITE_MCP_MAX_QUANTITY` (none) | Each order |
| Live orders today plus this one above the count | `KITE_MCP_MAX_ORDERS_PER_DAY` (20) | New orders |
| Value placed today plus this order above the total | `KITE_MCP_MAX_DAILY_VALUE` (₹50,000) | New orders |

**Daily caps survive restarts.** Today's usage (the IST calendar day) is computed from the audit log: the count and
value of `live_result` events marked as counting. Nothing is held in memory, so a restart does not reset them, and every
client of the server shares them. Deleting today's audit file resets them, which is why that folder must be protected.

Details that matter:

- `autoslice=true` counts as **10** orders against the daily count, because Kite may split the order into up to 10. The
  value is the full quantity.
- **Cancellations and deletions** (cancel an order, delete a GTT or alerts, cancel an MF order or SIP) skip the value,
  quantity and daily checks, because they reduce risk. They still need confirmation, and the kill switch still refuses
  them. Only the **exchange and instrument** allow-lists apply to them, so you can still cancel an `SL-M` stop with
  `KITE_MCP_ALLOWED_ORDER_TYPES=LIMIT` set. Cancelling a stop-loss can increase your risk, which is why it is still a
  confirmed write.
- **Modifications** are checked against the per-order cap using the new value, against the allow-lists and against the
  quantity cap. They do not count as new orders, but any **increase** in value is added to the day's value and checked
  against the daily cap (a GTT modification likewise adds its increase over the stored trigger, and an ATO alert
  modification its whole new basket).
- The other tools that create or change orders (GTTs, and alerts that trigger orders) are valued with the same rules.

## 7. The 25-modification limit

Kite allows at most 25 modifications per order. The server counts the successful live `modify_order` calls for an order
in today's audit file, and once the count reaches 25 it blocks further modifications with "cancel it and place a new
one". Kite enforces the same limit itself; the server's count covers only modifications made through it today, so it
spares you a rejected request but Kite remains the final judge.

## 8. Allow-lists

Six optional lists narrow what the server will send. Each is comma-separated; unset, empty or `*` means anything.

| Setting | Matches | Example |
|---|---|---|
| `KITE_MCP_ALLOWED_INSTRUMENTS` | `EXCHANGE:TRADINGSYMBOL`, case-insensitive | `NSE:INFY,NSE:TCS` |
| `KITE_MCP_ALLOWED_EXCHANGES` | exchange | `NSE,BSE` |
| `KITE_MCP_ALLOWED_PRODUCTS` | product | `CNC` |
| `KITE_MCP_ALLOWED_VARIETIES` | variety (lower-cased) | `regular,amo` |
| `KITE_MCP_ALLOWED_ORDER_TYPES` | order type | `LIMIT,SL` |
| `KITE_MCP_ALLOWED_TRANSACTION_TYPES` | side | `BUY` |

A write that falls outside a list is refused, and the message names the list. It shows in a dry run as a violation, so
no confirmation token is issued. The lists apply to every order a write creates or changes; cancellations and
deletions are judged by the exchange and instrument lists only (see above).

## 9. The kill switch

The kill switch is a file. While it exists, **every write is refused**: dry runs still run, but report the violation
and issue no token, and live sends fail.

- Location: `KITE_MCP_KILL_SWITCH_FILE`, default `<state dir>/KILL_SWITCH`. `kite-connect-mcp check-config` shows the
  path; `kite-connect-mcp status` shows whether it is on.
- **A tool can only turn it on.** `engage_kill_switch(reason)` creates the file (mode 600) containing the time and the
  reason, and writes a `kill_switch_engaged` audit event. No tool removes it. A person switches it off by deleting the
  file on the server (`rm` the path above).
- You can engage it yourself with `touch` on the same path, even while the server is running; it is checked on every
  write attempt.
- It stops writes made **through this server**. It does not cancel orders that are already open at Kite, and cancelling
  through this server is a write too, so it is refused while the switch is on. Cancel from the Kite app or web terminal.
- The tool stays available in read-only mode.

## 10. Read-only mode

`KITE_MCP_READ_ONLY=true` is the strongest setting short of not running the server.

- **No write tool is registered.** The model does not see `place_order` and the rest at all.
- **The HTTP client refuses write endpoints**, even if some code path produced a valid ticket. This is the second line
  of defence.
- **The pipeline refuses too**, and audits `refused`, as a third.
- **Calculators still work.** Order margins, basket margins and charges are POSTs that only compute, and they are
  allowed.
- `engage_kill_switch`, the audit log and the safety status stay available. The managed login still works: reading
  needs a session.
- Read-only overrides `KITE_MCP_TRADING_MODE=live`.

A read-only server needs neither the Connect plan (unless you want market data) nor the [static IP](KITE_SETUP.md#static-ip-for-live-orders).

## 11. The door: an endpoint allow-list

Every request the HTTP client sends must match an entry in a fixed table of Kite endpoints (method and path pattern),
each classed as `read`, `calc` (a computing POST), `auth` (the token exchange and logout) or `write`. An unknown path is
refused before any network I/O. A `write` is sent only with a ticket that the pipeline minted for exactly that method,
path and payload digest; the ticket works once. Tool modules cannot mint tickets, and tests assert it. This is why a bug
in a tool cannot, by itself, send an unreviewed write.

## 12. The audit log

- **Where:** `<state dir>/audit/audit-YYYY-MM-DD.jsonl`, one file per IST day. The folder is mode 700, the files mode
  600, and the server only appends to them.
- **Format:** one compact JSON object per line. Every event has `ts` (IST, with milliseconds) and `event`. Events of a
  write tool also carry `call_id`, `tool`, the exact `request` (method, path, query, form or JSON) and a one-line
  `summary`. Credentials are never written: the request holds order parameters only.

| Event | Written when | Extra fields |
|---|---|---|
| `dry_run` | A dry run that issued no token | `ok`, `violations`, `warnings`, `legs` |
| `confirmation_issued` | A dry run that issued a token | same as `dry_run` |
| `refused` | A live attempt was blocked (policy, mode, token, read-only) | `reasons` |
| `live_intent` | The policy passed. **Written and fsynced before the request is sent** | `value_inr`, `notional_inr`, `order_id` |
| `live_result` | Kite accepted the request | `counts`, `order_count`, `value_inr`, `notional_inr`, `order_id`, `response` |
| `live_error` | Kite answered with an error | `error_type`, `status`, `error` |
| `live_unknown` | No answer (timeout, dropped connection): the write may have happened | `counts`, `order_count`, `value_inr`, `notional_inr`, `error` |

`value_inr` is what a write adds to today's usage (for a modification, its increase; for a conversion or a cancel,
nothing); `notional_inr` is the full value of its legs, kept for the record.
| `kill_switch_engaged` | `engage_kill_switch` ran | `reason` |

The `call_id` ties a `live_intent` to its `live_result`, `live_error` or `live_unknown`. A `live_intent` with no outcome
after a crash means the outcome is unknown: check the order book.

A live send, as it appears in the file (shortened):

```json
{"ts":"2026-11-04T10:00:00.000+05:30","event":"live_intent","call_id":"fbeca3bdbf68","tool":"place_order","request":{"method":"POST","path":"/orders/regular","form":{"exchange":"NSE","tradingsymbol":"INFY","transaction_type":"BUY","quantity":"2","product":"CNC","order_type":"LIMIT","price":"1500.0","validity":"DAY"}},"summary":"BUY 2 NSE:INFY LIMIT @ 1500.0 (CNC, regular)","value_inr":"3000.00","order_id":null}
{"ts":"2026-11-04T10:00:00.412+05:30","event":"live_result","call_id":"fbeca3bdbf68","tool":"place_order","request":{"...":"same as above"},"summary":"BUY 2 NSE:INFY LIMIT @ 1500.0 (CNC, regular)","counts":true,"order_count":1,"value_inr":"3000.00","order_id":"250101000000001","response":{"order_id":"250101000000001"}}
```

Read it with `kite-connect-mcp audit [--date YYYY-MM-DD] [--limit N]`, with the read-only `get_audit_log` tool, or with
`jq` on the file. The server never rotates or deletes it; keep it, because it is your record of what was sent.

## 13. Rate limiting, retries and outcome unknown

- **Limits kept.** The server paces its own requests under Kite's limits: quotes 1/s, historical candles 3/s, order
  placement 10/s and 400/min, everything else 10/s. Each window is 10% longer than Kite's as a margin. The pacing is
  per server process; another program using the same account has its own and shares Kite's budget (see
  [KITE_SETUP.md](KITE_SETUP.md#8-rate-limits)).
- **Reads are retried.** On HTTP 429 (honouring `Retry-After`), 502, 503, 504 and transport errors, up to three times
  with backoff.
- **Writes are never retried.** If the connection could not even be made, the write was not sent and the error says so.
  If the request may have left (a timeout, a dropped connection), the outcome is **unknown**: the tool reports "no
  answer from Kite; the request may have been executed. Check the matching book (get_orders, get_gtts, get_alerts,
  get_mf_orders or get_mf_sips) before trying again", and the audit log gets a `live_unknown`. An HTTP 429 or 5xx answer to a write is reported as it
  is (audited as `live_error`), also without a retry; after a 5xx, check the order book before you send it again.
- **Unknown outcomes are counted** in the daily caps as if they happened, so the caps err on the safe side. Check the
  order book and reconcile by hand: an order that never reached Kite still uses up its share of the day's caps.

## 14. Sessions, credentials and external mode

- **Credentials stay out of sight.** The API key, secret, access token and bearer token are held in memory, never
  logged, never in a settings dump (`check-config` prints `set` or `unset`), and scrubbed from every error message.
- **The token file** is written atomically with mode 600 in `managed` mode. A token file that other users can read earns
  a warning in the log.
- **`external` mode never ends a session.** When another program owns the login, the token file is read-only to this
  server: writing or deleting it is refused, the token exchange and logout calls are blocked at the door, and the
  login and logout tools and commands refuse. The server cannot replace or end a session it does not own.
- **A rejected token is dropped.** If Kite answers `TokenException`, the server stops using that token until a
  different one appears.
- **`KITE_MCP_ALLOWED_USER_ID`** makes managed login refuse any Kite account other than yours.
- **The HTTP endpoint** needs a bearer token of at least 32 characters (`/mcp` only), checks the `Host` and `Origin`
  headers, writes no access log, and serves unauthenticated HTTP only on loopback. See
  [SELF_HOSTING.md](SELF_HOSTING.md).

## 15. What is not protected

Be clear-eyed about the limits of this layer.

- **Orders placed outside this server.** Orders from the Kite app or terminal, from another API program, GTTs that
  fire later at Kite, and anything already open are invisible to the caps and the allow-lists. The caps count only what
  this server sent.
- **The caps are estimates of notional value, not of risk or loss.** A limit order counts at its limit price; a MARKET
  order counts at the last price when you asked for the preview, which can move before you confirm. Derivatives count
  at `quantity × price`, which is not the margin used and not the maximum loss. A short position can lose more than it
  cost to open. MCX, CDS and BCD values are not estimated at all (those orders are dry-run only).
- **A SELL limit is valued at its limit price**, even though it can fill higher, and a SIP's later instalments run on
  Kite's schedule, outside these caps (only the first instalment or lump sum is checked).
- **Anyone with shell access to the machine** can edit the settings, switch on live trading, delete the kill switch,
  delete the audit log (which resets the daily caps) or read the access token and call Kite directly, bypassing this
  server completely. Protect the account that runs it and the folders it uses.
- **Anyone with the bearer token** can use every tool you expose, including live orders within your caps. It is a
  password.
- **The model can read your account.** Holdings, positions, orders, funds, your profile and the audit log are returned
  to the AI client you connect, and so to its provider. Narrow what is exposed with `KITE_MCP_TOOLSETS` and
  `KITE_MCP_EXCLUDED_TOOLS`.
- **A confirmation token is not a human decision.** The same model that asks for the dry run also makes the second call.
  The token stops mistakes, replays and silent changes of parameters, and it makes the model show a preview. It does not
  stop a model that has been misled (for example by text it read in a web page or a tool result) from confirming its own
  order. The human gate is your MCP client's per-tool approval prompt: keep it on for write tools, and read the preview.
  In `allowlist` mode there is no per-order step at all.
- **Kite's own risks remain:** exchange price bands, circuit limits, rejected or partially filled orders, and failures
  on Kite's side.
- **This is alpha software.** Start with the defaults, in dry-run mode, and read the audit log.

## 16. Recommended configurations

Put the lines in the server's settings file (see [CONFIGURATION.md](CONFIGURATION.md)). Values are examples: size the
caps to your own account.

### (i) Read-only research

For asking questions about your portfolio, orders, funds and (on Connect) market data. Nothing can change the account.
You do not need a static IP, and the free Personal plan is enough unless you want market data.

```bash
KITE_MCP_READ_ONLY=true
KITE_MCP_ALLOWED_USER_ID=AB1234
```

### (ii) Supervised trading

You review every order. Low caps, token confirmation, a narrow set of what can be sent. Keep your client's approval
prompt on for `place_order`, `modify_order` and `cancel_order`, and read each preview before the second call.

```bash
KITE_MCP_TRADING_MODE=live
KITE_MCP_LIVE_CONFIRMATION=token
KITE_MCP_CONFIRMATION_TTL_S=120
KITE_MCP_MAX_ORDER_VALUE=5000
KITE_MCP_MAX_DAILY_VALUE=15000
KITE_MCP_MAX_ORDERS_PER_DAY=5
KITE_MCP_ALLOWED_EXCHANGES=NSE,BSE
KITE_MCP_ALLOWED_PRODUCTS=CNC
KITE_MCP_ALLOWED_VARIETIES=regular
KITE_MCP_ALLOWED_ORDER_TYPES=LIMIT,SL
KITE_MCP_ALLOWED_USER_ID=AB1234
```

`LIMIT,SL` lets you place and cancel limit and stop-limit orders, which keeps every order's value known (this also
works on the Personal plan). Add `SL-M` if you need stop-market orders, because the allow-lists apply to cancellations.
Live orders need the registered static IP.

### (iii) An automated agent

A scheduled or autonomous agent that must act without you. This is the riskiest setup; make the blast radius small.

```bash
KITE_MCP_TRADING_MODE=live
KITE_MCP_LIVE_CONFIRMATION=allowlist
KITE_MCP_ALLOWED_INSTRUMENTS=NSE:INFY,NSE:TCS
KITE_MCP_ALLOWED_EXCHANGES=NSE
KITE_MCP_ALLOWED_PRODUCTS=CNC
KITE_MCP_ALLOWED_VARIETIES=regular
KITE_MCP_ALLOWED_ORDER_TYPES=LIMIT
KITE_MCP_MAX_ORDER_VALUE=3000
KITE_MCP_MAX_DAILY_VALUE=6000
KITE_MCP_MAX_ORDERS_PER_DAY=4
KITE_MCP_MAX_QUANTITY=2
KITE_MCP_TOOLSETS=session,account,orders,portfolio,market,safety
KITE_MCP_EXCLUDED_TOOLS=modify_order
KITE_MCP_ALLOWED_USER_ID=AB1234
```

- Keep the instrument list to the few symbols the agent really needs, and the caps to what you could lose without
  caring.
- List only the toolsets the agent needs. A new write tool in a later version would otherwise be available to it.
- Run it in `dry_run` mode first, read the audit log for a week, then switch the mode to `live`.
- Check that the kill switch works before you rely on it, and know where the file is.
- The bearer token is shared by every client of the server. If the agent runs somewhere you trust less than the server,
  rotate the token when you stop using that agent.

## 17. Checking your setup

1. `kite-connect-mcp check-config` shows the effective settings. Confirm `trading_mode`, `read_only`, the caps, the
   allow-lists and `kill_switch_file`.
2. Ask the model to call `get_safety_status`. It reports the same limits as the server sees them, plus today's usage.
3. Dry-run an order that should pass, then one that must fail (above the per-order cap, or outside an allow-list).
   Check that the second shows a violation and no token.
4. With live trading on, try the second call with a wrong token, or with a changed quantity. It must be refused.
5. Engage the kill switch (`touch` the file, or ask the model to call `engage_kill_switch`), confirm that a dry run shows
   the violation, then delete the file.
6. Read the audit log (`kite-connect-mcp audit`) and check that each step left an event.
