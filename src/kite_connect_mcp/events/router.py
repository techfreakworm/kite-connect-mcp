"""The router: which consumer gets which event, and (part 1B) running their commands.

The consumers file (KITE_MCP_EVENTS_CONSUMERS_FILE) is JSON:

    {"tag_labels": {"mytag": "my strategy"},
     "consumers": [{"name": "notify", "command": ["/usr/local/bin/notify-me"],
                    "match": {"kinds": ["order", "session", "router", "selftest"], "changes": ["filled"]},
                    "timeout_s": 30, "alert_after_s": 60,
                    "retry": {"delays_s": [10, 30, 60, 120, 300], "every_s": 300, "give_up_after_s": 1800},
                    "env_pass": ["NOTIFY_TOKEN"]}]}

A consumer gets order events unless `match.kinds` says otherwise; session, router and selftest events only reach the
consumers that list those kinds. The order filters (statuses, changes, tags, transaction_types, sources) apply to order
events only. The command is an argv list (no shell) whose program is an absolute path. `env_pass` names variables of
the service's own environment to pass on; KITE_* variables are never passed. The file defines programs the service runs,
so it must not be writable by group or others.
"""

from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..errors import ConfigError
from .model import CHANGES, KINDS, ORDER_SOURCES, STATUSES, TRANSACTION_TYPES

DEFAULT_RETRY_DELAYS = (10, 30, 60, 120, 300)
DEFAULT_RETRY_EVERY = 300
DEFAULT_GIVE_UP_AFTER = 1800
DEFAULT_TIMEOUT = 30.0
DEFAULT_ALERT_AFTER = 60
EXIT_RETRY = 75  # EX_TEMPFAIL: "try again later"

_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,39}")
_ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_TOP_KEYS = {"consumers", "tag_labels"}
_CONSUMER_KEYS = {"name", "command", "match", "timeout_s", "alert_after_s", "retry", "env_pass"}
_RETRY_KEYS = {"delays_s", "every_s", "give_up_after_s"}
_MATCH_CHOICES: dict[str, frozenset[str] | None] = {
    "kinds": frozenset(KINDS),
    "statuses": STATUSES,
    "changes": frozenset(CHANGES),
    "tags": None,  # any string
    "transaction_types": frozenset(TRANSACTION_TYPES),
    "sources": frozenset(ORDER_SOURCES),
}


@dataclass(frozen=True)
class Match:
    kinds: frozenset[str] = frozenset({"order"})
    statuses: frozenset[str] | None = None
    changes: frozenset[str] | None = None
    tags: frozenset[str] | None = None
    transaction_types: frozenset[str] | None = None
    sources: frozenset[str] | None = None

    def accepts(self, event: Mapping[str, Any]) -> bool:
        kind = event.get("kind")
        if kind not in self.kinds:
            return False
        if kind != "order":
            return True
        order = event.get("order") or {}
        checks = (
            (self.statuses, order.get("status")),
            (self.changes, event.get("change")),
            (self.tags, order.get("tag")),
            (self.transaction_types, order.get("transaction_type")),
            (self.sources, event.get("source")),
        )
        return all(allowed is None or value in allowed for allowed, value in checks)


@dataclass(frozen=True)
class Consumer:
    name: str
    command: tuple[str, ...]
    match: Match = Match()
    timeout_s: float = DEFAULT_TIMEOUT
    alert_after_s: int = DEFAULT_ALERT_AFTER
    retry_delays_s: tuple[int, ...] = DEFAULT_RETRY_DELAYS
    retry_every_s: int = DEFAULT_RETRY_EVERY
    give_up_after_s: int = DEFAULT_GIVE_UP_AFTER
    env_pass: tuple[str, ...] = ()

    def retry_delay(self, attempts: int) -> int:
        """Seconds to wait after the `attempts`-th failed try (1-based)."""
        if 1 <= attempts <= len(self.retry_delays_s):
            return self.retry_delays_s[attempts - 1]
        return self.retry_every_s


def _positive_int(value: Any, *, minimum: int) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        return None
    return value


