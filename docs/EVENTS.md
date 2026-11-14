# Order events

kite-connect-mcp can record every change to your orders (placed, partly filled, modified, filled, cancelled,
rejected) and hand each change to programs you choose: a notification on your phone, a trading journal, an automation.
This page explains where the changes come from, what an event holds, how the events reach your programs, and how to run
and check it all. The feature is off by default.

Contents: [What it does](#what-it-does) · [Turning it on](#turning-it-on) · [Sources](#sources) · [Events](#events) ·
[Consumers](#consumers) · [Operating it](#operating-it) · [Scheduled login links](#scheduled-login-links) ·
[Security notes](#security-notes) · [Limits](#limits)

## What it does

The order events follow the orders of one Kite account: the account whose token the server holds.

- **Postbacks are a doorbell.** When Kite posts an order update to the server, the server checks it, then reads that
  order back from Kite and records what Kite answered. The postback itself is never stored.
- **The order-book poll is the backup.** Every minute from 09:00 to 16:00 IST on weekdays, by default, the server
  reads the day's order book. That catches a postback that never arrived, and it is the only source for orders placed
  outside this API key: on Kite's web site or app, or by another program. That includes an order a GTT placed, if Kite
  sent no postback for it.
- **One durable store.** Every event is written to a SQLite file before anything else happens, so a restart loses
  nothing.
- **A router hands each event to the commands you configure** (the consumers). A command reads the event as JSON on
  its standard input and says with its exit code whether it is done; a failed delivery is retried.

```
  Kite postback --> /postback --> doorbell --> GET /orders/{id} ----+
                                                                    +--> event store --> router --> your commands
  every minute, in the poll window ----------> GET /orders ---------+         |
                                                     get_order_events, get_event_status, events status|tail
```

It is read-only. The event service calls Kite for two reads only, the order book (`GET /orders`) and one order's
history (`GET /orders/{order_id}`): nothing here can place, modify or cancel an order. Both reads work on the Personal
plan as well as on Connect.

## Turning it on

The event service runs inside the HTTP server, because it needs one long-lived process: `KITE_MCP_EVENTS=true` is
refused unless the transport is HTTP (`KITE_MCP_TRANSPORT=http`, or `serve --transport http` as the systemd unit and
the Docker image do). Every setting below is off, or leaves the server as it was, by default.

| Setting | Default | Meaning |
|---|---|---|
| `KITE_MCP_EVENTS` | `false` | Run the event service in this server: the doorbell, the poll, the store and the router. HTTP transport only. |
| `KITE_MCP_EVENTS_DB` | `<state dir>/events.sqlite3` when `KITE_MCP_EVENTS=true`, else unset | The event store. The server needs it only to move the store. Any other process (a stdio client, the `events` commands) names the server's store here to read it. |
| `KITE_MCP_EVENTS_CONSUMERS_FILE` | unset | The consumers file (see [Consumers](#consumers)). Without it events are recorded and nobody is called. Read when the server starts. |
| `KITE_MCP_ORDER_POLL_INTERVAL_S` | `60` | Seconds between two polls of the order book. At least 10. |
| `KITE_MCP_ORDER_POLL_WINDOW` | `09:00-16:00` | When the poll runs, in IST: `HH:MM-HH:MM`, the start before the end. |
| `KITE_MCP_ORDER_POLL_DAYS` | `Mon-Fri` | The days the poll runs: a range (`Mon-Fri`), a list (`mon,wed,fri`), one day (`Tue`) or `*` for every day. A day is three or more letters of its English name, in any case; a range runs forward, from Monday towards Sunday. |
| `KITE_MCP_EVENTS_KEEP_DAYS` | `30` | How many days events are kept. From 1 to 3650. |
| `KITE_MCP_POSTBACK_PATH` | unset | The postback route, for example `/postback`. Without it there are no postbacks, and the poll alone feeds the events. |

To turn it on:

1. **The settings.** Set `KITE_MCP_EVENTS=true`, `KITE_MCP_POSTBACK_PATH=/postback` and, once you have one,
   `KITE_MCP_EVENTS_CONSUMERS_FILE`. On a systemd server the drop-in
   [`deploy/systemd/kite-connect-mcp.service.d/events.conf`](../deploy/systemd/kite-connect-mcp.service.d/events.conf)
   sets the first two for the service only: `KITE_MCP_EVENTS=true` needs the HTTP transport, which the commands run
   with the settings file do not have. The store (`KITE_MCP_EVENTS_DB`), the consumers file and any other events
   setting go in the settings file, which the service and the `events` commands both read;
   [SELF_HOSTING.md](SELF_HOSTING.md#order-events-on-a-server) walks through it. The Docker image runs HTTP already, so
   there every line goes in the settings file.
2. **The API secret.** The postback checksum is made with your app's API secret, so the server needs
   `KITE_API_SECRET` (or `KITE_API_SECRET_FILE`). Without it every postback is refused.
3. **The postback URL.** In the [developer console](https://developers.kite.trade), set your app's postback URL to the
   server's public URL plus the path, for example `https://kite.example.com/postback`. See
   [KITE_SETUP.md](KITE_SETUP.md#5-postback-url-optional).
4. **The reverse proxy.** Forward `POST /postback` to the server. The [Caddy](../deploy/caddy/Caddyfile.example) and
   [nginx](../deploy/nginx/kite-connect-mcp.conf.example) samples have that route, with a cap on the request body of
   64 KiB, the server's own cap.
5. **The account pin.** Set `KITE_MCP_ALLOWED_USER_ID` to your user ID (for example `AB1234`), or
   `KITE_MCP_ALLOWED_USER_ID_SHA256` to its SHA-256 (see [CONFIGURATION.md](CONFIGURATION.md#session-and-token-store)):
   a postback for any other account is then refused.
6. **Check it.** Restart the server, send a selftest through the public URL and read the status (see
   [Operating it](#operating-it)): `kite-connect-mcp events selftest --url https://kite.example.com/postback`, then
   `kite-connect-mcp events status`.

## Sources

### Postbacks: a doorbell

Kite's checksum is the SHA-256 of `order_id + order_timestamp + api_secret`. It proves that Kite sent that order id
with that timestamp, and nothing more: the status, the quantities and the prices in a postback could be changed, or a
whole postback captured and sent again, and the checksum would still match. So the server trusts a postback only as a
ring of a doorbell ("something happened to this order"), and reads the order from Kite itself.

The postback route is public, like the login callback: Kite cannot send a bearer token. It checks each request in this
order, and refuses it with a bare response (no body) at the first check that fails:

| Check | Refused with | Counted as |
|---|---|---|
| An API secret is configured | 403 | `no API secret` |
| The body is at most 64 KiB, counted as it arrives | 413 | `body over the cap` |
| The body is JSON | 400 | `body not JSON` |
| The body is a JSON object | 400 | `body not a JSON object` |
| The checksum matches (compared in constant time) | 403 | `bad checksum` |
| The order id has an order id's form: 1 to 64 letters, digits, `_` or `-` | 400 | `bad order id` |
| With an account pin set (`KITE_MCP_ALLOWED_USER_ID` or `KITE_MCP_ALLOWED_USER_ID_SHA256`), the postback's `user_id` is that account (a signed selftest names no account, and is not held to it) | 403 | `another account` |

Refusals are counted in `postback_refused`, with the last one's reason as its note; a wrong API secret shows up there
as `bad checksum`. They are written to the store at most once a second and logged at most once a minute per reason, so
junk sent to the route can neither keep the disk busy nor flood the log. The line for an accepted postback is logged at
most once a minute too, since a replay can repeat it as often. Nothing from a refused request is stored, and the route
logs nothing from any request (no order id, no checksum, no user).

A postback that passes every check is answered:

- **200** once its doorbell is on disk: the order id is kept in the store and counted in `postback_accepted`;
- **200 at once, with the ring written within 2 seconds,** when its order's doorbell was written less than 2 seconds
  ago. One order's doorbell is written at most once every 2 seconds: the postbacks in between are held in memory and
  written as one ring (each counted) when the 2 seconds are over, or when the server stops. So a burst of updates still
  gets one more read after its last postback, and a captured postback replayed with a field changed (which the checksum
  does not cover) costs at most one write and one read every 2 seconds. A held ring that cannot be written (it is
  logged), or that a crash loses, is left to the poll. A selftest within 2 seconds of the same one is answered 200 and
  not written again;
- **503** when the store cannot write: nothing is written to the store, the log says so, and the poll catches the
  change later;
- **200 without a write** when it repeats a postback accepted in the last 10 seconds (Kite sending it twice, or a
  replay): its doorbell has rung already. A repeat is the same JSON, whitespace and the order of the keys aside.

A worker then reads each waiting doorbell's order from Kite (`GET /orders/{order_id}`), takes the last row of its
history, and records Kite's answer, with `postback` as its source, if it reports a change (see [Events](#events)).
The `gtt` marker and the check on the exchange's update time (see
[Deduplication](#deduplication-and-why-progress-only-moves-forward)) need Kite to include `meta` and
`exchange_update_timestamp` in the history's rows as in the order book's: without them an event from a postback reads
`gtt` false, and is judged without the exchange's time.

- **No token.** Without a valid token nothing is read, and the doorbells wait. The poll that a login starts reads them
  first, so a fill that happened before your morning login (of an AMO placed through this API key, say) is reported
  right after it.
- **An order Kite does not know.** Kite answers 404 `GeneralException` "Couldn't find that `order_id`." (or an empty
  history), for instance to a replay of an old postback. The doorbell is dropped and counted in
  `doorbell_unknown_order`.
- **An answer about another order.** The doorbell is dropped, with no event, and counted in `doorbell_wrong_order`.
- **Any other failure** (Kite is down, a timeout, another error). The doorbell stays and is read again in a later
  round: a round runs on every ring and every minute, and a doorbell counts at most one failed try a minute. At its
  20th failed try it is given up and counted in `doorbell_gave_up`; the poll still reports the order.
- **A ring during the read.** A doorbell that rings again while its order is being read stays for another read,
  whatever the first read found: Kite's answer may be older than the change the new ring reports.
- **A rejected token** ends the round and raises `token_rejected` (see the poll, below).
- Doorbells older than two days are deleted: an order book lives for one day.

**With `KITE_MCP_EVENTS` off** the postback route works as before: an accepted postback goes to the ticker's
order-update buffer, which `ticker_order_updates` reads. The one difference is that a signed selftest (see
[Operating it](#operating-it)) is answered 404 and never reaches the buffer. With the events on, the route exists
whenever `KITE_MCP_POSTBACK_PATH` is set, whether the ticker tools are on or not, and an accepted postback still goes
to the ticker's buffer too while `ticker_order_updates` is on.

### The order-book poll

The poll reads the day's order book (`GET /orders`) and records each order's change. Postbacks cover only the orders
placed with this API key; the poll catches the rest within one interval, on the same day.

It runs:

- every `KITE_MCP_ORDER_POLL_INTERVAL_S` seconds (60) inside `KITE_MCP_ORDER_POLL_WINDOW` (09:00-16:00 IST) on
  `KITE_MCP_ORDER_POLL_DAYS` (Monday to Friday);
- once when the service starts, after reading the doorbells that wait;
- at once when a new token appears (a login, or in `external` mode a new token file), again after reading the
  doorbells that wait;
- after the window closes on a poll day, every interval until one poll succeeds. Kite's order book holds only today's
  orders, so this is the last chance to see the day's final statuses.

It catches a postback that was lost, refused or never sent (an order a GTT placed may get none), a stretch when the
service was down (on the same day), and orders placed on Kite's web site or app or by another program.

- **The first poll ever is a silent baseline.** It records the state of every order in the book and tells nobody, so
  turning the events on in the middle of a day does not replay the day. Every change after it is an event.
- **No token:** the poll skips quietly, and runs at once when a token appears.
- **A rejected token:** when Kite refuses the token, the server stops sending it, raises one `token_rejected` session
  event for that token (whichever call met the refusal first: the poll, a doorbell or a tool call), and the poll waits
  for a new login.
- **Failures** are counted in `poll_failed`, with the error as the note (or "an unexpected error: <type> (see the
  log)"). The log says when the polls start failing and when they work again, not once a minute.
- Each poll that works is counted in `poll_ok`, with why it ran as the note: `startup`, `window`, `login` or
  `after_close`.

The poll does not run after the window, so a change later in the evening reaches the events only by its postback,
which only orders placed with this API key have. And a window that ends before a market closes runs its after-close
poll before that close: the poll succeeds, so `events status` shows no warning, yet the final statuses after it are
never seen. End the window after the last close you trade in:

- currency derivatives trade until 17:00: for example `KITE_MCP_ORDER_POLL_WINDOW=09:00-17:15`;
- MCX trades until 23:30, or 23:55 while the US is on standard time (roughly November to March): use
  `KITE_MCP_ORDER_POLL_WINDOW=09:00-23:59`, the latest end the setting takes. The after-close poll then has the day's
  last minute; if it fails, `events status` warns.

## Events

Every event is a JSON object with these fields:

- `kind`: `order`, `session`, `router` or `selftest`;
- `event_id`: a 32-digit hexadecimal id (see [Deduplication](#deduplication-and-why-progress-only-moves-forward));
- `source`: where it came from;
- `received_at`: when the server recorded it, in IST, to the second;
- `text`: a one-line English summary that a consumer can forward as it is. It never holds an order id, an account or
  a link.

The store adds `seq`, a number that only goes up, when it reads an event back: consumers, `events tail` and
`get_order_events` all see it.

| `kind` | Raised when | `source` |
|---|---|---|
| `order` | an order changed | `postback` or `poll` |
| `session` | the day's login changed: a new token was stored (`session` is `login`), another account's login was refused (`login_refused`), a login on a link of this server's did not complete (`login_failed`), or Kite rejected the stored access token (`token_rejected`). See [Session events](#session-router-and-selftest-events) | `service` |
| `router` | a delivery to a consumer is late (`delivery_delayed`) or has failed (`delivery_failed`) | `router` |
| `selftest` | a signed selftest postback arrived (`kite-connect-mcp events selftest`) | `selftest` |

### Order events

```json
{
  "kind": "order",
  "event_id": "4fe192403b7a90fb2be8ea0da9f4e9e7",
  "state_id": "6d7580b7cf4612338f47c100cf939349",
  "source": "postback",
  "received_at": "2026-11-16T10:15:02+05:30",
  "change": "filled",
  "text": "BUY 25 ABCD (NSE) filled at 1234.50: complete [mytag]",
  "order": {
    "order_id": "261116000000001",
    "status": "COMPLETE",
    "status_raw": "COMPLETE",
    "status_message": null,
    "tag": "mytag",
    "gtt": false,
    "tradingsymbol": "ABCD",
    "exchange": "NSE",
    "transaction_type": "BUY",
    "order_type": "LIMIT",
    "product": "CNC",
    "variety": "regular",
    "validity": "DAY",
    "quantity": 25,
    "filled_quantity": 25,
    "pending_quantity": 0,
    "cancelled_quantity": 0,
    "price": 1234.5,
    "trigger_price": 0,
    "average_price": 1234.5,
    "order_timestamp": "2026-11-16 10:15:00",
    "exchange_update_timestamp": "2026-11-16 10:15:02"
  },
  "seq": 42
}
```

`order` holds these documented fields of Kite's order and nothing else: no `user_id`, `placed_by`, `app_id`,
checksum, `guid` or the rest of `meta`. Numbers are JSON numbers, whole when they are whole. A field Kite left out is
`null`.

- `status` is normalized, and `status_raw` keeps Kite's own word:

  | `status` | Kite's statuses |
  |---|---|
  | `COMPLETE` | COMPLETE |
  | `CANCELLED` | CANCELLED, and CANCEL |
  | `REJECTED` | REJECTED |
  | `OPEN` | every other one: OPEN, UPDATE, TRIGGER PENDING, AMO REQ RECEIVED, the other PENDING states, MODIFIED |

- `status_message` is Kite's message, cleaned: the account's user ids become `[account]`, links become `[link]`, and
  it is cut to 160 characters.
- `tag` is the order's tag (or the first of its `tags`); `gtt` is true when a GTT placed the order.

`change` says what this event reports, against the last state the store holds for the order:

| `change` | When |
|---|---|
| `placed` | The first sight of an `OPEN` order with nothing filled. |
| `partial_fill` | `OPEN`, with more filled than before. Also the first sight of an `OPEN` order that has a fill already (the service started after it). |
| `modified` | `OPEN`, with the same fill but another quantity, price or trigger price. |
| `filled` | `COMPLETE`. |
| `cancelled` | `CANCELLED`, possibly after a partial fill. |
| `rejected` | `REJECTED`. |
| `seen_final` | The first sight of an order that is final already: it was placed and finished between two polls, or while the service was down. |

Only the state counts: (status, filled quantity, quantity, price, trigger price). A change of anything else is no
event; for example a stop-loss order whose trigger is hit goes from TRIGGER PENDING to OPEN, and both are `OPEN`.

`text` for each change (`seen_final` reads as the final status it found):

| `change` | `text` |
|---|---|
| `placed` | `BUY 25 ABCD (NSE) placed: LIMIT 1234.50 [mytag]` |
| `partial_fill` | `BUY 25 ABCD (NSE) partly filled: 10 of 25 at 1234.25 [mytag]` |
| `modified` | `BUY 25 ABCD (NSE) modified: LIMIT 1230.00 [mytag]` |
| `filled` | `BUY 25 ABCD (NSE) filled at 1234.50: complete [mytag]` |
| `cancelled` | `BUY 25 ABCD (NSE) cancelled after 10 of 25 filled at 1234.50 [mytag]` |
| `rejected` | `BUY 25 ABCD (NSE) rejected: Insufficient funds [mytag]` |

An order that a GTT placed reads `BUY 25 ABCD (NSE) (GTT) placed: ...`; a stop-loss order shows its trigger
(`SL 1234.50, trigger 1220.00`). The tag in brackets comes last, and `tag_labels` in the consumers file can replace it
with a label: with `{"mytag": "my strategy"}` the text ends in `[my strategy]`, while the `tag` field keeps `mytag`.

### Deduplication, and why progress only moves forward

The same change often arrives twice: the postback and the next poll both report it. The store keeps each order's last
state and compares every read with it, with every number as canonical decimal text, so that 5, 5.0 and "5.00" are
equal. A read with the same state reports nothing.

- `event_id` is a digest of the order id, the `event_id` of the order's previous event, and the new state. Two reads
  judged against the same stored state get the same id and are stored once. And because the id is chained to the
  previous event, an order that goes back to an earlier state (a price moved, then moved back) gets a new event each
  time.
- `state_id` is a digest of the order id and the state alone: every event that reports the same state has the same
  `state_id`.

**Progress only moves forward.** A read is dropped when the order is final already, when its filled quantity is below
the stored one, or when its `exchange_update_timestamp` is older than the stored one. The doorbell and the poll read
the same order at different moments and can finish in either order, so an older read can arrive after a newer one;
without this rule it would be reported as a change back, and a consumer would see a fill shrink or a filled order
reopen.

**Known limit.** When either read lacks the exchange's update time, or both carry the same second, a late read of an
earlier state cannot be told from a real modification back to it, and is reported as `modified`.

### Session, router and selftest events

Session events follow the day's login. Their `session` field names what happened:

| `session` | Raised when | Once per |
|---|---|---|
| `login` | A new token was stored: by this server's login callback or `complete_login`, or, in managed mode, by another process beside the serving one (such as `kite-connect-mcp login` run by hand), which the poll sees within seconds, on any day and at any hour. | token |
| `login_refused` | A login by another Kite account was refused (the account pin), and the session it opened was ended, or ending it failed. | refused session |
| `login_failed` | A real login on a link this server issued (its state was known, unexpired and unused) did not complete: Kite did not accept the exchange (`exchange_failed`), or the token could not be stored (`token_not_stored`). | link |
| `token_rejected` | Kite rejected the stored access token, whichever call met the rejection first: the poll, a doorbell or a tool call. Never raised by a logout, nor for the session of another account that was refused. | token |

A login, once per token, whoever stored it (the server's own report and the poll's are one event):

```json
{"kind": "session", "event_id": "5e33dd0116e6733635911bc9028d71e2", "source": "service", "received_at": "2026-11-16T07:35:03+05:30", "session": "login", "text": "Kite login completed: the session is valid until 06:00 IST tomorrow.", "logged_in_at": "2026-11-16T07:35:03+05:30", "expires_at": "2026-11-17T06:00:00+05:30", "seq": 38}
```

Another account's login, refused (the account pin), once per refused session:

```json
{"kind": "session", "event_id": "e04def03cee2752f711e2a6f4ecc0eaf", "source": "service", "received_at": "2026-11-16T07:34:41+05:30", "session": "login_refused", "text": "A Kite login by another account was refused, and the session it opened was ended.", "session_ended": true, "seq": 37}
```

When ending that session failed, the flag is false and the text reads "A Kite login by another account was refused;
ending the session it opened failed, so it lapses at 06:00 IST."

A login on a link of this server's that did not complete, once per link:

```json
{"kind": "session", "event_id": "7616bc64e2ce4f4a8b1729fbfe1aacc4", "source": "service", "received_at": "2026-11-16T07:33:12+05:30", "session": "login_failed", "text": "A Kite login did not complete: Kite did not accept the login. Tap the link again or ask for a new one.", "reason": "exchange_failed", "seq": 36}
```

`reason` is `exchange_failed` or `token_not_stored` (text: "A Kite login did not complete: the token could not be
stored."), never Kite's own message. After a failed exchange the same link still works until it expires.

A rejected token, once per token:

```json
{"kind": "session", "event_id": "17559475683e3cbc60b4721bc8c413a7", "source": "service", "received_at": "2026-11-16T09:00:05+05:30", "session": "token_rejected", "text": "Kite rejected the stored access token: log in again to resume the order events.", "seq": 40}
```

- **Once.** Each session event's id comes from what it is about (a fingerprint of the token, or a digest of the
  link's state, never the thing itself), so a second report of the same login, refusal, failure or rejection stores
  nothing new.
- **The poll's `login`** is managed mode only. The token in use when the service starts is not a login, so a restart
  raises none, and nor does a login made while the service was down.
- **The callback's noise never becomes an event.** A malformed redirect, an unknown, expired, used or replayed state,
  rate limiting, probes and an error in the route stay a throttled line in the audit log. A request token pasted
  without a state (`complete_login` without one) that fails raises nothing either: whoever pasted it sees the answer.
- **Never in a session event:** a user id, an access or request token, a state, a login link, or Kite's message text.
- **A consumer that takes the `session` kind gets every session event.** `match` has no filter by session name: a
  consumer that wants only some reads `session` and exits 0 for the names it does not want.

A delivery that failed:

```json
{
  "kind": "router",
  "event_id": "ed67062eb7300c401bd5b589fd04e788",
  "source": "router",
  "received_at": "2026-11-16T10:15:03+05:30",
  "router": "delivery_failed",
  "consumer": "webhook",
  "about_seq": 42,
  "about_event_id": "4fe192403b7a90fb2be8ea0da9f4e9e7",
  "error": "exit 1: webhook.sh: HTTP 404",
  "text": "Delivery to webhook failed: BUY 25 ABCD (NSE) filled at 1234.50: complete [mytag] (exit 1: webhook.sh: HTTP 404)",
  "seq": 43
}
```

- `router` is `delivery_delayed` (still retrying past the consumer's alert point; its text reads "Delivery to webhook
  is delayed and still retrying: ...") or `delivery_failed` (given up, or a permanent failure).
- `about_seq` and `about_event_id` name the event whose delivery is late or failed; `error` is the last error, at
  most 200 characters: the router's reason (a timeout, a command that cannot start or fails the ownership check), or
  the exit status and the end of the command's stderr (see [What a command gets](#what-a-command-gets)).
- A router event never goes to the consumer it is about, and nothing is raised about a router event itself, so two
  failing consumers cannot report each other's failures for ever.

A selftest (its order id is `selftest-` and 12 random hexadecimal digits; it never reaches Kite):

```json
{"kind": "selftest", "event_id": "3b949ae9b610e992bfa59fb467eed46b", "source": "selftest", "received_at": "2026-11-16T08:30:00+05:30", "order_id": "selftest-0a1b2c3d4e5f", "text": "Self-test postback received and verified: the postback path works.", "seq": 39}
```

The server never puts an access token, a request token, a login state, a login link, a user id or a checksum in an
event. The one field that carries text from outside is a router event's `error`: the end of a consumer's standard
error.

## Consumers

A consumer is a program that the router runs for each event it matches. [examples/](../examples/README.md) has three
to start from: a Telegram notifier, a JSON-lines log and a webhook.

### The consumers file

The file named by `KITE_MCP_EVENTS_CONSUMERS_FILE` is JSON, in UTF-8. A full example:

```json
{
  "tag_labels": {"mytag": "my strategy"},
  "consumers": [
    {
      "name": "telegram",
      "command": ["/usr/bin/python3", "/opt/kite-connect-mcp/examples/consumers/telegram_notify.py"],
      "match": {
        "kinds": ["order", "session", "router", "selftest"],
        "changes": ["filled", "partial_fill", "cancelled", "rejected", "seen_final"]
      },
      "timeout_s": 20,
      "alert_after_s": 60,
      "retry": {"delays_s": [10, 30, 60, 120, 300], "every_s": 300, "give_up_after_s": 1800},
      "env_pass": ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"]
    },
    {
      "name": "webhook",
      "command": ["/opt/kite-connect-mcp/examples/consumers/webhook.sh"],
      "match": {"kinds": ["order"], "statuses": ["COMPLETE"], "tags": ["mytag"], "sources": ["postback", "poll"]},
      "env_pass": ["WEBHOOK_URL"]
    }
  ]
}
```

| Key | Default | Meaning |
|---|---|---|
| `tag_labels` | none | A label for a tag, shown in `text` in place of the tag. A label is one printable line of at most 40 characters. |
| `consumers` | none | The list of consumers. |
| `name` | required | 1 to 40 lower-case letters, digits, `-` or `_`, starting with a letter or a digit. Unique. The commands and the router events use it. |
| `command` | required | The program and its arguments, as a list (argv). No shell. The first item is the absolute path of an executable file. |
| `match` | order events | Which events the consumer gets (below). |
| `timeout_s` | `30` | Seconds the command may run. From 1 to 600. |
| `alert_after_s` | `60` | Raise `delivery_delayed` once when a delivery is still retrying this many seconds after its first try. From 1 to 86400. |
| `retry.delays_s` | `[10, 30, 60, 120, 300]` | The waits after the first, second, ... failed try: 1 to 20 whole numbers of seconds, each from 1 to 86400. |
| `retry.every_s` | `300` | The wait after each later failed try. From 1 to 86400. |
| `retry.give_up_after_s` | `1800` | Stop retrying once this many seconds have passed since the first try. From 0 (one try, no retry) to 86400. |
| `env_pass` | none | Names of variables of the server's own environment to pass to the command. Never a `KITE_*` variable. |

The file is read strictly, so a mistake cannot quietly change what a consumer gets. An unknown key anywhere is refused;
so is a key given twice anywhere (JSON parsers keep the last one, which could drop a filter or a whole list), and a
consumer named twice. Any problem stops the server from starting, with every problem listed. The file is read when the
server starts: restart it after a change.

### `match`

| Key | Values | Applies to |
|---|---|---|
| `kinds` | `order`, `session`, `router`, `selftest` | every event. Default `["order"]` |
| `statuses` | `OPEN`, `COMPLETE`, `CANCELLED`, `REJECTED` (the normalized status) | order events |
| `changes` | `placed`, `partial_fill`, `modified`, `filled`, `cancelled`, `rejected`, `seen_final` | order events |
| `tags` | any tag (the order's tag, not its label) | order events |
| `transaction_types` | `BUY`, `SELL` | order events |
| `sources` | `postback`, `poll` | order events |

- A consumer without `match` gets every order event, and only order events. Session, router and selftest events
  reach only the consumers that list those kinds.
- The other keys filter order events only, so a consumer that gives one must list `order` among its kinds. There is no
  key for session names: a consumer that lists `session` gets `login`, `login_refused`, `login_failed` and
  `token_rejected`, and ignores the names it does not want (exit 0).
- An event matches when it matches every key given; within a key, any value listed matches. A list must not be empty:
  leave the key out to match anything. `statuses` and `transaction_types` are read in any case.
- To hear about every final outcome, list `seen_final` with `filled`, `cancelled` and `rejected`: an order seen for the
  first time when it is final already arrives as `seen_final`.

### What a command gets

| | |
|---|---|
| Standard input | One event as JSON (UTF-8), then a newline, then the end of the input. |
| Environment | `PATH`, `HOME`, `LANG`, `KITE_EVENT_CONSUMER` (the consumer's name) and the variables named in `env_pass`. Nothing else, and never one of the server's `KITE_*` settings. |
| Arguments | As written in `command`. Other users of the machine can see a command's arguments (`ps`): never put a secret there; put it in the server's environment and name it in `env_pass`. |
| Standard output | Discarded. |
| Standard error | Its end becomes the delivery's error (below). |
| User | The server's user, in a process group of its own. |

How it ends decides what the router does:

| The command | Meaning | The router |
|---|---|---|
| exits 0 | Handled, including a refusal the consumer has reported itself | Marks the delivery done. |
| exits 75 (`EX_TEMPFAIL`) | A temporary failure | Retries it. |
| is killed by a signal | A service stop, the out-of-memory killer | Retries it. |
| runs past `timeout_s` | Too slow | Kills its process group and retries it. |
| cannot start because the system is short of something for a while (`EAGAIN`, `ENOMEM`, `EMFILE`, `ENFILE`, `ETXTBSY`) | A temporary failure | Retries it. |
| exits with any other code, cannot start for another reason, or fails the ownership check | A permanent failure | Raises `delivery_failed` at once, with no retry. |

**Retries.** By default a delivery is tried again after 10 s, 30 s, 1 min, 2 min and 5 min, then every 5 min, until
30 minutes have passed since its first try; then it fails and `delivery_failed` is raised. A delivery still retrying
past its alert point (60 s by default) raises `delivery_delayed`, once per round of tries. A paused consumer's
deliveries raise no alert, and `events resume` starts their tries afresh, so the time paused counts towards neither
point.

**Two traps.**

- A command has ended when it has exited **and** its standard error is closed. A process it starts in the background
  must not keep standard error open (redirect it, for example `2>/dev/null`), or the delivery waits for it and times
  out.
- Only the command's own death by a signal counts. A shell script whose child is killed exits 128 + the signal's
  number, which is a permanent failure: have the script exit 75 itself in that case.

**Standard error is not private.** The router keeps the last 4 KB of a command's standard error and cleans it:
terminal escape sequences and other unprintable characters are dropped, line breaks become spaces, and every word that
holds a link becomes `[link]`. It is then cut, with its head (`exit 1: ...`), to 200 characters. That text is written
to the server's log, shown in `events status` (`last_error`) and sent to the other consumers as the `error` of a
router event. **Never print a secret to standard error.** A secret without a scheme is not recognised: an HTTP
client's exception may quote a URL path that holds a token (`Max retries exceeded with url: /bot<token>/sendMessage`
has no scheme or host). Catch your HTTP client's exceptions and print a message of your own, as the examples do.

The program is checked again before each run (see [File permissions](#file-permissions)); one that someone else could
change since the server started, or that is gone, is a permanent failure and is not run. The log names a consumer and
its program, never the command's arguments or the event.

### Order of delivery, and at least once

- Each consumer runs one command at a time. Consumers do not wait for each other.
- An order's events reach a consumer one at a time and in order. While one is retrying, that order's later events
  wait behind it; other orders go on. Session, router and selftest events do not wait for each other.
- After a delivery fails for good, the order's later events go on. `events retry` queues failed deliveries again, so a
  re-sent event can arrive after later events of its order: tell them apart by `seq`.
- Deliveries are **at least once**: a crash between a command's exit and the bookkeeping repeats the delivery. Make
  your consumer idempotent on `event_id`.
- A restart loses nothing: the pending deliveries are in the store. A command still running when the server stops is
  killed, and its delivery stays pending for the next start.

### File permissions

The commands run as the server's user, who can read the Kite token. Whoever could change the consumers file, or a
program it names, could run code as that user. So, like OpenSSH's StrictModes, the server refuses to start, with the
reason, unless:

- the consumers file and each program (the first item of a `command`) are regular files owned by the server's user or
  root, and writable by no one else;
- every folder on the way to them is owned by that user or root and writable by no one else, unless the folder is
  sticky (like `/tmp`);
- every link on the way is owned by that user or root. Links are followed one step at a time, as the system follows
  them.

Group write is accepted on a file or folder whose group is private to its owner (Debian and Ubuntu give each user a
group of their own) and that has no POSIX ACL. Make the consumers file `chmod 600` anyway.

Not checked, so keep them the same way yourself:

- a script given as an argument, as in
  `["/usr/bin/python3", "/opt/kite-connect-mcp/examples/consumers/log_to_file.py"]`: keep it owned by you or root and
  writable by no one else;
- NFSv4 ACLs and macOS ACLs, which can let others write whatever the mode bits say.

In Docker the image runs as uid 10001, so a consumers file or a program bind-mounted into the container must belong to
uid 10001 or root.

## Operating it

### The `events` commands

| Command | What it does |
|---|---|
| `kite-connect-mcp events status` | The health of the postbacks, the poll and each consumer, as JSON (below). |
| `kite-connect-mcp events tail [--limit N] [--order ID]` | The newest events, oldest first, one JSON object per line: 20 by default, from 1 to 1000. `--order` keeps one order's events. |
| `kite-connect-mcp events pause NAME` | Hold a consumer's deliveries. Events are still recorded, and its deliveries wait, with no `delivery_delayed` alert. |
| `kite-connect-mcp events resume NAME [--skip-backlog]` | Deliver again, the backlog first; a delivery that was retrying starts its tries afresh, as a new one. With `--skip-backlog` the waiting deliveries are marked skipped instead (events you have dealt with by hand). |
| `kite-connect-mcp events retry NAME` | Queue a consumer's failed deliveries again. |
| `kite-connect-mcp events selftest [--url URL]` | Post a signed, made-up postback and check that the answer is 200. |

These commands open the store by its path, so they work while the server runs, and they never create one.

- They need the store named: `KITE_MCP_EVENTS_DB`, or the server's own settings (`KITE_MCP_EVENTS=true` with
  `KITE_MCP_TRANSPORT=http` finds the default store). `status`, `pause`, `resume` and `retry` also read the consumers
  file (`KITE_MCP_EVENTS_CONSUMERS_FILE`) for the consumers' names; without it, `status` lists only the consumers the
  store knows.
- The store is mode 600: run them as the server's user.
- A running server sees a pause, a resume or a retry within about 5 seconds (a paused consumer is looked at every 5
  seconds) to 30 seconds (an idle one).
- A name that is neither in the consumers file nor in the store (a typo) is refused, and so is `retry` for a consumer
  that is no longer in the file, unless it is paused.

On a systemd server set up as in [SELF_HOSTING.md](SELF_HOSTING.md#order-events-on-a-server), the settings file
names the store and the consumers file, and the `kite-mcp-ctl` wrapper runs a command as the server's user with that
file:

```bash
kite-mcp-ctl events status
```

**The selftest** signs a postback for a made-up order id (`selftest-` and 12 random hexadecimal digits) with the
configured API secret, which it reads from the configuration itself and never prints, and posts it.

- Without `--url` it posts to this server's own route on loopback (`http://127.0.0.1:8765/postback` with the default
  port and the path `/postback`), and it refuses to run when this configuration has the event service off: a server
  without it answers a selftest 404, so there would be nothing to test.
- With `--url https://kite.example.com/postback` it tests the whole way a postback travels: DNS, TLS, the proxy, the
  route, the checksum and the store's write. Use the URL registered in the developer console.
- Every run needs the API secret, and without `--url` it also needs `KITE_MCP_POSTBACK_PATH`; it refuses to run
  without them.
- It prints `selftest: POST <url> -> HTTP 200` and exits 0 on a 200; 1 on another answer or when the URL cannot be
  reached; 2 when it refuses to run. Another answer comes with a hint: 403, the server has another API secret; 404, no
  postback route there, or the event service is off there; 503, the server's store cannot write; a redirect, give the
  final URL.
- A 200 means the selftest was stored, as a `selftest` event (counted in `selftest_received`), and the consumers that
  list `selftest` get it. Whether they did is for you to see: `events tail`, `events status`, or the consumer's own
  output (the Telegram example marks it `[test]`).
- The proxy variables (`HTTPS_PROXY` and the others) are used, except for a loopback URL, which a proxy could not
  reach. A user name and password in the URL are printed as `[redacted]`.

### The tools

The `events` toolset has two read-only tools. They read a local file: they never call Kite and never change anything.

| Tool | What it returns |
|---|---|
| `get_order_events` | Events, oldest first, and `last_seq`, the seq of the newest event stored. `order_id` keeps one order's events; `limit` is from 1 to 200 (20 by default). With `after_seq` 0 it returns the newest; to page forward, pass the last event's seq as `after_seq` until a page ends at `last_seq` or comes back empty. |
| `get_event_status` | The same as `events status` (in a reader process, without the poll section, the warning, `configured_consumers` and `login_links`; see below). |

In the serving process they read its store. Any other MCP process (a stdio client on the same machine, say) can read
the same store read-only by naming it in `KITE_MCP_EVENTS_DB`; it must run as a user that can read the file. Such a
reader leaves out the poll's settings and the warning (`"poll": "see the serving process's status"`), and
`configured_consumers`, because those come from the serving process. Without a store both tools are hidden, and
`kite-connect-mcp tools` says why.

### What `events status` shows

```json
{
  "last_seq": 43,
  "counters": {
    "poll_ok": {"count": 312, "last_at": "2026-11-16T15:30:00.412+05:30", "note": "window"},
    "postback_accepted": {"count": 4, "last_at": "2026-11-16T10:15:01.877+05:30", "note": null},
    "postback_refused": {"count": 1, "last_at": "2026-11-16T11:02:40.120+05:30", "note": "bad checksum"}
  },
  "doorbells_waiting": 0,
  "baseline_done": "2026-11-13T09:00:01.250+05:30",
  "after_close_ok": "2026-11-13",
  "consumers": {
    "telegram": {"pending": 0, "retrying": 0, "delayed": 0, "failed": 0, "done": 6, "skipped": 0,
                 "last_done_at": "2026-11-16T10:15:03.040+05:30", "last_error": null},
    "webhook": {"pending": 0, "retrying": 0, "delayed": 0, "failed": 1, "done": 3, "skipped": 0,
                "last_done_at": "2026-11-16T09:20:11.630+05:30", "last_error": "exit 1: webhook.sh: HTTP 404"}
  },
  "paused": {},
  "store": "/var/lib/kite-connect-mcp/events.sqlite3",
  "configured_consumers": ["telegram", "webhook"],
  "poll": {"interval_s": 60, "window": "09:00-16:00", "days": [0, 1, 2, 3, 4], "in_window_now": true}
}
```

| Field | Meaning |
|---|---|
| `last_seq` | The seq of the newest event ever stored. |
| `counters` | Each with its `count`, the time of the last one (`last_at`) and a `note`: `postback_accepted`; `postback_refused` (the note is the last reason); `selftest_received`; `poll_ok` (why the last poll ran); `poll_failed` (the last error); `doorbell_unknown_order`, `doorbell_wrong_order` and `doorbell_gave_up`. A counter shows up once it has moved. |
| `doorbells_waiting` | Rung orders not read from Kite yet: they wait for a token, or for Kite. |
| `baseline_done`, `after_close_ok` | When the first poll recorded the baseline; the last day a poll succeeded after the window closed. |
| `consumers` | For each consumer: `pending` (every waiting delivery, the retrying ones too), `retrying`, `delayed` (alerted), `failed`, `done`, `skipped`, the time of the last delivery done and the last failure's error. |
| `paused` | The paused consumers, and since when. |
| `store`, `configured_consumers` | The store's path; the names in the consumers file. |
| `poll` | The poll's interval, window and days (0 is Monday, 6 Sunday), and whether it is inside the window now. These come from the settings of the process that runs the command, so give it the server's poll settings. |
| `warning` | Present while no poll has succeeded after the latest close of the poll window, and names that day: "no successful poll after the close on Fri 2026-11-13: fills after the last poll may be missing". A missed Friday stays visible over the weekend, until Monday's close is polled. |
| `login_links` | Present with a link command (`KITE_MCP_LOGIN_LINK_COMMAND`): the [scheduled login links](#scheduled-login-links)' times and days, the next time, the last time handled (`last_scheduled_for`) and the last attempt. Like `poll`, it comes from the settings of the process that runs the command, and the state folder it names. |

It holds no event, no order id and no login link.

### Retention

Housekeeping runs when the service starts and every 6 hours. It deletes the events older than
`KITE_MCP_EVENTS_KEEP_DAYS` days (30) with their deliveries (so also a paused consumer's backlog that old), the order
states not updated since, and doorbells older than two days.

### The store and the service

- The store is one SQLite file in WAL mode (with its `-wal` and `-shm` files beside it), mode 600; its folder is made
  with mode 700 when it does not exist. The default is `<state dir>/events.sqlite3`.
- An event, its order's new state and a pending delivery for every matching consumer are written in one transaction,
  so a crash leaves all of them or none.
- The file records its schema version. A store from another version of kite-connect-mcp, or a database that is not an
  event store, is refused when it is opened, not misread or taken over.
- One process serves a store. The server holds a lock file beside it (`events.sqlite3.lock`), and a second server on
  the same store fails to start: "another process is already serving order events from ...".
- The service starts with the HTTP server. If it cannot start (a bad consumers file, the lock, a store it cannot
  open), the server stops instead of running without it.

## Scheduled login links

You log in to Kite yourself every day; these links bring the login to you. At the times you set, when no valid token
is stored, the server issues a login link and hands it to one program of yours, the link command, which sends it to
you: a Telegram message, an e-mail, a push notification. You tap it, log in at Kite, and Kite's redirect brings the
new token back to the server. The links work with the order events on or off; only the link after a rejected token
needs them. [examples/README.md](../examples/README.md#a-login-link-command) has a link command for Telegram.

### Settings

| Setting | Default | Meaning |
|---|---|---|
| `KITE_MCP_LOGIN_REMINDER_TIMES` | unset | The times, in IST: `HH:MM`, comma-separated, such as `07:30,08:30`. Needs a link command. |
| `KITE_MCP_LOGIN_REMINDER_DAYS` | `Mon-Fri` | The days, written as `KITE_MCP_ORDER_POLL_DAYS` is. |
| `KITE_MCP_LOGIN_REMINDER_SKIP_DATES_FILE` | unset | A file of dates with no scheduled link, such as exchange holidays (below). |
| `KITE_MCP_LOGIN_LINK_COMMAND` | unset | The link command: the absolute path of one executable, run with no arguments. |
| `KITE_MCP_LOGIN_LINK_ENV_PASS` | empty | Names of the server's environment variables the command gets, such as `TELEGRAM_BOT_TOKEN,TELEGRAM_CHAT_ID`. Never a `KITE_*` name. |
| `KITE_MCP_LOGIN_LINK_TIMEOUT_S` | `30` | Seconds the command may run, from 1 to 600. |

Times or a command need `KITE_MCP_AUTH_MODE=managed` and `KITE_MCP_PUBLIC_URL`, since a link is of use only when
Kite's redirect reaches this server's callback (checked with the other settings), and the HTTP transport (checked when
`serve` starts: the server does not start without it). A command without times sends only the links after a rejected
token and the ones asked for. Put these settings in the settings file that the commands read too, so that
`kite-connect-mcp status` shows the links. The full rules: [CONFIGURATION.md](CONFIGURATION.md#scheduled-login-links).

```bash
KITE_MCP_LOGIN_REMINDER_TIMES=07:30,08:30
KITE_MCP_LOGIN_REMINDER_SKIP_DATES_FILE=/etc/kite-connect-mcp/skip-dates.txt
KITE_MCP_LOGIN_LINK_COMMAND=/usr/local/bin/send-kite-link
KITE_MCP_LOGIN_LINK_ENV_PASS=TELEGRAM_BOT_TOKEN,TELEGRAM_CHAT_ID
```

**The skip-dates file** holds one `YYYY-MM-DD` per line; `#` starts a comment, on a line of its own or after a date,
and blank lines are fine. It must be a regular file of at most 64 KiB, in UTF-8 (a byte-order mark is fine). It is
read again each time it is needed, so a new list takes effect without a restart. A file that is missing, unreadable,
not a regular file or too large is a warning in the log and skips nothing, and so is a line that is not a date (the
other dates still apply): a link on a holiday is better than none on a trading day.

```
# exchange holidays: no scheduled link on these days
2026-12-25
2027-01-26  # a comment after a date
```

### When a link goes out

| `reason` | When |
|---|---|
| `scheduled` | At a reminder time, on a reminder day that is not a skip date, when no valid token is stored. With a valid token (not expired, and not rejected by Kite) nothing is sent, and the time is recorded as skipped. |
| `token_rejected` | Right after the event service raised `token_rejected` (so only with `KITE_MCP_EVENTS=true`), when still no valid token is stored; at most one every two hours, counted from the last such attempt whatever its outcome, across a restart too. A rejection inside those two hours gets no link, then or later: the next scheduled time is the one. The days and the skip dates do not apply: a token was in use that day. A scheduled time due in the same check as such a link that went out is recorded as skipped: you just got one. |
| `requested` | An agent called `login(deliver=true)`. The link goes to you, not to the agent: its answer says whether the link was sent (delivered), when it expires, the AI-risk warning and any notes, and on a failure the short error, never the link. As with every `login`, a valid token means no link unless force=true. |

With `KITE_MCP_LOGIN_ALLOW_FORCE=false`, `login(deliver=true)` issues no link while a valid token exists either, even
with force=true (the refusal is audited as `login_link_refused`).

**A missed time is caught up, once.** The server checks at each reminder time and at least every 30 seconds. A time
it missed, because it was down or the machine slept, is sent when it comes back if that is within 3 hours of the
time: only the latest such time, once; older ones are dropped. Every scheduled attempt (sent, skipped or failed)
records its time in the state file (`last_scheduled_for`), so neither the next check nor a restart sends that time
again, and a time recorded as skipped because a valid token existed is never caught up later. A first start counts
too. With the times `07:30,08:30` on a weekday with no valid token:

- the server is down from 07:00 and comes back at 09:10: it sends the 08:30 link at once, and the 07:30 one never;
- it comes back at 11:45 instead: it sends nothing, as 08:30 is more than 3 hours past; the next link is the next
  reminder time;
- it is stopped while the command runs: a command still running 10 seconds later is killed and nothing is recorded,
  so a restart within the 3 hours sends that time again.

A late link is logged as caught up. A failed link is not retried: the next scheduled time is the retry.

### The link command

| | |
|---|---|
| Program | `KITE_MCP_LOGIN_LINK_COMMAND`: one absolute path, no arguments and no whitespace, run without a shell. It must be an executable regular file that only the server's user or root can change, reached through folders and links only they can change, as for consumer programs ([File permissions](#file-permissions)). The server checks it when it starts (and does not start otherwise) and again before each run (that run then fails). |
| Standard input | One JSON object, then a newline (below). |
| Environment | `PATH`, `HOME` (when set), `LANG` and the variables named in `KITE_MCP_LOGIN_LINK_ENV_PASS` that are set. Never a `KITE_*` variable. |
| Standard output and error | Discarded: nothing the command prints is kept, logged or shown. Run it by hand to see its messages. |
| Exit | 0: sent. Anything else is a failed attempt with a short error: `exit 3`, `killed by SIGTERM`, `timed out after 30 s`, `cannot start the command: ...`. |
| Timeout | `KITE_MCP_LOGIN_LINK_TIMEOUT_S` (30 seconds). Past it, the command and everything it started (its process group: it runs in a session of its own) are killed. |
| Retries | None: the next scheduled time is the retry. |

```json
{"url": "https://kite.zerodha.com/connect/login?v=3&api_key=...", "expires_at": "2026-11-16T08:30:00+05:30", "reason": "scheduled"}
```

`url` is the login link; `expires_at` when it stops working (`KITE_MCP_LOGIN_LINK_TTL_S` after it was issued, and
never past 05:59 IST); `reason` is `scheduled`, `token_rejected` or `requested`. Whoever holds the link can complete
one login to your account with it until it expires: send it only to yourself, and never print or log it.

### What is never stored or logged

The link: it is never stored (the server keeps only a digest of its one-time state, as for every login link), never
logged, and never put in an event, a status, an audit line, an error or the answer of `login(deliver=true)`. The
command's output is discarded for the same reason. The audit log records each link issued (`login_link_issued`, with
its source, `scheduled`, `token_rejected` or `tool`, and its expiry) and each refused (`login_link_refused`); the
server's log has one line per attempt (sent, skipped or failed, with the short error).

### The status and the state file

With a link command, `session_status`, `kite-connect-mcp status`, `kite-connect-mcp events status` and, in the
serving process, `get_event_status` show `login_links`:

```json
{"times": ["07:30", "08:30"], "days": [0, 1, 2, 3, 4], "next_at": "2026-11-16T07:30+05:30", "last_scheduled_for": "2026-11-13T08:30+05:30", "last": {"at": "2026-11-13T08:30:00+05:30", "reason": "scheduled", "outcome": "skipped", "error": null, "link_expires_at": null}}
```

| Field | Meaning |
|---|---|
| `times`, `days` | The reminder times (IST) and days (0 is Monday, 6 Sunday). |
| `next_at` | The next reminder time on a reminder day that is not a skip date, within 31 days; null when there is none. |
| `last_scheduled_for` | The last reminder time handled: sent, skipped or failed. |
| `last` | The last attempt, whatever its reason: when (`at`), the `reason`, the `outcome` (`sent`; `skipped`, because a valid token existed or a link for a rejected token had just gone out; or `failed`), the short `error` of a failed one, and when its link expires (`link_expires_at`, for a link that was issued). |

The state behind it is `<state dir>/login_links.json`, mode 600: the last attempt, the last time handled and when the
last link after a rejected token was tried (where the two hours start). It never holds a link. A missing or damaged
file reads as empty, and a time in it later than now is ignored. Beside it, `login_links.json.lock` is held while the
server sends links: one serving process per state folder, as two would both send each time. A second server on the
same `KITE_MCP_STATE_DIR` does not start ("another process is already sending login links from the state folder
...").

## Security notes

- **A forged postback** (made without the API secret) fails the checksum: a bare 403, counted, nothing stored.
- **A replayed or altered postback** (a real one, captured) passes the checksum, but only rings the doorbell: it costs
  one read of that order from Kite, plus one in each later round while Kite fails, until it is given up. What is
  stored is Kite's own answer, so a replay can neither invent a state nor hold an order in an old one. A repeat (the
  same JSON) within 10 seconds costs no write at all, and one order's doorbell is written at most once every 2 seconds
  however its postbacks vary.
- **No payload in the logs.** The route logs nothing from a request (no order id, checksum or user), and logs its
  refusals, and the accepted postbacks, at most once a minute per reason.
- **The consumers' environment is minimal:** argv only, no shell, `PATH`, `HOME`, `LANG`, `KITE_EVENT_CONSUMER` and the
  names in `env_pass`, never a `KITE_*` variable. The consumers file and the programs must not be changeable by others
  (see [File permissions](#file-permissions)).
- **The store** holds what your orders were (symbols, quantities, prices, tags), and no account or user id: it is mode
  600, readable by the server's user only.
- **One serving process per store** (the lock): two servers on one store can never run the same consumers for the
  same events.
- **An event is a notice, not the book of record.** A consumer that acts on an event, for example one that places an
  order when another fills, should read what it relies on (the order's status, the fill) from Kite itself and apply its
  own checks before it acts. Anyone who can change the server's settings or the consumers file, or run as its user,
  can run your commands with an event of their own making.

## Limits

- **No WebSocket feed yet.** Kite advises WebSocket order updates for individual developers, as they cover every order
  at once; the events use postbacks and the poll. Personal apps cannot use the WebSocket at all, so there postbacks plus
  the poll are the way to hear about orders.
- **Orders from outside this API key are seen by the poll only:** up to one interval late (60 seconds by default), and
  only on poll days, inside the window, or by the polls at start-up, after a login and after the close.
- **A service down across the close cannot catch up.** Kite's order book holds only today's orders, so what changed
  after the last poll is gone from it the next day. `events status` then shows the warning "no successful poll after
  the close on ...".
- **No exchange timestamps, no certainty.** See the known limit under
  [Deduplication](#deduplication-and-why-progress-only-moves-forward).
- **Doorbells wait for a token.** Before the day's login nothing is read; the events arrive right after it.
- **One account.** The events follow the account whose token the server holds.
