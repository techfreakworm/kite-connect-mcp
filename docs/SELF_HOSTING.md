# Self-hosting kite-connect-mcp

This guide runs the server on your own machine (part A) or on a server you control (part B). You need a Kite Connect
app first; [KITE_SETUP.md](KITE_SETUP.md) walks through creating it, the redirect URL and the static IP.

| You want | Use |
|---|---|
| One assistant on your laptop (Claude Desktop, Claude Code, an editor) | [Part A](#part-a-running-it-locally): stdio, nothing to host |
| An always-on server shared by several clients or scheduled agents | [Part B](#part-b-hosting-it-on-a-server): HTTP behind a TLS reverse proxy, with systemd or Docker |

Contents: [Part A](#part-a-running-it-locally) ·
[Part B](#part-b-hosting-it-on-a-server) ·
[Sharing a token with another program](#sharing-a-token-with-another-program-external-mode) ·
[Backups](#backups) · [Upgrades](#upgrades) · [Logs](#logs) · [Troubleshooting](#troubleshooting)

## Part A: running it locally

The server talks to your assistant over standard input and output. The client starts it when it needs it; you do not
run it by hand.

### Install

You need Python 3.11 or newer. There is no PyPI release yet; install from GitHub with any of these:

```bash
uv tool install git+https://github.com/techfreakworm/kite-connect-mcp
# or
pipx install git+https://github.com/techfreakworm/kite-connect-mcp
# or plain pip in a virtual environment
python3 -m venv ~/.venvs/kite-connect-mcp
~/.venvs/kite-connect-mcp/bin/pip install git+https://github.com/techfreakworm/kite-connect-mcp
```

Check it with `kite-connect-mcp --version` (with plain pip, use `~/.venvs/kite-connect-mcp/bin/kite-connect-mcp`).
`uv` and `pipx` put the command in `~/.local/bin`; make sure that folder is on your `PATH`.

### Configure

Settings come from environment variables or from a file passed with `--env-file`. Keep the API secret in its own file
that only you can read, and refer to it with `KITE_API_SECRET_FILE`:

```bash
mkdir -p ~/.config/kite-connect-mcp && chmod 700 ~/.config/kite-connect-mcp
touch ~/.config/kite-connect-mcp/api_secret ~/.config/kite-connect-mcp/env
chmod 600 ~/.config/kite-connect-mcp/api_secret ~/.config/kite-connect-mcp/env
```

Put the secret from the developer console (on one line, nothing else) in `api_secret`, and the rest in `env`:

```bash
KITE_API_KEY=your_api_key
KITE_API_SECRET_FILE=/home/you/.config/kite-connect-mcp/api_secret
KITE_MCP_ALLOWED_USER_ID=AB1234
```

Use your real home folder (`/Users/you` on macOS) and your own client ID. Every other setting is optional and keeps its
safe default: dry-run trading, a ₹10,000 per-order cap, loopback-only HTTP. The full list is in
[CONFIGURATION.md](CONFIGURATION.md).

`--env-file` is a global option and goes **before** the subcommand. To avoid typing it every time:

```bash
alias kcm='kite-connect-mcp --env-file ~/.config/kite-connect-mcp/env'
```

### Check, log in, look at the status

```bash
kcm check-config     # validate the settings and print them; credentials show only as set/unset
kcm login --listen   # print the Kite login link and wait for the redirect (see below)
kcm status           # session and safety status, no secrets
kcm status --verify  # also make one read-only call to Kite to prove the token works
```

- `check-config` stops with a list of problems and exit status 2 if anything is wrong. It does not insist that the API
  key is set, so look for `"api_key": "set"`.
- For `login --listen` register `http://127.0.0.1:8765/callback` as your app's redirect URL. The command starts a
  temporary local callback, prints a link, and finishes when you have logged in. If the redirect cannot reach it, log in
  anyway, copy the `request_token` value from the address bar of the page you land on, and run
  `kcm login --request-token <value>`. The token is single-use and expires within minutes.
- `kcm login` without options prints the link for use with a server's `/callback`; see
  [the daily login on a server](#the-daily-login-on-a-server).
- You do this once per trading day. Kite tokens expire at about 06:00 IST; see
  [KITE_SETUP.md](KITE_SETUP.md#7-the-daily-login).

### Connect an assistant

All the clients below run the same command, `kite-connect-mcp serve`. GUI applications do not inherit your shell's
`PATH`, so give them the full path of the command (`command -v kite-connect-mcp`).

**Claude Desktop** (Settings, Developer, Edit Config; on macOS the file is
`~/Library/Application Support/Claude/claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "kite": {
      "command": "/home/you/.local/bin/kite-connect-mcp",
      "args": ["--env-file", "/home/you/.config/kite-connect-mcp/env", "serve"]
    }
  }
}
```

Putting `--env-file` in `args` keeps the credentials out of the client's own config file. If you prefer, pass them as
`"env": { "KITE_API_KEY": "...", "KITE_API_SECRET_FILE": "..." }` instead. Restart Claude Desktop after editing.

**Claude Code:**

```bash
claude mcp add kite --scope user \
  --env KITE_API_KEY=your_api_key \
  --env KITE_API_SECRET_FILE=$HOME/.config/kite-connect-mcp/api_secret \
  -- kite-connect-mcp serve
claude mcp list
```

`--scope user` makes it available in every project; leave it out for the current project only. The `--` separates
Claude Code's options from the server command. The options of `claude mcp add` change between releases, so check
`claude mcp add --help` if this errors.

**Other stdio clients** (Cursor, Zed, Cline, VS Code and others) take the same three things: a command
(`kite-connect-mcp`), arguments (`["serve"]`, or `["--env-file", "...", "serve"]`) and optionally environment
variables. Look for "MCP servers" in the client's settings.

**Clients that speak HTTP** connect to a server you host in part B. They need two things: the URL
`https://kite.example.com/mcp` and the header `Authorization: Bearer <your bearer token>`.

```bash
# Claude Code, directly over HTTP (the header is stored in Claude Code's config file)
claude mcp add --transport http kite https://kite.example.com/mcp \
  --header "Authorization: Bearer $KITE_MCP_BEARER"
```

For a client that can only start local programs, bridge with `mcp-remote` (it needs Node.js):

```bash
npx mcp-remote https://kite.example.com/mcp --header "Authorization: Bearer ${KITE_MCP_BEARER}"
```

In a client's JSON configuration, with the token in the environment variable:

```json
{
  "mcpServers": {
    "kite": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "https://kite.example.com/mcp",
               "--header", "Authorization: Bearer ${KITE_MCP_BEARER}"],
      "env": { "KITE_MCP_BEARER": "your_bearer_token" }
    }
  }
}
```

`mcp-remote` is a separate project: check its README for the current options if a client mangles the space inside the
header argument.

**First checks.** Ask the assistant to call `session_status`, then `get_profile`. Then ask it to dry-run a small order:
nothing is sent, and the answer shows the exact request, the checks and Kite's margin calculation. See
[SAFETY.md](SAFETY.md).

## Part B: hosting it on a server

The server listens on `127.0.0.1:8765` over plain HTTP. A reverse proxy in front terminates TLS and forwards only the
few paths that should be public:

```
  MCP clients ---- https + bearer token ----+
  your browser (login redirect) ---- https -+--> Caddy or nginx :443 --> kite-connect-mcp 127.0.0.1:8765
  Kite postbacks (optional) ---- https -----+      (TLS, path allow-list)        |
                                                                                 | https and wss, from the static IP
                                                                                 v
                                                                      api.kite.trade, ws.kite.trade
```

| Path | Who calls it | Protection |
|---|---|---|
| `/mcp` | Your MCP clients | Bearer token |
| `/callback` | Your browser, redirected by Kite after you log in (path set by `KITE_MCP_CALLBACK_PATH`) | One-time `state` and `request_token`; the server checks them |
| `/postback` | Kite, if you use postbacks (path set by `KITE_MCP_POSTBACK_PATH`; off by default) | SHA-256 checksum with your API secret |
| `/health` | You, or an uptime monitor | None. It answers `{"status":"ok","version":...}`; leave it off the proxy if nothing outside needs it |

Everything else should be a 404 from the proxy. The HTTP transport is stateless, so restarting or upgrading the server
does not break your clients' sessions.

### 1. Before you start

- A Linux machine you control, with Python 3.11+ (3.12 is what the Docker image uses), or Docker. A small VPS is enough.
  Check `python3 --version`: Ubuntu 22.04 ships 3.10, which is too old (Ubuntu 24.04 and Debian 12 are fine); use Docker
  there.
- A DNS name for it, for example `kite.example.com`, pointing at the machine.
- For **live orders**, the machine's outbound address registered as your static IP. Do this before you rely on it; see
  [Static IP for live orders](KITE_SETUP.md#static-ip-for-live-orders) and [the static IP](#the-static-ip) below.
- Your Kite app's redirect URL set to `https://kite.example.com/callback`.
- SSH by key only, a patched system and a firewall (see [Firewall](#firewall)). This process holds the means to trade
  your account.

### 2. Create the user, the folders and the install (systemd)

If you prefer Docker, skip to [Docker](#4-or-run-it-with-docker).

```bash
# a user with no login and no password, and the folders it uses
sudo useradd --system --user-group --home-dir /var/lib/kite-connect-mcp --shell /usr/sbin/nologin kite-mcp
sudo install -d -m 0750 -o root     -g kite-mcp /etc/kite-connect-mcp      # settings and secrets (read-only to the service)
sudo install -d -m 0700 -o kite-mcp -g kite-mcp /var/lib/kite-connect-mcp  # state: token, audit log, kill switch
sudo install -d -m 0700 -o kite-mcp -g kite-mcp /var/cache/kite-connect-mcp # cache: instrument lists, safe to delete

# the program, in a virtual environment that only root can change
sudo apt install python3-venv git      # Debian and Ubuntu; use your distribution's equivalent
sudo python3 -m venv /opt/kite-connect-mcp/venv
sudo /opt/kite-connect-mcp/venv/bin/pip install git+https://github.com/techfreakworm/kite-connect-mcp
/opt/kite-connect-mcp/venv/bin/kite-connect-mcp --version
```

| Path | Owner and mode | Holds |
|---|---|---|
| `/etc/kite-connect-mcp/` | `root:kite-mcp`, 0750 | the settings file and the secret files |
| `/etc/kite-connect-mcp/*` | `kite-mcp:kite-mcp`, 0600 | one value or one settings file each |
| `/var/lib/kite-connect-mcp/` | `kite-mcp:kite-mcp`, 0700 | `access_token.json`, `audit/`, `KILL_SWITCH` |
| `/var/cache/kite-connect-mcp/` | `kite-mcp:kite-mcp`, 0700 | downloaded instrument lists |
| `/opt/kite-connect-mcp/venv/` | `root:root` | the code, which the service cannot modify |

Create the secret files and the settings file. Use `sudoedit` so that the values do not land in your shell history:

```bash
sudo install -m 0600 -o kite-mcp -g kite-mcp /dev/null /etc/kite-connect-mcp/api_secret
sudoedit /etc/kite-connect-mcp/api_secret            # paste the API secret on one line

sudo install -m 0600 -o kite-mcp -g kite-mcp /dev/null /etc/kite-connect-mcp/bearer_token
openssl rand -hex 32 | sudo tee /etc/kite-connect-mcp/bearer_token > /dev/null

sudo install -m 0600 -o kite-mcp -g kite-mcp /dev/null /etc/kite-connect-mcp/kite-connect-mcp.env
sudoedit /etc/kite-connect-mcp/kite-connect-mcp.env
sudo ls -l /etc/kite-connect-mcp                     # every file: -rw------- kite-mcp kite-mcp
```

The settings file:

```bash
KITE_API_KEY=your_api_key
KITE_API_SECRET_FILE=/etc/kite-connect-mcp/api_secret
KITE_MCP_BEARER_TOKEN_FILE=/etc/kite-connect-mcp/bearer_token
KITE_MCP_PUBLIC_URL=https://kite.example.com
KITE_MCP_ALLOWED_USER_ID=AB1234
KITE_MCP_STATE_DIR=/var/lib/kite-connect-mcp
KITE_MCP_CACHE_DIR=/var/cache/kite-connect-mcp
```

Keep the default safety settings (dry-run) until everything works; see [SAFETY.md](SAFETY.md) before you change them.
Write plain `KEY=VALUE` lines with no quotes and no comment after a value, because systemd, Docker and the server's own
`--env-file` read those differently. A whole line starting with `#` is fine.

Run commands as the service user so that the files they create belong to it. A small wrapper makes that easy:

```bash
sudo tee /usr/local/bin/kite-mcp-ctl > /dev/null <<'EOF'
#!/bin/sh
# run kite-connect-mcp as the service user, with the service's settings
exec sudo -u kite-mcp /opt/kite-connect-mcp/venv/bin/kite-connect-mcp --env-file /etc/kite-connect-mcp/kite-connect-mcp.env "$@"
EOF
sudo chmod 755 /usr/local/bin/kite-mcp-ctl
kite-mcp-ctl check-config
```

If you run a command such as `login` as root, the token or audit files would be created with the wrong owner and the
service could not write them.

### 3. Run it with systemd

The repository ships a hardened unit in [`deploy/systemd/`](../deploy/systemd/kite-connect-mcp.service). Get it from a
clone of the repository:

```bash
git clone https://github.com/techfreakworm/kite-connect-mcp
sudo cp kite-connect-mcp/deploy/systemd/kite-connect-mcp.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now kite-connect-mcp
systemctl status kite-connect-mcp
curl -s http://127.0.0.1:8765/health
```

What the unit does:

| Setting | Effect |
|---|---|
| `User=kite-mcp`, `EnvironmentFile=` | Runs unprivileged; systemd reads the settings file for it |
| `ExecStart=... serve --transport http` | Always HTTP, even if a settings file says `stdio` |
| `Restart=on-failure`, `RestartSec=5s`, `StartLimitBurst=5` | Comes back after a crash, and gives up after 5 failed starts in 5 minutes (a bad setting, say). After fixing the cause: `sudo systemctl reset-failed kite-connect-mcp`, then start it again |
| `UMask=0077` | Every file it creates is private to the service user |
| `ProtectSystem=strict`, `ReadWritePaths=` | The whole file system is read-only except the state and cache folders |
| `ProtectHome=true`, `PrivateTmp=true`, `PrivateDevices=true` | No home folders, a private `/tmp`, no hardware devices |
| `NoNewPrivileges=true`, empty `CapabilityBoundingSet=` | It cannot gain privileges, and has no capabilities |
| `ProtectKernel*`, `ProtectControlGroups`, `ProtectClock`, `ProtectHostname`, `ProtectProc=invisible` | Kernel, cgroups, clock and other processes are out of reach |
| `RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX`, `RestrictNamespaces`, `RestrictRealtime`, `RestrictSUIDSGID`, `LockPersonality` | No raw sockets, namespaces, realtime scheduling or setuid files |
| `SystemCallFilter=@system-service`, `~@privileged @resources` | Only the system calls an ordinary service needs |

If you move the state or cache folder, change `ReadWritePaths=` to match. A service that fails with
`status=226/NAMESPACE` is missing one of those folders. `systemd-analyze security kite-connect-mcp` rates the result.

### 4. Or run it with Docker

The repository has a [`Dockerfile`](../Dockerfile) (Python 3.12, built in two stages, running as the unprivileged user
with uid 10001) and a [`docker-compose.example.yml`](../docker-compose.example.yml). No image is published: you build it.

```bash
git clone https://github.com/techfreakworm/kite-connect-mcp && cd kite-connect-mcp
cp docker-compose.example.yml docker-compose.yml
touch kite-connect-mcp.env && chmod 600 kite-connect-mcp.env
$EDITOR kite-connect-mcp.env      # see below
docker compose up -d --build
docker compose ps                 # should say (healthy) after a few seconds
docker compose logs -f kite-connect-mcp
```

The settings file for Docker is minimal. **Do not copy `.env.example` into it**: that file sets
`KITE_MCP_TRANSPORT=stdio` and `KITE_MCP_HOST=127.0.0.1`, which would override what the image needs (HTTP on
`0.0.0.0` inside the container):

```bash
KITE_API_KEY=your_api_key
KITE_API_SECRET=your_api_secret
KITE_MCP_BEARER_TOKEN=paste the output of openssl rand -hex 32 here
KITE_MCP_PUBLIC_URL=https://kite.example.com
KITE_MCP_ALLOWED_USER_ID=AB1234
```

(Replace the bearer token's placeholder text with the generated value, with no spaces. The server refuses to start while
it contains spaces or is shorter than 32 characters.)

What the compose file sets up:

- **Non-root, read-only.** The container runs as uid 10001 with a read-only root file system (`read_only: true`, a tmpfs
  on `/tmp`), no Linux capabilities (`cap_drop: [ALL]`) and `no-new-privileges`.
- **Loopback only.** `ports: "127.0.0.1:8765:8765"` publishes the port on the host's loopback address, so only the
  reverse proxy on the same machine can reach it. Never publish it on `0.0.0.0`: Docker adds its own firewall rules that
  a firewall tool such as `ufw` does not govern.
- **A volume.** `/var/lib/kite-connect-mcp` holds the token file, the audit log, the kill switch and the cache (the image
  sets `KITE_MCP_STATE_DIR` and `KITE_MCP_CACHE_DIR` under it). The named volume survives upgrades.
- **A health check.** The image asks `GET /health` on the container's loopback address every 30 seconds.
- **A bearer token is required.** The image binds to `0.0.0.0` inside the container, and the server refuses to start in
  that case without `KITE_MCP_BEARER_TOKEN` (or `_FILE`). The `Host` header check accepts `127.0.0.1:<any port>`, so
  requests through the published port work; for a proxy and a public name, set `KITE_MCP_PUBLIC_URL`.

Run the CLI inside the container, as the right user:

```bash
docker compose exec kite-connect-mcp kite-connect-mcp status
docker compose exec kite-connect-mcp kite-connect-mcp login
```

**Secrets.** `env_file` is the simplest route, because Docker reads it as root and the container user never needs file
access to it; keep it mode 600. If you would rather use `*_FILE` variables, the server refuses any secret file that
group or others can read, and a file bind-mounted from the host keeps its host owner and mode. So it must be owned by
uid 10001 with mode 600 (`sudo chown 10001:10001 file && sudo chmod 600 file`). Secrets that Docker or Compose mounts
under `/run/secrets` are often world-readable, which the server then rejects; check with
`docker compose exec kite-connect-mcp ls -l /run/secrets`.

**A bind mount instead of a named volume.** The host folder must belong to uid 10001, or the server starts but cannot
write (`PermissionError: ... '/var/lib/kite-connect-mcp/state'`, at the first login or the first dry run):

```bash
sudo install -d -m 0700 -o 10001 -g 10001 /srv/kite-connect-mcp
# in docker-compose.yml: volumes: ["/srv/kite-connect-mcp:/var/lib/kite-connect-mcp"]
```

### 5. Put a TLS reverse proxy in front

Run either one; the server itself speaks plain HTTP on loopback. Both examples expose only `/mcp` (protected by the
bearer token), `/callback`, `/postback` and `/health`, and answer every other path with a 404 themselves. Delete the
blocks you do not need. Both write **no access log**, because the callback URL carries one-time values.

The commands below assume you are in the folder that holds your clone of the repository
(`git clone https://github.com/techfreakworm/kite-connect-mcp`).

**Caddy** (automatic HTTPS). Install Caddy from your distribution or from Caddy's own packages, point the DNS name at the
machine, open ports 80 and 443, then:

```bash
sudo cp kite-connect-mcp/deploy/caddy/Caddyfile.example /etc/caddy/Caddyfile
sudoedit /etc/caddy/Caddyfile        # replace kite.example.com with your name
sudo systemctl reload caddy
```

[`deploy/caddy/Caddyfile.example`](../deploy/caddy/Caddyfile.example) also redacts `request_token` and `state` from
Caddy's error log, which would otherwise show the full URI when the server is down.

**nginx with certbot.** The certificate must exist before nginx will load the HTTPS block, so install the example in two
steps. The comments at the top of
[`deploy/nginx/kite-connect-mcp.conf.example`](../deploy/nginx/kite-connect-mcp.conf.example) list them: first only the
port-80 block, then `sudo certbot certonly --webroot -w /var/www/certbot -d kite.example.com`, then the whole file in
`/etc/nginx/conf.d/`, then `sudo nginx -t && sudo systemctl reload nginx`. certbot renews the certificate by itself.
nginx's error log can quote the request line when the server is down, so keep `/var/log/nginx` private.

**Test it from another machine:**

```bash
curl -i https://kite.example.com/health                 # 200 and {"status":"ok",...}
curl -i -X POST https://kite.example.com/mcp            # 401: the bearer token is required
curl -i https://kite.example.com/admin                  # 404 from the proxy
```

### 6. The bearer token

Every request to `/mcp` must carry `Authorization: Bearer <token>`; otherwise the answer is a bare 401.

- Generate it with `openssl rand -hex 32` (64 hex characters). The server accepts 32 characters or more with no
  whitespace, and refuses to start with a shorter one. The comparison is constant-time.
- Keep it in `KITE_MCP_BEARER_TOKEN_FILE` (mode 600), or in `KITE_MCP_BEARER_TOKEN` in a private env file for Docker.
- Treat it like a password: whoever holds it can use every tool you expose, including live orders within your caps.
  Never put it in a URL.
- To rotate it, write a new value into the file, restart the service (`sudo systemctl restart kite-connect-mcp`), and
  update each client.
- There is one token for the whole server, not one per client.

### 7. Host-header checks: `KITE_MCP_PUBLIC_URL` and `KITE_MCP_ALLOWED_HOSTS`

To block DNS-rebinding attacks, `/mcp` accepts only requests whose `Host` header is one of:

- `127.0.0.1`, `localhost` and `[::1]` (any port), always;
- the host of `KITE_MCP_PUBLIC_URL` (for `https://kite.example.com`, the name `kite.example.com`, with or without a
  port);
- each name in `KITE_MCP_ALLOWED_HOSTS` (comma-separated; an entry without a port also matches any port).

Any other `Host` gets `421 Invalid Host header`. A request that carries an `Origin` header must come from the same set
of places, or it gets `403 Invalid Origin header`; ordinary MCP clients send no `Origin`.

So behind a proxy, set `KITE_MCP_PUBLIC_URL=https://kite.example.com` (include `https://`; a value without a scheme is
ignored) and make sure the proxy passes the original `Host` through. The Caddy and nginx examples do. Use
`KITE_MCP_ALLOWED_HOSTS` for additional names, such as a private VPN name. Through the loopback port that Docker
publishes, no setting is needed.

### Firewall

Allow SSH, 80 and 443 in; nothing else. The server's own port (8765) must **not** be reachable from outside.

```bash
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow 22/tcp     # your SSH port; set this BEFORE enabling, or you lock yourself out
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw enable
sudo ufw status verbose
```

Also use your cloud provider's firewall if it has one. If you filter outbound traffic, the server itself talks only to
`api.kite.trade` (HTTPS) and, on the Connect plan, `ws.kite.trade` (WebSocket); your browser, not the server, talks to
`kite.zerodha.com`. A Docker setup should publish only on `127.0.0.1`, as the compose example does.

### The static IP

Live order requests must leave the machine from the address you registered in the developer console. Reads, the
WebSocket and everything else may come from anywhere. The full rules (one change per calendar week, at most two
addresses, your own and your family's only) are in [KITE_SETUP.md](KITE_SETUP.md#static-ip-for-live-orders).

- **Know which address Kite sees.** On a cloud VPS it is usually the public IPv4 address of the instance; behind NAT or a
  shared gateway it is the gateway's address, which you may not be allowed to register. The provider's console shows it,
  and so does asking a "what is my IP" service from the server with `curl -4` and with `curl -6`.
- **IPv6.** If the machine has IPv6 and its outbound connections use it, Kite sees the IPv6 address. Register that one
  too (you may register two), or make outbound traffic prefer IPv4.
- **No static IP on this machine?** Send the server's requests to Kite through a forward proxy on a host that has one, by
  adding `HTTPS_PROXY=http://203.0.113.10:3128` (your proxy's address) to the settings file that systemd or Docker reads.
  The server's HTTP client honours the standard proxy variables. Register the proxy's address with Zerodha. The variable
  has to be in the process environment: one that is only in a file passed to `--env-file` is not exported and has no
  effect, so give it to a client through its `env` block instead. Check your proxy's logs for a CONNECT to
  `api.kite.trade:443` after the first request.
- **Reading and dry runs work without any of this.** You only need the static IP when you want to send a live order.

### The daily login on a server

You log in once per trading day. On a server it takes about half a minute and works from your phone.

1. Ask the assistant to call the `login` tool, or run `kite-mcp-ctl login` (Docker:
   `docker compose exec kite-connect-mcp kite-connect-mcp login`). It prints a link to `kite.zerodha.com`.
2. Open the link on your phone or laptop and log in with your Zerodha user ID, password and TOTP.
3. Kite redirects your browser to `https://kite.example.com/callback`. The server checks the one-time state, exchanges
   the request token, and writes the access token to its state folder. If you set `KITE_MCP_ALLOWED_USER_ID`, a login
   from any other account is refused.
4. Check it: `kite-mcp-ctl status`, or ask the assistant for `session_status`. It shows when the token expires (about
   06:00 IST the next morning).

Everything connected to the server uses that token until it expires, and the server picks it up again after a restart.
If the link no longer works, ask for a new one (a link is valid for `KITE_MCP_LOGIN_LINK_TTL_S`, an hour by default, and
never past the next 06:00 IST).

Do not log in with the same API key from anywhere else: a new session invalidates the old token. See
[Why one program owns the login](KITE_SETUP.md#why-one-program-owns-the-login).

### Optional: an evening logout

In `managed` mode you can end the Kite session every evening, so that a token does not sit valid overnight. The
repository includes a oneshot unit and a timer ([`deploy/systemd/`](../deploy/systemd/kite-connect-mcp-logout.timer)):

```bash
sudo cp kite-connect-mcp/deploy/systemd/kite-connect-mcp-logout.service \
        kite-connect-mcp/deploy/systemd/kite-connect-mcp-logout.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now kite-connect-mcp-logout.timer
systemctl list-timers kite-connect-mcp-logout.timer
```

It runs `kite-connect-mcp logout --yes` at 22:30 IST (edit `OnCalendar=` to change the time, and pick a time after your
last session of the day). Logging out ends the session at Kite, so it also stops any other program that shared this
token, and every tool returns "not logged in" until you log in again. Do not install it in `external` mode: there the
command refuses, because the token is not this server's to end. To test it, run
`sudo systemctl start kite-connect-mcp-logout` and read `journalctl -u kite-connect-mcp-logout`.

## Sharing a token with another program (external mode)

If another program on the machine already performs your daily login, do not let two programs log in with one API key:
each login kills the other's token. Instead let that program own the login and make this server follow its token.

```bash
KITE_MCP_AUTH_MODE=external
KITE_TOKEN_FILE=/var/lib/other-program/kite_token.json
```

- **File format.** A JSON object `{"access_token": "...", "login_date": "2026-11-04", "logged_in_at": "2026-11-04T08:30:00+05:30", "expires_at": "2026-11-05T06:00:00+05:30"}`,
  or a file holding only the bare token. For a bare token, or a JSON file without `expires_at`, the expiry is the first
  06:00 IST after the file's time. Times should carry a UTC offset; one without is read as IST.
- **It is re-read whenever it changes**, so the new token every morning is picked up without a restart.
- **It is never written, deleted or logged out.** In this mode the server cannot make a session call: the `login` and
  `logout` tools and commands refuse, and nothing in the process can call Kite's token endpoints. If Kite rejects the
  token, the server stops using it until the file holds a different one.
- The redirect URL, the callback and the logout belong to the other program. You do not need `/callback` on this server.
- The service user must be able to read the file. Under the systemd unit, `ProtectHome=true` hides `/home`, so keep the
  token outside it (for example under `/var/lib` or `/etc`). Mode 600 is best; a more open mode earns a log warning.
- `kite-mcp-ctl status` shows the token's source and expiry.

## Backups

| What | Back it up? |
|---|---|
| `/var/lib/kite-connect-mcp/audit/` (Docker: `state/audit/` in the volume) | **Yes.** It is your record of every order attempt, and the daily caps are computed from today's file. |
| `/var/lib/kite-connect-mcp/access_token.json` | No. It expires daily. If you must, encrypt the backup: it is a live credential until 06:00 IST. |
| `/var/lib/kite-connect-mcp/KILL_SWITCH` | No. It is a flag; recreate it if you need it. |
| `/var/cache/kite-connect-mcp/` | No. It is rebuilt. |
| `/etc/kite-connect-mcp/` | Keep a copy of the settings file somewhere private (a password manager is fine). Never put the secrets in a repository, a ticket or a chat. You can generate a new bearer token at any time. |

```bash
sudo tar --exclude=access_token.json -czf kite-connect-mcp-state.tgz -C /var/lib kite-connect-mcp
```

The server never deletes audit files. Archive old days yourself, but leave today's file in place: deleting it resets
the day's caps.

## Upgrades

Read [CHANGELOG.md](../CHANGELOG.md) first. The token, the audit log and the daily caps survive a restart; pending
confirmation tokens do not (run the dry run again).

```bash
# systemd
sudo /opt/kite-connect-mcp/venv/bin/pip install --upgrade git+https://github.com/techfreakworm/kite-connect-mcp
/opt/kite-connect-mcp/venv/bin/kite-connect-mcp --version
sudo systemctl restart kite-connect-mcp
kite-mcp-ctl check-config

# Docker, from a fresh clone or `git pull` of the repository
docker compose up -d --build
```

Add `@<tag-or-commit>` to the Git URL to pin a version. The image installs the newest compatible releases of its
dependencies at build time; there is no lock file yet.

## Logs

- **systemd:** `journalctl -u kite-connect-mcp -f`, or `-n 100 --no-pager`, or `--since today`.
- **Docker:** `docker compose logs -f kite-connect-mcp`.
- **Local stdio:** the client captures stderr; Claude Desktop keeps `mcp*.log` files in its logs folder.

The log shows start-up, the listening address, and errors, at the level set by `KITE_MCP_LOG_LEVEL` (default `INFO`).
There is no HTTP access log (the callback URL carries one-time values), and credentials are never logged. Orders are
recorded in the [audit log](SAFETY.md#12-the-audit-log), not here.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `kite-connect-mcp: invalid configuration:` and a list | A setting is wrong. Fix each line shown; [CONFIGURATION.md](CONFIGURATION.md#validation-rules) explains the rules. |
| Exits at start: "HTTP transport needs KITE_MCP_BEARER_TOKEN" | Set `KITE_MCP_BEARER_TOKEN` or `KITE_MCP_BEARER_TOKEN_FILE`. The Docker image always needs one. |
| "readable by group/others (mode 644); run chmod 600" | A `*_FILE` secret has loose permissions. `chmod 600` it (and in Docker, `chown 10001`). |
| "cannot read ...: Permission denied" for a secret file | The service user cannot read it. Own it as `kite-mcp` (or uid 10001 in Docker) with mode 600. |
| A client gets 401 from `/mcp` | Wrong or missing bearer token, or the proxy dropped the `Authorization` header. |
| 421 "Invalid Host header" | The `Host` the server sees is not allowed. Set `KITE_MCP_PUBLIC_URL=https://...`, add `KITE_MCP_ALLOWED_HOSTS`, and make the proxy pass the original `Host` (nginx needs `proxy_set_header Host $host;`). |
| 403 "Invalid Origin header" | A browser-based client from an origin that is not the public URL. |
| 404 at `/callback` after logging in | The proxy does not forward `/callback`, or the registered redirect URL does not match `KITE_MCP_PUBLIC_URL` plus `KITE_MCP_CALLBACK_PATH`. |
| 502 or 503 from the proxy | The server is not running or not on `127.0.0.1:8765`. Check `systemctl status` or `docker compose ps`, and `curl http://127.0.0.1:8765/health` on the machine. |
| systemd: `status=226/NAMESPACE` | A folder in `ReadWritePaths=` does not exist. Create the folders from step 2. |
| systemd: "Failed to load environment files" | `/etc/kite-connect-mcp/kite-connect-mcp.env` is missing. |
| Docker: `PermissionError` on `/var/lib/kite-connect-mcp/state` | The mounted host folder does not belong to uid 10001. |
| Every tool says "not logged in" or the token expired | Normal each morning after 06:00 IST. Log in again. |
| `PermissionException` on quotes or historical data | The app is on the Personal plan. See [KITE_SETUP.md](KITE_SETUP.md#11-common-errors). |
| A live order is refused with an IP message | The static IP. See [KITE_SETUP.md](KITE_SETUP.md#static-ip-for-live-orders). |
| `status --verify` says the token was rejected | A session was replaced elsewhere. Log in again, and find what else uses your API key. |

First-run checklist for a server: `kite-mcp-ctl check-config`; `systemctl status kite-connect-mcp`; `curl` the three
URLs in step 5; log in; `kite-mcp-ctl status --verify`; connect one client and call `session_status`; dry-run one small
order; read `kite-mcp-ctl audit`.