@dataclass(frozen=True)
class ConsumersConfig:
    consumers: tuple[Consumer, ...] = ()
    tag_labels: Mapping[str, str] = field(default_factory=dict)

    def matching(self, event: Mapping[str, Any], *, exclude: str | None = None) -> list[str]:
        return [c.name for c in self.consumers if c.name != exclude and c.match.accepts(event)]

    def get(self, name: str) -> Consumer | None:
        return next((c for c in self.consumers if c.name == name), None)

    @classmethod
    def load(cls, path: Path | None) -> ConsumersConfig:
        if path is None:
            return cls()
        try:
            mode = stat.S_IMODE(path.stat().st_mode)
            raw = path.read_bytes()
        except OSError as exc:
            raise ConfigError(f"KITE_MCP_EVENTS_CONSUMERS_FILE: cannot read {path}: {exc.strerror}") from None
        if mode & 0o022:
            raise ConfigError(f"{path} is writable by group/others (mode {mode:o}); run chmod 600")
        try:
            data = json.loads(raw)  # bytes: text that is not UTF-8 is a ValueError here too
        except ValueError as exc:
            raise ConfigError(f"{path} is not valid JSON: {exc}") from None
        problems: list[str] = []
        config = cls._parse(data, problems)
        if problems:
            raise ConfigError(f"invalid consumers file {path}:\n  - " + "\n  - ".join(problems))
        return config

    @classmethod
    def _parse(cls, data: Any, problems: list[str]) -> ConsumersConfig:
        if not isinstance(data, dict):
            problems.append("the file must hold a JSON object")
            return cls()
        for key in sorted(set(data) - _TOP_KEYS):
            problems.append(f"unknown key {key!r} (known: consumers, tag_labels)")
        labels = data.get("tag_labels", {})
        if not isinstance(labels, dict) or not all(
            isinstance(k, str) and isinstance(v, str) and v.strip() and len(v) <= 40 and v.isprintable()
            for k, v in labels.items()
        ):
            problems.append("tag_labels must map each tag to a one-line label of at most 40 characters")
            labels = {}
        raw_consumers = data.get("consumers", [])
        if not isinstance(raw_consumers, list):
            problems.append("consumers must be a list")
            raw_consumers = []
        consumers: list[Consumer] = []
        names: set[str] = set()
        for index, raw in enumerate(raw_consumers):
            consumer = _parse_consumer(raw, index, problems)
            if consumer is None:
                continue
            if consumer.name in names:
                problems.append(f"consumer {consumer.name!r} is listed twice")
                continue
            names.add(consumer.name)
            consumers.append(consumer)
        return cls(tuple(consumers), dict(labels))


def _parse_match(raw: Any, where: str, problems: list[str]) -> Match:
    if raw is None:
        return Match()
    if not isinstance(raw, dict):
        problems.append(f"{where}: match must be an object")
        return Match()
    values: dict[str, frozenset[str] | None] = {}
    for key, items in raw.items():
        if key not in _MATCH_CHOICES:
            problems.append(f"{where}: unknown match key {key!r} (known: {', '.join(_MATCH_CHOICES)})")
            continue
        if not isinstance(items, list) or not all(isinstance(i, str) and i.strip() for i in items):
            problems.append(f"{where}: match.{key} must be a list of strings")
            continue
        if not items:
            problems.append(f"{where}: match.{key} must not be empty (leave it out to match anything)")
            continue
        wanted = frozenset(i.strip().upper() if key in ("statuses", "transaction_types") else i.strip() for i in items)
        allowed = _MATCH_CHOICES[key]
        if allowed is not None and not wanted <= allowed:
            problems.append(
                f"{where}: match.{key} has unknown values {sorted(wanted - allowed)}; known: {sorted(allowed)}"
            )
            continue
        values[key] = wanted
    kinds = values.pop("kinds", None) or frozenset({"order"})
    return Match(kinds=kinds, **values)


