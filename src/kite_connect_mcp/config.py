"""Settings, read from environment variables (and optional `*_FILE` indirections for secrets).

Everything has a safe default: dry-run trading, a ₹10,000 per-order cap, loopback-only HTTP. Credentials are never in
the repr of a Settings object, never logged, and never echoed in an error.

See `.env.example` for the full list with explanations.
"""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal

from .errors import ConfigError

AuthMode = Literal["managed", "external"]
TradingMode = Literal["dry_run", "live"]
LiveConfirmation = Literal["token", "allowlist"]
Transport = Literal["stdio", "http"]

TOOLSETS = (
    "session",
    "account",
    "orders",
    "gtt",
    "alerts",
    "portfolio",
    "market",
    "margins",
    "mf",
    "ticker",
    "safety",
)

DEFAULT_API_BASE_URL = "https://api.kite.trade"
DEFAULT_LOGIN_URL = "https://kite.zerodha.com/connect/login"
DEFAULT_TICKER_URL = "wss://ws.kite.trade"
DEFAULT_PORT = 8765

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}


def _bool(env: Mapping[str, str], name: str, default: bool, problems: list[str]) -> bool:
    raw = env.get(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    problems.append(f"{name} must be true or false, not {raw!r}")
    return default


def _int(env: Mapping[str, str], name: str, default: int, problems: list[str], *, minimum: int = 0) -> int:
    raw = env.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        problems.append(f"{name} must be a whole number, not {raw!r}")
        return default
    if value < minimum:
        problems.append(f"{name} must be at least {minimum}")
        return default
    return value


def _decimal(env: Mapping[str, str], name: str, default: Decimal, problems: list[str]) -> Decimal:
    raw = env.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = Decimal(raw.strip().replace(",", "").replace("_", ""))
    except InvalidOperation:
        problems.append(f"{name} must be a number (INR), not {raw!r}")
        return default
    if not value.is_finite() or value < 0:
        problems.append(f"{name} must be a non-negative number")
        return default
    return value


def _choice(env: Mapping[str, str], name: str, default: str, choices: tuple[str, ...], problems: list[str]) -> str:
    raw = env.get(name)
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower().replace("-", "_")
    if value not in choices:
        problems.append(f"{name} must be one of {', '.join(choices)}, not {raw!r}")
        return default
    return value


def _list(env: Mapping[str, str], name: str) -> tuple[str, ...]:
    raw = env.get(name, "")
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def _upper_set(env: Mapping[str, str], name: str) -> frozenset[str] | None:
    """A comma-separated allow-list. Unset, empty or '*' means "anything" (None)."""
    items = _list(env, name)
    if not items or "*" in items:
        return None
    return frozenset(item.upper() for item in items)


def _read_secret_file(path: str, name: str, problems: list[str]) -> str | None:
    file = Path(path).expanduser()
    try:
        mode = stat.S_IMODE(file.stat().st_mode)
        value = file.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        problems.append(f"{name}: file not found: {file}")
        return None
    except OSError as exc:
        problems.append(f"{name}: cannot read {file}: {exc.strerror}")
        return None
    if mode & 0o077:
        problems.append(f"{name}: {file} is readable by group/others (mode {mode:o}); run chmod 600")
        return None
    if not value or re.search(r"\s", value):
        problems.append(f"{name}: {file} is empty or holds more than one value")
        return None
    return value


def _secret(env: Mapping[str, str], name: str, problems: list[str]) -> str | None:
    """`NAME` directly, or the contents of the file named by `NAME_FILE` (mode 600). Setting both is an error."""
    direct = env.get(name, "").strip() or None
    file = env.get(f"{name}_FILE", "").strip() or None
    if direct and file:
        problems.append(f"set {name} or {name}_FILE, not both")
        return None
    if file:
        return _read_secret_file(file, f"{name}_FILE", problems)
    if direct and re.search(r"\s", direct):
        problems.append(f"{name} contains whitespace")
        return None
    return direct


def _default_dir(env: Mapping[str, str], xdg: str, fallback: str) -> Path:
    base = env.get(xdg, "").strip()
    root = Path(base).expanduser() if base else Path.home() / fallback
    return root / "kite-connect-mcp"


@dataclass(frozen=True)
class Settings:
    # --- Kite credentials (repr=False keeps them out of logs and tracebacks) ---
    api_key: str | None = field(default=None, repr=False)
    api_secret: str | None = field(default=None, repr=False)
    access_token: str | None = field(default=None, repr=False)  # a static token from the environment (optional)

    # --- auth / token store ---
    auth_mode: AuthMode = "managed"
    token_file: Path = Path("access_token.json")
    allowed_user_id: str | None = field(default=None, repr=False)  # refuse logins from any other Kite account
    login_link_ttl_s: int = 3600

    # --- endpoints ---
    api_base_url: str = DEFAULT_API_BASE_URL
    login_url: str = DEFAULT_LOGIN_URL
    ticker_url: str = DEFAULT_TICKER_URL
    http_timeout_s: float = 30.0

    # --- local state ---
    state_dir: Path = Path(".")
    cache_dir: Path = Path(".")

    # --- transport ---
    transport: Transport = "stdio"
    host: str = "127.0.0.1"
    port: int = DEFAULT_PORT
    public_url: str | None = None  # e.g. https://kite.example.com (used for the login redirect and Host checks)
    bearer_token: str | None = field(default=None, repr=False)
    allow_unauthenticated_http: bool = False
    allowed_hosts: tuple[str, ...] = ()
    callback_path: str = "/callback"
    postback_path: str | None = None  # e.g. /postback; None disables the postback endpoint

    # --- safety ---
    read_only: bool = False
    trading_mode: TradingMode = "dry_run"
    live_confirmation: LiveConfirmation = "token"
    confirmation_ttl_s: int = 300
    max_order_value: Decimal = Decimal(10_000)
    max_daily_value: Decimal = Decimal(50_000)
    max_orders_per_day: int = 20
    max_quantity: int | None = None
    allowed_instruments: frozenset[str] | None = None  # "EXCHANGE:TRADINGSYMBOL", upper case; None = any
    allowed_exchanges: frozenset[str] | None = None
    allowed_products: frozenset[str] | None = None
    allowed_varieties: frozenset[str] | None = None
    allowed_order_types: frozenset[str] | None = None
    allowed_transaction_types: frozenset[str] | None = None
    allow_mf_writes: bool = False
    kill_switch_file: Path = Path("KILL_SWITCH")

    # --- tools ---
    toolsets: frozenset[str] = frozenset(TOOLSETS)
    excluded_tools: frozenset[str] = frozenset()

    # --- ticker ---
    ticker_enabled: bool = True
    ticker_max_subscriptions: int = 1000

    log_level: str = "INFO"

    @property
    def live_trading(self) -> bool:
        return self.trading_mode == "live" and not self.read_only

    @property
    def audit_dir(self) -> Path:
        return self.state_dir / "audit"

    def redacted(self) -> dict[str, object]:
        """Every setting, with credentials replaced by whether they are set. Safe to log or return from a tool."""
        out: dict[str, object] = {}
        for f in fields(self):
            value = getattr(self, f.name)
            if not f.repr:
                out[f.name] = "set" if value else "unset"
            elif isinstance(value, frozenset):
                out[f.name] = sorted(value)
            elif isinstance(value, Path | Decimal):
                out[f.name] = str(value)
            else:
                out[f.name] = value
        return out

    def secrets(self) -> tuple[str, ...]:
        """The credential values held, for scrubbing error messages."""
        return tuple(v for v in (self.api_key, self.api_secret, self.access_token, self.bearer_token) if v)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        """Build the settings from `env` (default: os.environ). Raises ConfigError listing every problem found."""
        env = dict(os.environ if env is None else env)
        problems: list[str] = []

        state_dir = Path(env["KITE_MCP_STATE_DIR"]).expanduser() if env.get("KITE_MCP_STATE_DIR") else None
        state_dir = state_dir or _default_dir(env, "XDG_STATE_HOME", ".local/state")
        cache_dir = Path(env["KITE_MCP_CACHE_DIR"]).expanduser() if env.get("KITE_MCP_CACHE_DIR") else None
        cache_dir = cache_dir or _default_dir(env, "XDG_CACHE_HOME", ".cache")

        token_file_raw = env.get("KITE_TOKEN_FILE", "").strip()
        token_file = Path(token_file_raw).expanduser() if token_file_raw else state_dir / "access_token.json"

        kill_raw = env.get("KITE_MCP_KILL_SWITCH_FILE", "").strip()
        kill_switch = Path(kill_raw).expanduser() if kill_raw else state_dir / "KILL_SWITCH"

        toolsets_raw = _list(env, "KITE_MCP_TOOLSETS")
        toolsets = frozenset(t.lower() for t in toolsets_raw) if toolsets_raw else frozenset(TOOLSETS)
        if "all" in toolsets:
            toolsets = frozenset(TOOLSETS)
        unknown = sorted(toolsets - set(TOOLSETS))
        if unknown:
            problems.append(f"KITE_MCP_TOOLSETS has unknown toolsets {unknown}; known: {', '.join(TOOLSETS)}")

        max_qty_raw = env.get("KITE_MCP_MAX_QUANTITY", "").strip()
        max_quantity = _int(env, "KITE_MCP_MAX_QUANTITY", 0, problems, minimum=1) if max_qty_raw else None

        callback_path = env.get("KITE_MCP_CALLBACK_PATH", "/callback").strip() or "/callback"
        postback_path = env.get("KITE_MCP_POSTBACK_PATH", "").strip() or None
        for name, path in (("KITE_MCP_CALLBACK_PATH", callback_path), ("KITE_MCP_POSTBACK_PATH", postback_path)):
            if path is not None and (not path.startswith("/") or path.startswith("/mcp")):
                problems.append(f"{name} must start with / and must not be under /mcp")

        settings = cls(
            api_key=_secret(env, "KITE_API_KEY", problems),
            api_secret=_secret(env, "KITE_API_SECRET", problems),
            access_token=_secret(env, "KITE_ACCESS_TOKEN", problems),
            auth_mode=_choice(env, "KITE_MCP_AUTH_MODE", "managed", ("managed", "external"), problems),  # type: ignore[arg-type]
            token_file=token_file,
            allowed_user_id=(env.get("KITE_MCP_ALLOWED_USER_ID", "").strip().upper() or None),
            login_link_ttl_s=_int(env, "KITE_MCP_LOGIN_LINK_TTL_S", 3600, problems, minimum=60),
            api_base_url=env.get("KITE_API_BASE_URL", DEFAULT_API_BASE_URL).strip().rstrip("/") or DEFAULT_API_BASE_URL,
            login_url=env.get("KITE_LOGIN_URL", DEFAULT_LOGIN_URL).strip() or DEFAULT_LOGIN_URL,
            ticker_url=env.get("KITE_TICKER_URL", DEFAULT_TICKER_URL).strip() or DEFAULT_TICKER_URL,
            http_timeout_s=float(_int(env, "KITE_MCP_HTTP_TIMEOUT_S", 30, problems, minimum=1)),
            state_dir=state_dir,
            cache_dir=cache_dir,
            transport=_choice(env, "KITE_MCP_TRANSPORT", "stdio", ("stdio", "http"), problems),  # type: ignore[arg-type]
            host=env.get("KITE_MCP_HOST", "127.0.0.1").strip() or "127.0.0.1",
            port=_int(env, "KITE_MCP_PORT", DEFAULT_PORT, problems, minimum=1),
            public_url=(env.get("KITE_MCP_PUBLIC_URL", "").strip().rstrip("/") or None),
            bearer_token=_secret(env, "KITE_MCP_BEARER_TOKEN", problems),
            allow_unauthenticated_http=_bool(env, "KITE_MCP_ALLOW_UNAUTHENTICATED_HTTP", False, problems),
            allowed_hosts=_list(env, "KITE_MCP_ALLOWED_HOSTS"),
            callback_path=callback_path,
            postback_path=postback_path,
            read_only=_bool(env, "KITE_MCP_READ_ONLY", False, problems),
            trading_mode=_choice(env, "KITE_MCP_TRADING_MODE", "dry_run", ("dry_run", "live"), problems),  # type: ignore[arg-type]
            live_confirmation=_choice(  # type: ignore[arg-type]
                env, "KITE_MCP_LIVE_CONFIRMATION", "token", ("token", "allowlist"), problems
            ),
            confirmation_ttl_s=_int(env, "KITE_MCP_CONFIRMATION_TTL_S", 300, problems, minimum=10),
            max_order_value=_decimal(env, "KITE_MCP_MAX_ORDER_VALUE", Decimal(10_000), problems),
            max_daily_value=_decimal(env, "KITE_MCP_MAX_DAILY_VALUE", Decimal(50_000), problems),
            max_orders_per_day=_int(env, "KITE_MCP_MAX_ORDERS_PER_DAY", 20, problems),
            max_quantity=max_quantity,
            allowed_instruments=_upper_set(env, "KITE_MCP_ALLOWED_INSTRUMENTS"),
            allowed_exchanges=_upper_set(env, "KITE_MCP_ALLOWED_EXCHANGES"),
            allowed_products=_upper_set(env, "KITE_MCP_ALLOWED_PRODUCTS"),
            allowed_varieties=(
                frozenset(v.lower() for v in s) if (s := _upper_set(env, "KITE_MCP_ALLOWED_VARIETIES")) else None
            ),
            allowed_order_types=_upper_set(env, "KITE_MCP_ALLOWED_ORDER_TYPES"),
            allowed_transaction_types=_upper_set(env, "KITE_MCP_ALLOWED_TRANSACTION_TYPES"),
            allow_mf_writes=_bool(env, "KITE_MCP_ALLOW_MF_WRITES", False, problems),
            kill_switch_file=kill_switch,
            toolsets=toolsets,
            excluded_tools=frozenset(t.lower() for t in _list(env, "KITE_MCP_EXCLUDED_TOOLS")),
            ticker_enabled=_bool(env, "KITE_MCP_TICKER_ENABLED", True, problems),
            ticker_max_subscriptions=_int(env, "KITE_MCP_TICKER_MAX_SUBSCRIPTIONS", 1000, problems, minimum=1),
            log_level=(env.get("KITE_MCP_LOG_LEVEL", "INFO").strip().upper() or "INFO"),
        )
        problems.extend(settings._consistency_problems())
        if problems:
            raise ConfigError("invalid configuration:\n  - " + "\n  - ".join(problems))
        return settings

    def _consistency_problems(self) -> list[str]:
        problems: list[str] = []
        if self.ticker_max_subscriptions > 3000:
            problems.append("KITE_MCP_TICKER_MAX_SUBSCRIPTIONS cannot exceed 3000 (Kite's per-connection limit)")
        if self.live_confirmation == "allowlist" and self.allowed_instruments is None:
            problems.append(
                "KITE_MCP_LIVE_CONFIRMATION=allowlist needs an explicit KITE_MCP_ALLOWED_INSTRUMENTS list (not '*')"
            )
        if self.transport == "http" and not self.bearer_token and not self.allow_unauthenticated_http:
            problems.append(
                "HTTP transport needs KITE_MCP_BEARER_TOKEN (or KITE_MCP_BEARER_TOKEN_FILE); set "
                "KITE_MCP_ALLOW_UNAUTHENTICATED_HTTP=true only for a loopback-only test setup"
            )
        if self.allow_unauthenticated_http and self.host not in ("127.0.0.1", "localhost", "::1"):
            problems.append("KITE_MCP_ALLOW_UNAUTHENTICATED_HTTP=true is only allowed with a loopback KITE_MCP_HOST")
        if self.bearer_token is not None and len(self.bearer_token) < 32:
            problems.append("KITE_MCP_BEARER_TOKEN must be at least 32 characters (try: openssl rand -hex 32)")
        if self.log_level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            problems.append("KITE_MCP_LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR or CRITICAL")
        return problems

    def require_api_key(self) -> str:
        if not self.api_key:
            raise ConfigError("KITE_API_KEY (or KITE_API_KEY_FILE) is not set")
        return self.api_key

    def require_api_secret(self) -> str:
        if not self.api_secret:
            raise ConfigError("KITE_API_SECRET (or KITE_API_SECRET_FILE) is not set; it is needed to log in")
        return self.api_secret


def load_env_file(path: Path, env: dict[str, str] | None = None, *, override: bool = False) -> dict[str, str]:
    """Merge a simple KEY=VALUE file (comments with #, optional quotes, optional `export `) into `env`.

    The file should be mode 600 if it holds credentials; a warning-free load is the caller's business. Values already in
    `env` win unless `override` is set, so the real environment beats the file."""
    target = dict(os.environ) if env is None else env
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read env file {path}: {exc.strerror}") from None
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[len("export ") :].lstrip()
        key, sep, value = stripped.partition("=")
        key = key.strip()
        if not sep or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ConfigError(f"{path}:{number}: expected KEY=VALUE")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        if override or key not in target:
            target[key] = value
    return target
