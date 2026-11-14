"""Command line: `kite-connect-mcp [serve|status|tools|check-config|login|logout|audit|events] ...`.

Logs go to stderr only: with the stdio transport, stdout carries the MCP protocol and nothing else.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import logging
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import __version__
from .config import Settings, load_env_file, unknown_variables
from .errors import ConfigError, KiteMCPError

if TYPE_CHECKING:
    from .events.store import EventStore

log = logging.getLogger("kite_connect_mcp")


def _settings(args: argparse.Namespace, **overrides: str | None) -> Settings:
    env = dict(os.environ)
    if getattr(args, "env_file", None):
        env = load_env_file(Path(args.env_file), env)
        for key, text in env.items():  # e.g. HTTPS_PROXY from the file must reach the HTTP client too
            os.environ.setdefault(key, text)
    for key, override in overrides.items():
        if override is not None:
            env[key] = str(override)
    for name in unknown_variables(env):
        print(f"kite-connect-mcp: warning: {name} is not a setting this server reads (a typo?)", file=sys.stderr)
    return Settings.from_env(env)


def _setup_logging(level: str) -> None:
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stderr)
    for noisy in ("mcp", "httpx2", "httpcore2", "uvicorn.access", "websockets"):
        logging.getLogger(noisy).setLevel(max(logging.WARNING, logging.getLevelName(level)))


def cmd_serve(args: argparse.Namespace) -> int:
    settings = _settings(
        args,
        KITE_MCP_TRANSPORT=args.transport,
        KITE_MCP_HOST=args.host,
        KITE_MCP_PORT=str(args.port) if args.port else None,
    )
    _setup_logging(settings.log_level)
    from .auth.reminders import serving_problem

    if (problem := serving_problem(settings)) is not None:  # before anything is built: stdio has no lifespan
        raise ConfigError(problem)
    from .server import build_http_app, build_runtime, build_server

    rt = build_runtime(settings)
    server, reg = build_server(rt)
    log.info(
        "kite-connect-mcp %s: %d tools, auth=%s, trading=%s%s, transport=%s",
        __version__,
        len(reg.registered),
        settings.auth_mode,
        settings.trading_mode,
        " (read-only)" if settings.read_only else "",
        settings.transport,
    )
    if settings.transport == "stdio":
        import anyio

        from .server import run_stdio

        anyio.run(run_stdio, server, rt)
        return 0

    import uvicorn

    app = build_http_app(server, rt)
    log.info("listening on http://%s:%d/mcp", settings.host, settings.port)
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        log_level="warning",
        access_log=False,  # the login callback's query string holds one-time secrets
        server_header=False,
        date_header=False,
        proxy_headers=True,
        forwarded_allow_ips="127.0.0.1",
        timeout_graceful_shutdown=10,
        lifespan="on",  # a startup that fails in any way (the event service) exits instead of serving without it
    )
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    settings = _settings(args)
    from .auth.account import AccountPin
    from .server import build_runtime

    rt = build_runtime(settings, with_events=False)  # a status read never touches the serving process's store

    async def collect() -> dict[str, Any]:
        try:
            out: dict[str, Any] = {
                "version": __version__,
                "auth_mode": settings.auth_mode,
                "token_file": str(settings.token_file),
                "session": rt.tokens.status(),
                "safety": rt.pipeline.limits(),
                "today": rt.audit.usage().as_dict(),
            }
            if rt.links is not None:  # KITE_MCP_LOGIN_LINK_COMMAND: the times, the next one, the last attempt
                out["login_links"] = rt.links.status()
            if args.verify:
                try:
                    profile = await rt.client.get("/user/profile")
                    user_id = profile.get("user_id") if isinstance(profile, dict) else None
                    out["live_check"] = {"verified": True, **AccountPin.from_settings(settings).label(user_id)}
                except KiteMCPError as exc:
                    out["live_check"] = {"verified": False, "error": rt.client.scrub(str(exc))}
            return out
        finally:
            await rt.aclose()  # in the loop the client ran in

    print(json.dumps(asyncio.run(collect()), indent=2, default=str))
    return 0


def cmd_tools(args: argparse.Namespace) -> int:
    settings = _settings(args)
    from .server import build_runtime, build_server

    rt = build_runtime(settings, with_events=False)  # the events tools read an existing store read-only
    try:
        server, reg = build_server(rt)
        if args.markdown:
            print(tools_markdown(server, reg))
        else:
            for info in reg.registered:
                print(f"{info.name:28} {info.toolset:10} {info.kind:6} {info.title}")
            for name, why in sorted(reg.skipped.items()):
                print(f"{name:28} (hidden: {why})")
    finally:
        asyncio.run(rt.aclose())  # the client was never used; this closes what the tool modules opened
    return 0


def _schema_type(spec: dict[str, Any]) -> str:
    """A short type for a JSON-schema property: string, integer, "BUY" | "SELL", array of object, ..."""
    if "enum" in spec:
        return " | ".join(f"`{v}`" for v in spec["enum"])
    if "anyOf" in spec:
        parts = [_schema_type(p) for p in spec["anyOf"] if p.get("type") != "null"]
        return " | ".join(parts) + (" (optional)" if any(p.get("type") == "null" for p in spec["anyOf"]) else "")
    if spec.get("type") == "array":
        items = spec.get("items", {})
        inner = items.get("$ref", "").rsplit("/", 1)[-1] or _schema_type(items)
        return f"array of {inner}"
    if "$ref" in spec:
        return spec["$ref"].rsplit("/", 1)[-1]
    return str(spec.get("type", "any"))


def tools_markdown(server: Any, reg: Any) -> str:
    """The tool reference: every registered tool with its description and a table of its arguments."""
    tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
    lines = [
        "# Tool reference",
        "",
        "Generated by `KITE_MCP_ALLOW_MF_WRITES=true KITE_MCP_EVENTS_DB=<a store> kite-connect-mcp tools --markdown`:",
        "every toolset, with the write tools visible, and the `events` tools, which need an event store to read.",
        "Write tools are dry runs by default; see [SAFETY.md](SAFETY.md).",
        "",
    ]
    by_set: dict[str, list[Any]] = {}
    for info in reg.registered:
        by_set.setdefault(info.toolset, []).append(info)
    lines += ["| Toolset | Tools |", "|---|---|"]
    lines += [f"| {name} | {', '.join(f'`{i.name}`' for i in infos)} |" for name, infos in by_set.items()]
    lines.append("")
    for toolset, infos in by_set.items():
        lines += [f"## {toolset}", ""]
        for info in infos:
            schema = getattr(tools.get(info.name), "input_schema", None) or {}
            props: dict[str, Any] = schema.get("properties", {})
            required = set(schema.get("required", []))
            lines += [f"### `{info.name}`: {info.title}", "", f"*{info.kind}*", "", info.description, ""]
            if props:
                lines += ["| Argument | Type | Required | Default | Description |", "|---|---|---|---|---|"]
                for name, spec in props.items():
                    default = f"`{json.dumps(spec['default'])}`" if "default" in spec else ""
                    text = " ".join(str(spec.get("description", "")).split()).replace("|", "\\|")
                    kind = _schema_type(spec).replace("|", "\\|")
                    lines.append(f"| `{name}` | {kind} | {'yes' if name in required else ''} | {default} | {text} |")
                lines.append("")
    return "\n".join(lines)


def cmd_check_config(args: argparse.Namespace) -> int:
    settings = _settings(args)
    print(json.dumps(settings.redacted(), indent=2, default=str))
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    settings = _settings(args)
    from .safety.audit import AuditLog

    try:
        day = dt.date.fromisoformat(args.date) if args.date else None
    except ValueError:
        raise ConfigError(f"--date must be YYYY-MM-DD, not {args.date!r}") from None
    for event in AuditLog(settings.audit_dir).read(day, limit=args.limit):
        print(json.dumps(event, default=str))
    return 0


def _open_store(settings: Settings, *, readonly: bool) -> EventStore:
    """The event store by its path, never through build_runtime: these commands work while the service runs. A store
    that is not there is never made, and one that cannot be opened (missing, out of reach, another schema version) is
    a ConfigError with EventStore's reason."""
    from .events.store import EventStore, StoreError

    path = settings.events_db
    if path is None:
        raise ConfigError("no event store: set KITE_MCP_EVENTS=true or KITE_MCP_EVENTS_DB")
    try:
        return EventStore(path, readonly=readonly, create=False)
    except StoreError as exc:
        raise ConfigError(str(exc)) from None


