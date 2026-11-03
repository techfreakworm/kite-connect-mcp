"""Command line: `kite-connect-mcp [serve|status|tools|check-config|login|logout|audit] ...`.

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
from typing import Any

from . import __version__
from .config import Settings, load_env_file, unknown_variables
from .errors import ConfigError, KiteMCPError

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
    )
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    settings = _settings(args)
    from .server import build_runtime

    rt = build_runtime(settings)
    out: dict[str, Any] = {
        "version": __version__,
        "auth_mode": settings.auth_mode,
        "token_file": str(settings.token_file),
        "session": rt.tokens.status(),
        "safety": rt.pipeline.limits(),
        "today": rt.audit.usage().as_dict(),
    }
    if args.verify:

        async def verify() -> dict[str, Any]:
            try:
                profile = await rt.client.get("/user/profile")
                return {"verified": True, "user_id": (profile or {}).get("user_id")}
            except KiteMCPError as exc:
                return {"verified": False, "error": rt.client.scrub(str(exc))}
            finally:
                await rt.client.aclose()

        out["live_check"] = asyncio.run(verify())
    print(json.dumps(out, indent=2, default=str))
    return 0


def cmd_tools(args: argparse.Namespace) -> int:
    settings = _settings(args)
    from .server import build_runtime, build_server

    rt = build_runtime(settings)
    server, reg = build_server(rt)
    if args.markdown:
        print(tools_markdown(server, reg))
    else:
        for info in reg.registered:
            print(f"{info.name:28} {info.toolset:10} {info.kind:6} {info.title}")
        for name, why in sorted(reg.skipped.items()):
            print(f"{name:28} (hidden: {why})")
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
        "Generated by `KITE_MCP_ALLOW_MF_WRITES=true kite-connect-mcp tools --markdown`: every toolset, with the",
        "write tools visible.",
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


def cmd_login(args: argparse.Namespace) -> int:
    settings = _settings(args)
    _setup_logging(settings.log_level)
    from .auth.login import cli_login

    return asyncio.run(cli_login(settings, request_token=args.request_token, listen=args.listen))


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
        "Open it and log in at Kite; Kite then redirects to the redirect URL registered for your app.",
    )
    login.add_argument(
        "--request-token",
        help="finish the login with the request_token from the redirect URL's address bar (the whole URL also works; "
        "put it in quotes)",
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