def _parse_consumer(raw: Any, index: int, problems: list[str]) -> Consumer | None:
    where = f"consumers[{index}]"
    if not isinstance(raw, dict):
        problems.append(f"{where} must be an object")
        return None
    for key in sorted(set(raw) - _CONSUMER_KEYS):
        problems.append(f"{where}: unknown key {key!r}")
    name = raw.get("name")
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        problems.append(f"{where}: name must be 1-40 lower-case letters, digits, '-' or '_'")
        return None
    where = f"consumer {name!r}"
    command = raw.get("command")
    if not isinstance(command, list) or not command or not all(isinstance(a, str) and a for a in command):
        problems.append(f"{where}: command must be a non-empty list of strings (argv, no shell)")
        return None
    if not os.path.isabs(command[0]):
        problems.append(f"{where}: the program {command[0]!r} must be an absolute path")
    elif not (os.path.isfile(command[0]) and os.access(command[0], os.X_OK)):
        problems.append(f"{where}: {command[0]} is not an executable file")
    timeout = raw.get("timeout_s", DEFAULT_TIMEOUT)
    if isinstance(timeout, bool) or not isinstance(timeout, int | float) or not 1 <= timeout <= 600:
        problems.append(f"{where}: timeout_s must be a number of seconds from 1 to 600")
        timeout = DEFAULT_TIMEOUT
    alert = _positive_int(raw.get("alert_after_s", DEFAULT_ALERT_AFTER), minimum=1)
    if alert is None:
        problems.append(f"{where}: alert_after_s must be a whole number of seconds, at least 1")
        alert = DEFAULT_ALERT_AFTER
    retry = raw.get("retry", {})
    delays: tuple[int, ...] = DEFAULT_RETRY_DELAYS
    every, give_up = DEFAULT_RETRY_EVERY, DEFAULT_GIVE_UP_AFTER
    if not isinstance(retry, dict):
        problems.append(f"{where}: retry must be an object")
    else:
        for key in sorted(set(retry) - _RETRY_KEYS):
            problems.append(f"{where}: unknown retry key {key!r}")
        if "delays_s" in retry:
            raw_delays = retry["delays_s"]
            if (
                not isinstance(raw_delays, list)
                or not 1 <= len(raw_delays) <= 20
                or any(_positive_int(d, minimum=1) is None for d in raw_delays)
            ):
                problems.append(f"{where}: retry.delays_s must be 1-20 whole numbers of seconds, each at least 1")
            else:
                delays = tuple(raw_delays)
        every_value = _positive_int(retry.get("every_s", every), minimum=1)
        if every_value is None:
            problems.append(f"{where}: retry.every_s must be a whole number of seconds, at least 1")
        else:
            every = every_value
        give_up_value = _positive_int(retry.get("give_up_after_s", give_up), minimum=0)
        if give_up_value is None:
            problems.append(f"{where}: retry.give_up_after_s must be a whole number of seconds")
        else:
            give_up = give_up_value
    env_pass = raw.get("env_pass", [])
    if not isinstance(env_pass, list) or not all(isinstance(n, str) and _ENV_NAME_RE.fullmatch(n) for n in env_pass):
        problems.append(f"{where}: env_pass must be a list of environment variable names")
        env_pass = []
    for name_ in env_pass:
        if name_.upper().startswith("KITE_"):
            problems.append(f"{where}: env_pass must not pass KITE_* variables ({name_})")
    return Consumer(
        name=name,
        command=tuple(command),
        match=_parse_match(raw.get("match"), where, problems),
        timeout_s=float(timeout),
        alert_after_s=alert,
        retry_delays_s=delays,
        retry_every_s=every,
        give_up_after_s=give_up,
        env_pass=tuple(n for n in env_pass if not n.upper().startswith("KITE_")),
    )


def consumer_env(consumer: Consumer, environ: Mapping[str, str]) -> dict[str, str]:
    """A consumer's whole environment: PATH, HOME, LANG, KITE_EVENT_CONSUMER and the names it asked for.
    Never a KITE_* variable of the service."""
    env = {
        "PATH": environ.get("PATH", os.defpath),
        "LANG": environ.get("LANG", "C.UTF-8"),
        "KITE_EVENT_CONSUMER": consumer.name,
    }
    if "HOME" in environ:
        env["HOME"] = environ["HOME"]
    for name in consumer.env_pass:
        if not name.upper().startswith("KITE_") and name in environ:
            env[name] = environ[name]
    return env


__all__: Sequence[str] = (
    "DEFAULT_RETRY_DELAYS",
    "EXIT_RETRY",
    "Consumer",
    "ConsumersConfig",
    "Match",
    "consumer_env",
)