def _configured_consumers(settings: Settings) -> list[str] | None:
    """The names in the consumers file, or None when it cannot be read: that is said on stderr, and the commands go on
    with the names the store knows, so a consumer can still be paused while its file is broken."""
    from .events.consumers import ConsumersConfig

    try:
        return [c.name for c in ConsumersConfig.load(settings.events_consumers_file).consumers]
    except ConfigError as exc:
        print(f"kite-connect-mcp: warning: {exc}\n(going on with the consumers the event store knows)", file=sys.stderr)
        return None


def _check_consumer(settings: Settings, action: str, name: str) -> None:
    """Refuse, before the store is opened read-write, a name that is neither in the consumers file nor in the store (a
    typo), and `retry` for a consumer no longer in the file unless it is paused: no router would deliver what it
    queues. A paused one's deliveries wait for `resume`, by which time it may be configured again."""
    configured = _configured_consumers(settings)
    if configured is not None and name in configured:
        return
    store = _open_store(settings, readonly=True)
    try:
        known, paused = store.consumer_names(), store.is_paused(name)
    finally:
        store.close()
    if name not in known:
        everyone = ", ".join(sorted(known | set(configured or ()))) or "none"
        raise ConfigError(f"unknown consumer {name!r} (known: {everyone})")
    if action == "retry" and configured is not None and not paused:
        raise ConfigError(
            f"{name!r} is no longer in the consumers file, so no router would deliver what retry queues: add it back "
            "first, or pause it to queue its failed deliveries for a later resume"
        )


