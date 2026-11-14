"""The consumers file: which consumer gets which event, how long its command may take and how its delivery is retried,
which environment the command gets, and the labels shown for order tags. The router (events/router.py) runs the
commands.

The consumers file (KITE_MCP_EVENTS_CONSUMERS_FILE) is JSON:

    {"tag_labels": {"mytag": "my strategy"},
     "consumers": [{"name": "notify", "command": ["/usr/local/bin/notify-me"],
                    "match": {"kinds": ["order", "session", "router", "selftest"], "changes": ["filled"]},
                    "timeout_s": 30, "alert_after_s": 60,
                    "retry": {"delays_s": [10, 30, 60, 120, 300], "every_s": 300, "give_up_after_s": 1800},
                    "env_pass": ["NOTIFY_TOKEN"]}]}

A consumer gets order events unless `match.kinds` says otherwise; session, router and selftest events only reach the
consumers that list those kinds. The order filters (statuses, changes, tags, transaction_types, sources) apply to order
events only, so a consumer that gives one must list "order" among its kinds. A key given twice anywhere in the file is
refused: JSON keeps the last one, which would silently drop a filter or a whole list.

After a failed try a delivery waits `retry.delays_s[n]` seconds, then `every_s` between later tries, and gives up once
`give_up_after_s` seconds have passed since the first try: 0 means one try and no retries. Every time is at most a day
(86400 s), so a pending delivery never outlives its event in the store (kept for at least a day).

The command is an argv list (no shell) whose program is an absolute path. Its arguments are visible to other users
(`ps`): put secrets in the environment instead. `env_pass` names variables of the service's own environment to pass on;
the service's KITE_* variables are never passed. The commands run as the service's user, who can read its Kite token,
so the file and each program must be regular files that only that user or root can change, reached through folders
and links only they can change, like OpenSSH's StrictModes: see events/trust.py. A script given as an argument
(["/usr/bin/python3", "/home/me/notify.py"]) is not checked: keep it writable by you or root only.

What a command prints to stderr ends up, cleaned and clipped, in the service's log, in `events status` and in the other
consumers' delivery_failed events: never print a secret there. A process the command starts in the background must
not keep its stderr open (redirect it), or the delivery waits for it and times out. The whole contract (stdin, exit
codes, timeouts) is in events/router.py.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any

from ..errors import ConfigError
from .model import CHANGES, KINDS, ORDER_SOURCES, STATUSES, TRANSACTION_TYPES
from .trust import UntrustedFileError, check_owned_file, read_owned_file

FILE_SETTING = "KITE_MCP_EVENTS_CONSUMERS_FILE"
DEFAULT_RETRY_DELAYS = (10, 30, 60, 120, 300)
DEFAULT_RETRY_EVERY = 300
DEFAULT_GIVE_UP_AFTER = 1800
DEFAULT_TIMEOUT = 30.0
DEFAULT_ALERT_AFTER = 60
MAX_TIMEOUT = 600
MAX_SECONDS = 86400  # a day: the longest wait, alert or retry window
MAX_LABEL_CHARS = 40
EXIT_RETRY = 75  # EX_TEMPFAIL: "try again later"

_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,39}")
_ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_JOINERS = "\u200c\u200d"  # zero-width (non-)joiner: format characters that Indic scripts and emoji need in a label
_MISSING: Any = object()
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


@dataclass(frozen=True)
class ConsumersConfig:
    consumers: tuple[Consumer, ...] = ()
    tag_labels: Mapping[str, str] = field(default_factory=dict, hash=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "tag_labels", MappingProxyType(dict(self.tag_labels)))  # read-only, like the rest

    def matching(self, event: Mapping[str, Any], *, exclude: str | None = None) -> list[str]:
        return [c.name for c in self.consumers if c.name != exclude and c.match.accepts(event)]

    def get(self, name: str) -> Consumer | None:
        return next((c for c in self.consumers if c.name == name), None)

    @classmethod
    def load(cls, path: Path | None) -> ConsumersConfig:
        """The consumers of `path` (none without a file). Every problem is a ConfigError that names the setting."""
        if path is None:
            return cls()
        try:
            raw = read_owned_file(path, "the consumers file")  # the real file that was checked
        except UntrustedFileError as exc:
            raise ConfigError(f"{FILE_SETTING}: {exc}") from None
        try:
            text = raw.decode("utf-8-sig")  # a byte order mark is ignored, as JSON parsers may
        except UnicodeDecodeError as exc:
            raise ConfigError(f"{FILE_SETTING}: {path} is not UTF-8 text: {exc.reason} at byte {exc.start}") from None
        try:
            data = json.loads(text, object_pairs_hook=_unique_keys)  # a key given twice is a ValueError too
        except (ValueError, RecursionError) as exc:  # RecursionError: nested deeper than the parser goes
            raise ConfigError(f"{FILE_SETTING}: {path} is not valid JSON: {exc}") from None
        problems: list[str] = []
        config = cls._parse(data, problems)
        if problems:
            raise ConfigError(f"{FILE_SETTING}: invalid consumers file {path}:\n  - " + "\n  - ".join(problems))
        return config

    @classmethod
    def _parse(cls, data: Any, problems: list[str]) -> ConsumersConfig:
        if not isinstance(data, dict):
            problems.append("the file must hold a JSON object")
            return cls()
        for key in sorted(set(data) - _TOP_KEYS):
            problems.append(f"unknown key {key!r} (known: consumers, tag_labels)")
        labels = _parse_labels(data.get("tag_labels", {}), problems)
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
        return cls(tuple(consumers), labels)


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
        if not _is_kite_name(name) and name in environ:
            env[name] = environ[name]
    return env


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    data: dict[str, Any] = {}
    for key, value in pairs:
        if key in data:
            raise ValueError(f"duplicate key {key!r}")
        data[key] = value
    return data


def _is_kite_name(name: str) -> bool:
    return name.upper().startswith("KITE_")


def _whole_seconds(value: Any, *, minimum: int) -> int | None:
    """`value` when it is a whole number of seconds from `minimum` to MAX_SECONDS, else None."""
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= MAX_SECONDS:
        return None
    return value


def _parse_labels(raw: Any, problems: list[str]) -> dict[str, str]:
    if isinstance(raw, dict):
        labels = {tag: value.strip() for tag, value in raw.items() if isinstance(value, str)}
        if len(labels) == len(raw) and all(_is_label(label) for label in labels.values()):
            return labels
    problems.append(f"tag_labels must map each tag to a one-line label of at most {MAX_LABEL_CHARS} characters")
    return {}


def _is_label(label: str) -> bool:
    return 0 < len(label) <= MAX_LABEL_CHARS and all(c.isprintable() or c in _JOINERS for c in label)


def _parse_match(raw: Any, where: str, problems: list[str]) -> Match:
    if raw is _MISSING:
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
    if "order" not in kinds:
        for key in values:
            problems.append(f"{where}: match.{key} applies to order events only, but match.kinds leaves out 'order'")
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
    if (
        not isinstance(command, list)
        or not command
        or not all(isinstance(a, str) and a and "\x00" not in a for a in command)
    ):
        problems.append(f"{where}: command must be a non-empty list of non-empty strings without NUL (argv, no shell)")
        return None
    program = command[0]
    if not os.path.isabs(program):
        problems.append(f"{where}: the program {program!r} must be an absolute path")
    elif not (os.path.isfile(program) and os.access(program, os.X_OK)):
        problems.append(f"{where}: {program} is not an executable file")
    elif (problem := check_owned_file(program, "the program")) is not None:
        problems.append(f"{where}: {problem}")
    timeout = raw.get("timeout_s", DEFAULT_TIMEOUT)
    if isinstance(timeout, bool) or not isinstance(timeout, int | float) or not 1 <= timeout <= MAX_TIMEOUT:
        problems.append(f"{where}: timeout_s must be a number of seconds from 1 to {MAX_TIMEOUT}")
        timeout = DEFAULT_TIMEOUT
    alert = _whole_seconds(raw.get("alert_after_s", DEFAULT_ALERT_AFTER), minimum=1)
    if alert is None:
        problems.append(f"{where}: alert_after_s must be a whole number of seconds from 1 to {MAX_SECONDS}")
        alert = DEFAULT_ALERT_AFTER
    delays, every, give_up = _parse_retry(raw.get("retry", {}), where, problems)
    env_pass = raw.get("env_pass", [])
    if not isinstance(env_pass, list) or not all(isinstance(n, str) and _ENV_NAME_RE.fullmatch(n) for n in env_pass):
        problems.append(f"{where}: env_pass must be a list of environment variable names")
        env_pass = []
    for env_name in env_pass:
        if _is_kite_name(env_name):
            problems.append(f"{where}: env_pass must not pass KITE_* variables ({env_name})")
    return Consumer(
        name=name,
        command=tuple(command),
        match=_parse_match(raw.get("match", _MISSING), where, problems),
        timeout_s=float(timeout),
        alert_after_s=alert,
        retry_delays_s=delays,
        retry_every_s=every,
        give_up_after_s=give_up,
        env_pass=tuple(n for n in env_pass if not _is_kite_name(n)),
    )


def _parse_retry(retry: Any, where: str, problems: list[str]) -> tuple[tuple[int, ...], int, int]:
    """(delays_s, every_s, give_up_after_s); the defaults where `retry` gives none or a wrong one."""
    delays: tuple[int, ...] = DEFAULT_RETRY_DELAYS
    if not isinstance(retry, dict):
        problems.append(f"{where}: retry must be an object")
        return delays, DEFAULT_RETRY_EVERY, DEFAULT_GIVE_UP_AFTER
    for key in sorted(set(retry) - _RETRY_KEYS):
        problems.append(f"{where}: unknown retry key {key!r}")
    if "delays_s" in retry:
        raw_delays = retry["delays_s"]
        if (
            not isinstance(raw_delays, list)
            or not 1 <= len(raw_delays) <= 20
            or any(_whole_seconds(d, minimum=1) is None for d in raw_delays)
        ):
            problems.append(
                f"{where}: retry.delays_s must be 1-20 whole numbers of seconds, each from 1 to {MAX_SECONDS}"
            )
        else:
            delays = tuple(raw_delays)
    every = _whole_seconds(retry.get("every_s", DEFAULT_RETRY_EVERY), minimum=1)
    if every is None:
        problems.append(f"{where}: retry.every_s must be a whole number of seconds from 1 to {MAX_SECONDS}")
        every = DEFAULT_RETRY_EVERY
    give_up = _whole_seconds(retry.get("give_up_after_s", DEFAULT_GIVE_UP_AFTER), minimum=0)
    if give_up is None:
        problems.append(
            f"{where}: retry.give_up_after_s must be a whole number of seconds from 0 (one try, no retries) "
            f"to {MAX_SECONDS}"
        )
        give_up = DEFAULT_GIVE_UP_AFTER
    return delays, every, give_up


__all__: Sequence[str] = (
    "DEFAULT_RETRY_DELAYS",
    "EXIT_RETRY",
    "FILE_SETTING",
    "Consumer",
    "ConsumersConfig",
    "Match",
    "consumer_env",
)