def _deliveries(count: int, what: str) -> str:
    return f"{count} {what} {'delivery' if count == 1 else 'deliveries'}"


def cmd_events(args: argparse.Namespace) -> int:
    settings = _settings(args)
    action = args.events_command
    if action == "selftest":
        from .events import selftest

        if not args.url and not settings.events:
            raise ConfigError(
                "the event service is off in this configuration (KITE_MCP_EVENTS), so a selftest of this server has "
                "nothing to test: turn it on, or give --url of a server that has it"
            )
        url = args.url or selftest.default_url(settings)
        payload = selftest.build_payload(settings.require_api_secret("sign the selftest"))
        status = asyncio.run(selftest.send(url, payload))
        print(f"selftest: POST {selftest.shown_url(url)} -> HTTP {status}{selftest.explain(status)}")
        return 0 if status == 200 else 1
    from .auth.reminders import describe_links
    from .events.service import describe_status
    from .timeutil import now_ist

    if action in ("status", "tail"):
        names = _configured_consumers(settings) if action == "status" else None
        store = _open_store(settings, readonly=True)
        try:
            if action == "status":
                now = now_ist()  # one moment for the whole status
                links = describe_links(settings, now) if settings.login_link_command is not None else None
                health = describe_status(store, settings, now, names, links=links)
                print(json.dumps(health, indent=2, default=str))
            else:
                for event in store.events(order_id=args.order, limit=args.limit):
                    print(json.dumps(event, ensure_ascii=False))
        finally:
            store.close()
        return 0
    name = args.consumer
    _check_consumer(settings, action, name)
    store = _open_store(settings, readonly=False)
    try:
        if action == "pause":
            store.pause(name, now_ist())
            print(f"paused {name}: events are still recorded; `resume` delivers them")
        elif action == "resume":
            skipped = store.resume(name, skip_backlog=args.skip_backlog, now=now_ist())
            print(f"resumed {name}" + (f"; {_deliveries(skipped, 'waiting')} skipped" if args.skip_backlog else ""))
        elif action == "retry":
            print(f"{_deliveries(store.retry_failed(name, now_ist()), 'failed')} of {name} queued again")
        return 0
    finally:
        store.close()


def _event_limit(text: str) -> int:
    """`events tail --limit`: 1 to MAX_EVENTS_LIMIT, as the store gives at most that many."""
    from .events.store import MAX_EVENTS_LIMIT

    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a whole number: {text!r}") from None
    if not 1 <= value <= MAX_EVENTS_LIMIT:
        raise argparse.ArgumentTypeError(f"must be between 1 and {MAX_EVENTS_LIMIT}, not {value}")
    return value


def cmd_login(args: argparse.Namespace) -> int:
    settings = _settings(args)
    _setup_logging(settings.log_level)
    from .auth.login import cli_login

    return asyncio.run(cli_login(settings, request_token=args.request_token, listen=args.listen, as_json=args.json))


def cmd_logout(args: argparse.Namespace) -> int:
    settings = _settings(args)
    _setup_logging(settings.log_level)
    from .auth.login import cli_logout

    return asyncio.run(cli_logout(settings, yes=args.yes))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kite-connect-mcp", description="MCP server for Zerodha Kite Connect")
    parser.add_argument("--version", action="version", version=f"kite-connect-mcp {__version__}")
    parser.add_argument("--env-file", help="read settings from this KEY=VALUE file (the environment wins)")
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="run the MCP server (default)")
    serve.add_argument("--transport", choices=["stdio", "http"], help="default: $KITE_MCP_TRANSPORT or stdio")
    serve.add_argument("--host", help="HTTP bind address (default 127.0.0.1)")
    serve.add_argument("--port", type=int, help="HTTP port (default 8765)")
    serve.set_defaults(func=cmd_serve)

    status = sub.add_parser("status", help="session and safety status (no secrets)")
    status.add_argument("--verify", action="store_true", help="also check the token with one read-only Kite call")
    status.set_defaults(func=cmd_status)

    tools = sub.add_parser("tools", help="list the tools this configuration exposes")
    tools.add_argument("--markdown", action="store_true", help="print a Markdown reference")
    tools.set_defaults(func=cmd_tools)

    check = sub.add_parser("check-config", help="validate the configuration and print it (credentials hidden)")
    check.set_defaults(func=cmd_check_config)

    audit = sub.add_parser("audit", help="print audit-log events")
    audit.add_argument("--date", help="IST date YYYY-MM-DD (default today)")
    audit.add_argument("--limit", type=int, default=50)
    audit.set_defaults(func=cmd_audit)

    login = sub.add_parser(
        "login",
        help="managed mode: print a login link, or exchange a request_token",
        description="Managed mode only. Prints a one-time Kite login link on stdout (everything else goes to stderr). "
        "Open it and log in at Kite; Kite then redirects to the redirect URL registered for your app. With "
        "KITE_MCP_LOGIN_ALLOW_FORCE=false and a valid stored token, it first asks Kite once (GET /user/profile) and "
        "exits 2 without a link while Kite still accepts that token.",
    )
    login_with = login.add_mutually_exclusive_group()
    login_with.add_argument(
        "--request-token",
        help="finish the login with the request_token from the redirect URL's address bar (the whole URL also works, "
        "and carries the login link's state; put it in quotes). With KITE_MCP_LOGIN_ALLOW_FORCE=false and a valid "
        "stored token, a request token without that state exits 2 without asking Kite",
    )
    login_with.add_argument(
        "--json",
        action="store_true",
        help='print the link and its expiry as one JSON line, {"url", "expires_at"}, instead of the bare link',
    )
    login.add_argument(
        "--listen",
        action="store_true",
        help="print the link, then wait up to 10 minutes on KITE_MCP_HOST:KITE_MCP_PORT for Kite's redirect. This only "
        "works if your Kite app's redirect URL is http://127.0.0.1:<port><callback path> (default "
        "http://127.0.0.1:8765/callback). Not needed when `serve --transport http` is already running.",
    )
    login.set_defaults(func=cmd_login)

    logout = sub.add_parser("logout", help="managed mode: end the Kite session and delete the token file")
    logout.add_argument("--yes", action="store_true", help="really log out")
    logout.set_defaults(func=cmd_logout)

    events = sub.add_parser("events", help="order events: status, recent events, pause/resume a consumer, selftest")
    events_sub = events.add_subparsers(dest="events_command", required=True)
    events_sub.add_parser("status", help="health of the postbacks, the poll and each consumer")
    tail = events_sub.add_parser("tail", help="print recent events as JSON lines")
    tail.add_argument(
        "--limit", type=_event_limit, default=20, help="how many of the newest (default 20, at most 1000)"
    )
    tail.add_argument("--order", help="only this order's events")
    for name, text in (
        ("pause", "hold a consumer's deliveries (events are still recorded)"),
        ("resume", "deliver a paused consumer's backlog"),
        ("retry", "queue a consumer's failed deliveries again"),
    ):
        p = events_sub.add_parser(name, help=text)
        p.add_argument("consumer")
        if name == "resume":
            p.add_argument("--skip-backlog", action="store_true", help="mark the waiting deliveries skipped instead")
    st = events_sub.add_parser("selftest", help="post a signed synthetic postback and check the answer")
    st.add_argument("--url", help="the postback URL (default: this server's own KITE_MCP_POSTBACK_PATH on loopback)")
    events.set_defaults(func=cmd_events)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    func = getattr(args, "func", None)
    if func is None:
        args = parser.parse_args([*(argv or sys.argv[1:]), "serve"])
        func = args.func
    try:
        return int(func(args) or 0)
    except ConfigError as exc:
        print(f"kite-connect-mcp: {exc}", file=sys.stderr)
        return 2
    except KiteMCPError as exc:
        print(f"kite-connect-mcp: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
