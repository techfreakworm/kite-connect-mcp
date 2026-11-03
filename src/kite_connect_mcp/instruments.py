"""The instrument master: Kite's daily CSV dumps of every tradable instrument, cached locally in SQLite.

INTERFACE CONTRACT (used by the order and MF tools; keep it stable):

    store = InstrumentStore(settings, client, clock=...)   # cheap: nothing is loaded until first use
    await store.lookup(exchange, tradingsymbol) -> Instrument | None      # exact, case-insensitive
    await store.by_token(instrument_token) -> Instrument | None
    await store.search(query, *, exchange=None, segment=None, instrument_type=None, expiry=None, limit=20)
        -> list[Instrument]
    await store.lookup_mf(tradingsymbol) -> MFInstrument | None           # by ISIN-like MF tradingsymbol
    await store.search_mf(query, *, amc=None, plan=None, scheme_type=None, limit=20) -> list[MFInstrument]
    await store.status() -> dict                                           # loaded date, counts, file paths
    await store.refresh(force=False) -> dict                               # (re)download today's dumps

`lookup` returns None only when the instrument list is loaded and the symbol is not in it. When the list cannot be
loaded at all, the methods raise errors.InstrumentsUnavailable, and the order tools then skip the tick-size / lot-size
checks with a warning instead of refusing. The same exception answers a miss on a list that is known to be out of
date (a refresh failed and the cached copy predates today's dump): a day-old list cannot prove a symbol is unknown.

How it works
============
Two dumps, loaded independently and only when first needed (an MF question never downloads the equity list):

    GET /instruments       equities, indices, futures, options, currencies, commodities (100k+ rows)
    GET /mf/instruments    mutual fund schemes

Both live in one SQLite file, `<cache_dir>/instruments.sqlite` (the folder is created with mode 700). A refresh parses
the CSV and builds a complete new database in a temporary file next to it, then `os.replace`s it over the old one, so
readers see the old file or the new one, never a half-written one. When only one dump is refreshed the other is copied
across from the old file. Queries open the file read-only for each call (a few hundred microseconds), which keeps them
thread-safe and makes them pick up a replaced file at once. All blocking work (CSV parsing, SQLite) runs in worker
threads; an asyncio lock lets one download run at a time and everyone else wait for its result.

When is the cache stale? Kite regenerates the dumps once a day, around 08:30 IST (DUMP_READY). A dump is stale when it
was loaded before the most recent 08:30 IST, which covers both "loaded on an earlier day and it is now past 08:30" and
"two or more days old". (A load made at 07:00 holds yesterday's dump, so it goes stale at 08:30 the same day.)

When a refresh fails and an older copy exists, that copy keeps being served and status() says so (`stale` and
`last_error`); the next attempt waits RETRY_AFTER so a Kite outage does not slow every call. With no copy at all the
methods raise InstrumentsUnavailable. `refresh(force=True)` always tries again.
"""

from __future__ import annotations

import asyncio
import contextlib
import csv
import datetime as dt
import functools
import io
import json
import logging
import math
import os
import re
import sqlite3
import tempfile
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .errors import InstrumentsUnavailable, KiteMCPError, TokenError, ToolInputError
from .timeutil import IST, now_ist, to_ist

if TYPE_CHECKING:
    from .client.http import KiteClient
    from .config import Settings

log = logging.getLogger(__name__)

DB_FILENAME = "instruments.sqlite"
SCHEMA_VERSION = "1"
DUMP_READY = dt.time(8, 30)  # IST: when Kite's daily dumps are ready ("ideally at around 08:30 AM", market-quotes docs)
RETRY_AFTER = dt.timedelta(minutes=5)  # after a failed refresh, keep serving the old copy this long before retrying
FETCH_TIMEOUT_S = 90.0  # the whole download; the client's own timeout only covers silence on the connection
MAX_SEARCH_LIMIT = 1000
MAX_BAD_ROW_SHARE = 0.2  # a dump with more unreadable rows than this is treated as broken, not as a smaller dump
STALE_TEMP_AGE_S = 24 * 3600  # leftovers of an interrupted build are removed after this long


@dataclass(frozen=True)
class Instrument:
    instrument_token: int
    exchange_token: int
    tradingsymbol: str
    name: str
    last_price: float
    expiry: dt.date | None
    strike: float
    tick_size: Decimal
    lot_size: int
    instrument_type: str  # EQ, FUT, CE, PE
    segment: str  # NSE, NFO-FUT, NFO-OPT, INDICES, ...
    exchange: str

    @property
    def key(self) -> str:
        return f"{self.exchange}:{self.tradingsymbol}"

    def as_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["expiry"] = self.expiry.isoformat() if self.expiry else None
        out["tick_size"] = float(self.tick_size)
        out["key"] = self.key
        return out


@dataclass(frozen=True)
class MFInstrument:
    tradingsymbol: str  # the ISIN, e.g. INF846K01EW2
    amc: str
    name: str
    purchase_allowed: bool
    redemption_allowed: bool
    minimum_purchase_amount: Decimal
    purchase_amount_multiplier: Decimal
    minimum_additional_purchase_amount: Decimal
    minimum_redemption_quantity: Decimal
    redemption_quantity_multiplier: Decimal
    dividend_type: str
    scheme_type: str
    plan: str
    settlement_type: str
    last_price: Decimal
    last_price_date: dt.date | None

    def as_dict(self) -> dict[str, Any]:
        out = asdict(self)
        for key, value in out.items():
            if isinstance(value, Decimal):
                out[key] = float(value)
        out["last_price_date"] = self.last_price_date.isoformat() if self.last_price_date else None
        return out


# --- when is a dump out of date ------------------------------------------------------------------------------------


def latest_publication(now: dt.datetime) -> dt.datetime:
    """The most recent 08:30 IST at or before `now`: the moment Kite last published a fresh dump."""
    local = to_ist(now)
    today = dt.datetime.combine(local.date(), DUMP_READY, tzinfo=IST)
    return today if local >= today else today - dt.timedelta(days=1)


def next_publication(moment: dt.datetime) -> dt.datetime:
    """The first 08:30 IST strictly after `moment`: when a dump loaded at `moment` goes stale."""
    return latest_publication(moment) + dt.timedelta(days=1)


def is_stale(loaded_at: dt.datetime, now: dt.datetime) -> bool:
    return to_ist(loaded_at) < latest_publication(now)


# --- reading the CSV dumps -----------------------------------------------------------------------------------------


class DumpError(KiteMCPError):
    """A dump that downloaded fine but cannot be used (wrong columns, no readable rows, mostly garbage)."""


INSTRUMENT_COLUMNS = (
    "instrument_token",
    "exchange_token",
    "tradingsymbol",
    "name",
    "last_price",
    "expiry",
    "strike",
    "tick_size",
    "lot_size",
    "instrument_type",
    "segment",
    "exchange",
)
# the MF dump's identity is its ISIN and name; the purchase / redemption terms are optional (0 means "no rule known")
MF_COLUMNS = (
    "tradingsymbol",
    "amc",
    "name",
    "purchase_allowed",
    "redemption_allowed",
    "minimum_purchase_amount",
    "purchase_amount_multiplier",
    "minimum_additional_purchase_amount",
    "minimum_redemption_quantity",
    "redemption_quantity_multiplier",
    "dividend_type",
    "scheme_type",
    "plan",
    "settlement_type",
    "last_price",
    "last_price_date",
)
MF_REQUIRED = ("tradingsymbol", "name")


def _decimal(value: str, default: Decimal = Decimal(0)) -> Decimal:
    if not value:
        return default
    out = Decimal(value)
    if not out.is_finite() or out < 0:
        raise ValueError(f"not a usable number: {value!r}")
    return out


_MICRO = Decimal("0.000001")


@functools.lru_cache(maxsize=512)  # a dump has a handful of distinct tick sizes over 100k rows
def _tick(value: str) -> str:
    """A tick size as exact decimal text. Real ticks are round numbers (0.05, 0.0025, 1); anything finer than a
    millionth is single-precision float noise from the exchange feed, and would make every valid price look off-tick."""
    return format(_decimal(value).quantize(_MICRO).normalize(), "f")


def _float(value: str) -> float:
    out = float(value or 0)
    if not math.isfinite(out):
        raise ValueError(f"not a finite number: {value!r}")
    return out


def _whole(value: str, default: int) -> int:
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        return int(Decimal(value))  # "75.0"


@functools.lru_cache(maxsize=4096)  # and a few hundred distinct expiry dates
def _iso_date(value: str) -> str | None:
    return dt.date.fromisoformat(value).isoformat() if value else None


def _lenient_date(value: str) -> str | None:
    try:
        return _iso_date(value)
    except ValueError:
        return None


def _positions(header: Sequence[str], wanted: Sequence[str], required: Sequence[str], what: str) -> list[int]:
    """Where each wanted column sits in the CSV (-1: absent). Raises DumpError when a required column is missing."""
    names = [h.strip().lstrip("\ufeff") for h in header]
    missing = [column for column in required if column not in names]
    if missing:
        raise DumpError(
            f"unexpected {what} dump: the columns {', '.join(missing)} are missing (got: {', '.join(names)[:200]})"
        )
    return [names.index(column) if column in names else -1 for column in wanted]


def _fields(raw: Sequence[str], at: Sequence[int]) -> list[str]:
    """The wanted columns of one row, stripped; "" where the column or the cell is absent."""
    width = len(raw)
    return [raw[i].strip() if 0 <= i < width else "" for i in at]


def _instrument_row(raw: Sequence[str], at: Sequence[int]) -> tuple[Any, ...]:
    """One CSV row as the tuple the `instruments` table stores (columns in INSTRUMENT_COLUMNS order; all of them are
    required, which is why this indexes directly: it runs 100k+ times). Raises ValueError / IndexError for a row that
    cannot be used."""
    token, exchange_token, symbol, name, last, expiry, strike, tick, lot, kind, segment, exchange = [
        raw[i].strip() for i in at
    ]
    if not symbol or not exchange:
        raise ValueError("no tradingsymbol or exchange")
    return (
        int(token),
        _whole(exchange_token, 0),
        symbol,
        name,
        _float(last),
        _iso_date(expiry),
        _float(strike),
        _tick(tick),  # text keeps 0.0025 exact; 0 means "unknown" and disables the tick check
        _whole(lot, 1),
        kind,
        segment,
        exchange,
        symbol.upper(),
        name.upper(),
        f"{symbol} {name}".upper(),
    )


def _mf_row(raw: Sequence[str], at: Sequence[int]) -> tuple[Any, ...]:
    """One row of the MF dump, in MF_COLUMNS order; only the ISIN and name are required."""
    f = dict(zip(MF_COLUMNS, _fields(raw, at), strict=True))
    if not f["tradingsymbol"]:
        raise ValueError("no tradingsymbol")
    symbol, name, amc = f["tradingsymbol"], f["name"], f["amc"]
    return (
        symbol,
        amc,
        name,
        1 if f["purchase_allowed"] in ("1", "true", "True") else 0,
        1 if f["redemption_allowed"] in ("1", "true", "True") else 0,
        str(_decimal(f["minimum_purchase_amount"])),
        str(_decimal(f["purchase_amount_multiplier"])),
        str(_decimal(f["minimum_additional_purchase_amount"])),
        str(_decimal(f["minimum_redemption_quantity"])),
        str(_decimal(f["redemption_quantity_multiplier"])),
        f["dividend_type"],
        f["scheme_type"],
        f["plan"],
        f["settlement_type"],
        str(_decimal(f["last_price"])),
        _lenient_date(f["last_price_date"]),  # a NAV date is informational: do not drop the fund for it
        symbol.upper(),
        name.upper(),
        amc.upper(),
    )


def _read_rows(
    text: str, columns: Sequence[str], required: Sequence[str], what: str, build: Callable[..., tuple[Any, ...]]
) -> tuple[list[tuple[Any, ...]], int]:
    reader = csv.reader(io.StringIO(text))
    header = next(reader, None)
    if not header:
        raise DumpError(f"the {what} dump is empty")
    at = _positions(header, columns, required, what)
    rows: list[tuple[Any, ...]] = []
    skipped = 0
    for raw in reader:
        if not raw:
            continue
        try:
            rows.append(build(raw, at))
        except (ValueError, InvalidOperation, IndexError):
            skipped += 1
    if not rows:
        raise DumpError(f"the {what} dump has no usable rows ({skipped} unreadable)")
    if skipped > MAX_BAD_ROW_SHARE * (len(rows) + skipped):
        raise DumpError(f"the {what} dump looks broken: {skipped} of {len(rows) + skipped} rows are unreadable")
    return rows, skipped


def parse_instruments_csv(text: str) -> tuple[list[tuple[Any, ...]], int]:
    """(rows, skipped): the dump's rows in the `instruments` table's column order, and how many were unreadable."""
    return _read_rows(text, INSTRUMENT_COLUMNS, INSTRUMENT_COLUMNS, "instrument", _instrument_row)


def parse_mf_csv(text: str) -> tuple[list[tuple[Any, ...]], int]:
    return _read_rows(text, MF_COLUMNS, MF_REQUIRED, "mutual fund", _mf_row)


# --- the SQLite file -----------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class DumpSpec:
    key: str  # public name, also the prefix of this dump's rows in the meta table
    endpoint: str
    table: str
    label: str
    parse: Callable[[str], tuple[list[tuple[Any, ...]], int]]
    placeholders: int
    exchange_column: int | None = None  # which row field to count per exchange for status()


INSTRUMENTS = DumpSpec("instruments", "/instruments", "instruments", "instrument list", parse_instruments_csv, 15, 11)
MF_INSTRUMENTS = DumpSpec("mf_instruments", "/mf/instruments", "mf_instruments", "mutual fund list", parse_mf_csv, 19)
SPECS = (INSTRUMENTS, MF_INSTRUMENTS)

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE instruments (
    instrument_token INTEGER NOT NULL, exchange_token INTEGER NOT NULL, tradingsymbol TEXT NOT NULL,
    name TEXT NOT NULL, last_price REAL NOT NULL, expiry TEXT, strike REAL NOT NULL, tick_size TEXT NOT NULL,
    lot_size INTEGER NOT NULL, instrument_type TEXT NOT NULL, segment TEXT NOT NULL, exchange TEXT NOT NULL,
    sym_uc TEXT NOT NULL, name_uc TEXT NOT NULL, text_uc TEXT NOT NULL
);
CREATE TABLE mf_instruments (
    tradingsymbol TEXT NOT NULL, amc TEXT NOT NULL, name TEXT NOT NULL, purchase_allowed INTEGER NOT NULL,
    redemption_allowed INTEGER NOT NULL, minimum_purchase_amount TEXT NOT NULL,
    purchase_amount_multiplier TEXT NOT NULL, minimum_additional_purchase_amount TEXT NOT NULL,
    minimum_redemption_quantity TEXT NOT NULL, redemption_quantity_multiplier TEXT NOT NULL,
    dividend_type TEXT NOT NULL, scheme_type TEXT NOT NULL, plan TEXT NOT NULL, settlement_type TEXT NOT NULL,
    last_price TEXT NOT NULL, last_price_date TEXT,
    sym_uc TEXT NOT NULL, name_uc TEXT NOT NULL, amc_uc TEXT NOT NULL
);
"""
# Kite says exchange + tradingsymbol is the key and that tokens get reused after expiry: neither index is UNIQUE, so a
# duplicate in a dump can never make the whole load fail.
INDEXES = """
CREATE INDEX instruments_key ON instruments (exchange, sym_uc);
CREATE INDEX instruments_token ON instruments (instrument_token);
CREATE INDEX mf_instruments_key ON mf_instruments (sym_uc);
"""
INSTRUMENT_FIELDS = (
    "instrument_token, exchange_token, tradingsymbol, name, last_price, expiry, strike, tick_size, lot_size, "
    "instrument_type, segment, exchange"
)
MF_FIELDS = (
    "tradingsymbol, amc, name, purchase_allowed, redemption_allowed, minimum_purchase_amount, "
    "purchase_amount_multiplier, minimum_additional_purchase_amount, minimum_redemption_quantity, "
    "redemption_quantity_multiplier, dividend_type, scheme_type, plan, settlement_type, last_price, last_price_date"
)


def _uri(path: Path, mode: str) -> str:
    return f"{path.resolve().as_uri()}?mode={mode}"


@dataclass(frozen=True)
class DumpInfo:
    """What the cache file says about one dump (kept in its meta table)."""

    loaded_at: dt.datetime
    rows: int
    skipped: int = 0
    exchanges: dict[str, int] = field(default_factory=dict)


def _write_info(con: sqlite3.Connection, key: str, info: DumpInfo) -> None:
    meta = {
        "loaded_at": to_ist(info.loaded_at).isoformat(timespec="seconds"),
        "rows": str(info.rows),
        "skipped": str(info.skipped),
        "exchanges": json.dumps(info.exchanges, sort_keys=True),
    }
    con.executemany(
        "INSERT OR REPLACE INTO meta VALUES (?, ?)", [(f"{key}.{name}", value) for name, value in meta.items()]
    )


def _infos(meta: Mapping[str, str]) -> dict[str, DumpInfo]:
    out: dict[str, DumpInfo] = {}
    for spec in SPECS:
        try:
            out[spec.key] = DumpInfo(
                loaded_at=dt.datetime.fromisoformat(meta[f"{spec.key}.loaded_at"]),
                rows=int(meta[f"{spec.key}.rows"]),
                skipped=int(meta.get(f"{spec.key}.skipped", "0")),
                exchanges=dict(json.loads(meta.get(f"{spec.key}.exchanges", "{}"))),
            )
        except (KeyError, ValueError, TypeError):
            continue  # that dump is not in this file (yet)
    return out


def read_cache_info(path: Path) -> dict[str, DumpInfo]:
    """What an existing cache file holds, or {} if it is missing, unreadable or from another schema version."""
    try:
        with contextlib.closing(sqlite3.connect(_uri(path, "ro"), uri=True)) as con:
            meta = dict(con.execute("SELECT key, value FROM meta").fetchall())
    except sqlite3.Error:
        return {}
    return _infos(meta) if meta.get("schema") == SCHEMA_VERSION else {}


def _carry_over(con: sqlite3.Connection, spec: DumpSpec) -> None:
    """Copy `spec`'s rows and meta from the attached previous file; on any problem leave that dump empty."""
    try:
        con.execute(f"INSERT INTO main.{spec.table} SELECT * FROM old.{spec.table}")  # noqa: S608 - constant name
        con.execute(
            "INSERT OR REPLACE INTO main.meta SELECT key, value FROM old.meta WHERE substr(key, 1, ?) = ?",
            (len(spec.key) + 1, f"{spec.key}."),
        )
    except sqlite3.Error as exc:
        log.warning("could not carry the %s over from the previous cache: %s", spec.label, exc)
        con.execute(f"DELETE FROM main.{spec.table}")  # noqa: S608 - constant name
        con.execute("DELETE FROM main.meta WHERE substr(key, 1, ?) = ?", (len(spec.key) + 1, f"{spec.key}."))


def _attach_previous(con: sqlite3.Connection, path: Path) -> bool:
    """ATTACH the previous cache file as `old` if it is a readable one of this schema version."""
    if not path.exists():
        return False
    try:
        con.execute("ATTACH DATABASE ? AS old", (_uri(path, "ro"),))
    except sqlite3.Error:
        return False
    try:
        version = con.execute("SELECT value FROM old.meta WHERE key = 'schema'").fetchone()
    except sqlite3.Error:
        version = None
    if version is not None and version[0] == SCHEMA_VERSION:
        return True
    with contextlib.suppress(sqlite3.Error):
        con.execute("DETACH DATABASE old")
    return False


def _remove_old_temp_files(folder: Path) -> None:
    cutoff = time.time() - STALE_TEMP_AGE_S
    for leftover in folder.glob(".instruments-*.tmp"):
        with contextlib.suppress(OSError):
            if leftover.stat().st_mtime < cutoff:
                leftover.unlink()


def build_cache(path: Path, spec: DumpSpec, text: str, loaded_at: dt.datetime) -> dict[str, DumpInfo]:
    """Parse `text` (the CSV of `spec`) into a fresh database and swap it in for `path`. Runs in a worker thread.

    The other dump is copied across from the file being replaced. Nothing is written at `path` itself until the new
    file is complete, and on any error the temporary file is removed and the old cache stays as it was."""
    rows, skipped = spec.parse(text)
    folder = path.parent
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    _remove_old_temp_files(folder)
    fd, name = tempfile.mkstemp(dir=folder, prefix=".instruments-", suffix=".tmp")  # created with mode 600
    os.close(fd)
    tmp = Path(name)
    try:
        con = sqlite3.connect(_uri(tmp, "rwc"), uri=True, isolation_level=None)
        try:
            con.execute("PRAGMA journal_mode = MEMORY")
            con.execute("PRAGMA synchronous = OFF")  # the file is only published once complete; it is just a cache
            con.executescript(SCHEMA)
            attached = _attach_previous(con, path)
            con.execute("BEGIN")
            con.executemany(
                f"INSERT INTO {spec.table} VALUES ({','.join('?' * spec.placeholders)})",  # noqa: S608 - constants
                rows,
            )
            if attached:
                for other in SPECS:
                    if other is not spec:
                        _carry_over(con, other)
            exchanges = Counter(row[spec.exchange_column] for row in rows) if spec.exchange_column is not None else {}
            _write_info(con, spec.key, DumpInfo(loaded_at, len(rows), skipped, dict(exchanges)))
            con.execute("INSERT OR REPLACE INTO meta VALUES ('schema', ?)", (SCHEMA_VERSION,))
            con.execute("COMMIT")
            if attached:
                con.execute("DETACH DATABASE old")
            con.executescript(INDEXES)
            infos = _infos(dict(con.execute("SELECT key, value FROM meta").fetchall()))
        finally:
            con.close()
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
    return infos


# --- searching ---------------------------------------------------------------------------------------------------


def _like(text: str) -> str:
    """`text` with the LIKE wildcards made literal (used with ESCAPE '\\')."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _terms(query: str) -> tuple[str, list[str]]:
    q = " ".join(query.upper().split())
    return q, q.split()


def _check_limit(limit: int) -> int:
    if not 1 <= limit <= MAX_SEARCH_LIMIT:
        raise ToolInputError(f"limit must be between 1 and {MAX_SEARCH_LIMIT}")
    return limit


def expiry_bounds(expiry: dt.date | str) -> tuple[str, str]:
    """[from, to) as ISO dates for a day (a date or 'YYYY-MM-DD') or a whole month ('YYYY-MM')."""
    if not isinstance(expiry, str):
        day = expiry.date() if isinstance(expiry, dt.datetime) else expiry
        return day.isoformat(), (day + dt.timedelta(days=1)).isoformat()
    text = expiry.strip()
    try:
        month = re.fullmatch(r"(\d{4})-(\d{2})", text)
        if month:
            year, number = int(month[1]), int(month[2])
            first = dt.date(year, number, 1)
            following = dt.date(year + (number == 12), number % 12 + 1, 1)
            return first.isoformat(), following.isoformat()
        day = dt.date.fromisoformat(text)
    except (ValueError, OverflowError):
        raise ToolInputError(f"expiry {expiry!r} must be a date (YYYY-MM-DD) or a month (YYYY-MM)") from None
    return day.isoformat(), (day + dt.timedelta(days=1)).isoformat()


_RANK = r"""CASE WHEN sym_uc = :q THEN 0
    WHEN sym_uc LIKE :prefix ESCAPE '\' THEN 1
    WHEN name_uc LIKE :prefix ESCAPE '\' THEN 2
    WHEN sym_uc LIKE :contains ESCAPE '\' THEN 3
    ELSE 4 END"""
# NSE equities first, then its derivatives, then BSE...: the same ticker on several exchanges is mostly asked for NSE
_EXCHANGE_ORDER = (
    "CASE exchange WHEN 'NSE' THEN 0 WHEN 'NFO' THEN 1 WHEN 'BSE' THEN 2 WHEN 'BFO' THEN 3 WHEN 'MCX' THEN 4 ELSE 5 END"
)


def build_search(
    query: str,
    *,
    exchange: str | None = None,
    segment: str | None = None,
    instrument_type: str | None = None,
    expiry: dt.date | str | None = None,
    limit: int = 20,
) -> tuple[str, dict[str, Any]]:
    """The SQL and named parameters of `InstrumentStore.search`.

    Every word of the query must occur in the tradingsymbol or the name, in any order ("bank nifty" finds NIFTY BANK).
    Rank: the exact tradingsymbol, then tradingsymbol prefix, name prefix, tradingsymbol substring, anything else."""
    q, words = _terms(query)
    where: list[str] = []
    params: dict[str, Any] = {
        "q": q,
        "prefix": _like(q) + "%",
        "contains": "%" + _like(q) + "%",
        "limit": _check_limit(limit),
    }
    for i, word in enumerate(words):
        where.append(f"text_uc LIKE :w{i} ESCAPE '\\'")
        params[f"w{i}"] = "%" + _like(word) + "%"
    for column, value in (("exchange", exchange), ("segment", segment), ("instrument_type", instrument_type)):
        if value and value.strip():
            where.append(f"UPPER({column}) = :{column}")
            params[column] = value.strip().upper()
    if expiry is not None and expiry != "":
        where.append("expiry >= :expiry_from AND expiry < :expiry_to")
        params["expiry_from"], params["expiry_to"] = expiry_bounds(expiry)
    if not where:
        raise ToolInputError("give a query or at least one filter (exchange, segment, instrument_type, expiry)")
    order = f"{_RANK}, {_EXCHANGE_ORDER}, LENGTH(tradingsymbol), expiry, strike, tradingsymbol"
    sql = (
        f"SELECT {INSTRUMENT_FIELDS} FROM instruments WHERE {' AND '.join(where)} "  # noqa: S608 - constants only
        f"ORDER BY {order} LIMIT :limit"
    )
    return sql, params


_MF_RANK = r"""CASE WHEN sym_uc = :q THEN 0
    WHEN name_uc LIKE :prefix ESCAPE '\' THEN 1
    WHEN sym_uc LIKE :prefix ESCAPE '\' THEN 2
    WHEN name_uc LIKE :contains ESCAPE '\' THEN 3
    ELSE 4 END"""


def build_mf_search(
    query: str,
    *,
    amc: str | None = None,
    plan: str | None = None,
    scheme_type: str | None = None,
    limit: int = 20,
) -> tuple[str, dict[str, Any]]:
    """The SQL and named parameters of `InstrumentStore.search_mf`: every word of the query must occur in the fund
    name or ISIN; `amc` is a substring of the AMC code, `plan` and `scheme_type` are exact."""
    q, words = _terms(query)
    where: list[str] = []
    params: dict[str, Any] = {
        "q": q,
        "prefix": _like(q) + "%",
        "contains": "%" + _like(q) + "%",
        "limit": _check_limit(limit),
    }
    for i, word in enumerate(words):
        where.append(f"(name_uc LIKE :w{i} ESCAPE '\\' OR sym_uc LIKE :w{i} ESCAPE '\\')")
        params[f"w{i}"] = "%" + _like(word) + "%"
    if amc and amc.strip():
        where.append("amc_uc LIKE :amc ESCAPE '\\'")
        params["amc"] = "%" + _like(amc.strip().upper()) + "%"
    for column, value in (("plan", plan), ("scheme_type", scheme_type)):
        if value and value.strip():
            where.append(f"LOWER({column}) = :{column}")
            params[column] = value.strip().lower()
    if not where:
        raise ToolInputError("give a query or at least one filter (amc, plan, scheme_type)")
    sql = (
        f"SELECT {MF_FIELDS} FROM mf_instruments WHERE {' AND '.join(where)} "  # noqa: S608 - constants only
        f"ORDER BY {_MF_RANK}, name, tradingsymbol LIMIT :limit"
    )
    return sql, params


def _to_instrument(row: Sequence[Any]) -> Instrument:
    return Instrument(
        instrument_token=row[0],
        exchange_token=row[1],
        tradingsymbol=row[2],
        name=row[3],
        last_price=row[4],
        expiry=dt.date.fromisoformat(row[5]) if row[5] else None,
        strike=row[6],
        tick_size=Decimal(row[7]),
        lot_size=row[8],
        instrument_type=row[9],
        segment=row[10],
        exchange=row[11],
    )


def _to_mf(row: Sequence[Any]) -> MFInstrument:
    return MFInstrument(
        tradingsymbol=row[0],
        amc=row[1],
        name=row[2],
        purchase_allowed=bool(row[3]),
        redemption_allowed=bool(row[4]),
        minimum_purchase_amount=Decimal(row[5]),
        purchase_amount_multiplier=Decimal(row[6]),
        minimum_additional_purchase_amount=Decimal(row[7]),
        minimum_redemption_quantity=Decimal(row[8]),
        redemption_quantity_multiplier=Decimal(row[9]),
        dividend_type=row[10],
        scheme_type=row[11],
        plan=row[12],
        settlement_type=row[13],
        last_price=Decimal(row[14]),
        last_price_date=dt.date.fromisoformat(row[15]) if row[15] else None,
    )


# --- the store -----------------------------------------------------------------------------------------------------


@dataclass
class _DumpState:
    """What this process knows about one dump."""

    spec: DumpSpec
    info: DumpInfo | None = None
    error: str | None = None  # why the last refresh failed; cleared by a successful one
    retry_after: dt.datetime | None = None

    @property
    def loaded_at(self) -> dt.datetime | None:
        return self.info.loaded_at if self.info else None

    def describe(self, now: dt.datetime) -> dict[str, Any]:
        out: dict[str, Any] = {"loaded": self.info is not None}
        if self.info is not None:
            out.update(
                rows=self.info.rows,
                loaded_at=to_ist(self.info.loaded_at).isoformat(timespec="seconds"),
                stale=is_stale(self.info.loaded_at, now),
                next_refresh=next_publication(self.info.loaded_at).isoformat(timespec="minutes"),
            )
            if self.info.exchanges:
                out["exchanges"] = self.info.exchanges
            if self.info.skipped:
                out["unreadable_rows"] = self.info.skipped
        if self.error:
            out["last_error"] = self.error
            if self.retry_after is not None:
                out["next_attempt_after"] = to_ist(self.retry_after).isoformat(timespec="seconds")
        return out


class InstrumentStore:
    """Instrument lookups and search over the cached dumps (see the module docstring)."""

    def __init__(self, settings: Settings, client: KiteClient, *, clock: Callable[[], dt.datetime] = now_ist):
        self._client = client
        self._clock = clock
        self._path = Path(settings.cache_dir) / DB_FILENAME
        self._lock = asyncio.Lock()  # one download at a time; everyone else waits for its result
        self._open_lock = asyncio.Lock()  # reading the cache file's bookkeeping: quick, and never behind a download
        self._opened = False  # has the cache file's own bookkeeping been read yet?
        self._dumps = {spec.key: _DumpState(spec) for spec in SPECS}

    @property
    def cache_file(self) -> Path:
        return self._path

    # --- lookups ----------------------------------------------------------------------------------------------------

    async def lookup(self, exchange: str, tradingsymbol: str) -> Instrument | None:
        key = (exchange.strip().upper(), tradingsymbol.strip().upper())
        sql = f"SELECT {INSTRUMENT_FIELDS} FROM instruments WHERE exchange = ? AND sym_uc = ? ORDER BY rowid LIMIT 1"  # noqa: S608
        rows = await self._query(INSTRUMENTS, sql, key)
        if rows:
            return _to_instrument(rows[0])
        self._miss_means_unknown(INSTRUMENTS, f"{key[0]}:{key[1]}")
        return None

    async def by_token(self, instrument_token: int) -> Instrument | None:
        sql = f"SELECT {INSTRUMENT_FIELDS} FROM instruments WHERE instrument_token = ? ORDER BY rowid LIMIT 1"  # noqa: S608
        rows = await self._query(INSTRUMENTS, sql, (int(instrument_token),))
        if rows:
            return _to_instrument(rows[0])
        self._miss_means_unknown(INSTRUMENTS, f"instrument token {instrument_token}")
        return None

    async def search(
        self,
        query: str,
        *,
        exchange: str | None = None,
        segment: str | None = None,
        instrument_type: str | None = None,
        expiry: dt.date | str | None = None,
        limit: int = 20,
    ) -> list[Instrument]:
        """Instruments matching `query` (case-insensitive; every word in the tradingsymbol or name), best first.

        `expiry` is a date or "YYYY-MM" (that whole month). An empty query is fine when a filter is given."""
        sql, params = build_search(
            query, exchange=exchange, segment=segment, instrument_type=instrument_type, expiry=expiry, limit=limit
        )
        return [_to_instrument(row) for row in await self._query(INSTRUMENTS, sql, params)]

    async def lookup_mf(self, tradingsymbol: str) -> MFInstrument | None:
        sql = f"SELECT {MF_FIELDS} FROM mf_instruments WHERE sym_uc = ? ORDER BY rowid LIMIT 1"  # noqa: S608
        rows = await self._query(MF_INSTRUMENTS, sql, (tradingsymbol.strip().upper(),))
        if rows:
            return _to_mf(rows[0])
        self._miss_means_unknown(MF_INSTRUMENTS, tradingsymbol.strip().upper())
        return None

    async def search_mf(
        self,
        query: str,
        *,
        amc: str | None = None,
        plan: str | None = None,
        scheme_type: str | None = None,
        limit: int = 20,
    ) -> list[MFInstrument]:
        sql, params = build_mf_search(query, amc=amc, plan=plan, scheme_type=scheme_type, limit=limit)
        return [_to_mf(row) for row in await self._query(MF_INSTRUMENTS, sql, params)]

    # --- status and refresh -----------------------------------------------------------------------------------------

    async def status(self) -> dict[str, Any]:
        """What is cached and how old it is. Never downloads anything, and never waits for a download."""
        await self._ensure_open()
        return self._describe()

    async def refresh(self, force: bool = False) -> dict[str, Any]:
        """Download the dumps again: the stale or missing ones, or both with `force`. Returns status() plus
        `refreshed`, the dumps that were replaced. A failed dump keeps its old copy (see `last_error`); only when the
        instrument list ends up unavailable altogether does this raise InstrumentsUnavailable."""
        refreshed: list[str] = []
        await self._ensure_open()
        async with self._lock:
            now = self._clock()
            for state in self._dumps.values():
                if not force and state.loaded_at is not None and not is_stale(state.loaded_at, now):
                    continue
                try:
                    await self._load(state)
                    refreshed.append(state.spec.key)
                except Exception as exc:
                    self._record_failure(state, exc, now)
            main = self._dumps[INSTRUMENTS.key]
            if main.info is None:
                raise InstrumentsUnavailable(
                    f"the {INSTRUMENTS.label} could not be loaded and no cached copy exists: {main.error}"
                )
        return {**self._describe(), "refreshed": refreshed}

    def _describe(self) -> dict[str, Any]:
        now = self._clock()
        main = self._dumps[INSTRUMENTS.key]
        out: dict[str, Any] = {
            "loaded": main.info is not None,
            "stale": main.info is not None and is_stale(main.info.loaded_at, now),
            "refreshing": self._lock.locked(),
            "cache_file": str(self._path),
        }
        for key, state in self._dumps.items():
            out[key] = state.describe(now)
        return out

    # --- loading ----------------------------------------------------------------------------------------------------

    async def _ensure_open(self) -> None:
        """Read what an earlier run left in the cache file: once per process, and again after the file was damaged."""
        if self._opened:
            return
        async with self._open_lock:
            if self._opened:
                return
            infos = await asyncio.to_thread(read_cache_info, self._path)
            for key, state in self._dumps.items():
                state.info = infos.get(key)
            self._opened = True

    async def _ready(self, spec: DumpSpec) -> None:
        """Make sure `spec`'s dump is loaded and current, refreshing it if not. Returns normally when there is at least
        an older copy to serve; raises InstrumentsUnavailable when there is nothing."""
        state = self._dumps[spec.key]
        await self._ensure_open()
        if state.loaded_at is not None and not is_stale(state.loaded_at, self._clock()):
            return
        async with self._lock:
            now = self._clock()
            if state.loaded_at is not None and not is_stale(state.loaded_at, now):
                return  # another task refreshed it while this one waited for the lock
            if state.retry_after is None or now >= state.retry_after:
                try:
                    await self._load(state)
                    return
                except Exception as exc:
                    self._record_failure(state, exc, now)
            if state.info is None:  # (a recent failure, or this one) and nothing to fall back on
                raise InstrumentsUnavailable(
                    f"the {spec.label} could not be loaded and no cached copy exists: {state.error}"
                )

    async def _load(self, state: _DumpState) -> None:
        """Download one dump and swap it into the cache file. The caller holds the lock."""
        spec = state.spec
        started = time.monotonic()
        text = await self._fetch(spec.endpoint)
        infos = await asyncio.to_thread(build_cache, self._path, spec, text, self._clock())
        for key, other in self._dumps.items():
            other.info = infos.get(key)
        state.error = None
        state.retry_after = None
        log.info(
            "loaded the %s: %d rows in %.1fs",
            spec.label,
            state.info.rows if state.info else 0,
            time.monotonic() - started,
        )

    async def _fetch(self, endpoint: str) -> str:
        async with asyncio.timeout(FETCH_TIMEOUT_S):
            try:
                return await self._client.get_csv(endpoint)
            except TokenError:
                # the client has dropped the rejected token; the dumps are public, so ask again without it
                return await self._client.get_csv(endpoint)

    def _record_failure(self, state: _DumpState, exc: Exception, now: dt.datetime) -> None:
        if isinstance(exc, TimeoutError):
            reason = f"timed out after {FETCH_TIMEOUT_S:g}s"
        else:
            reason = f"{type(exc).__name__}: {exc}" if not isinstance(exc, KiteMCPError) else str(exc)
            if not isinstance(exc, KiteMCPError | OSError | sqlite3.Error | csv.Error | ValueError):
                log.exception("unexpected failure while loading the %s", state.spec.label)
        state.error = self._client.scrub(reason)[:300]
        state.retry_after = now + RETRY_AFTER
        log.warning("could not refresh the %s: %s", state.spec.label, state.error)

    def _miss_means_unknown(self, spec: DumpSpec, what: str) -> None:
        """Called when `what` was not found. On an outdated list that proves nothing, so say so instead of None."""
        state = self._dumps[spec.key]
        if state.info is not None and is_stale(state.info.loaded_at, self._clock()):
            day = to_ist(state.info.loaded_at).date().isoformat()
            raise InstrumentsUnavailable(
                f"{what} is not in the cached {spec.label}, which dates from {day} and could not be refreshed "
                f"({state.error or 'refresh pending'}); it may be newer than the cache"
            )

    # --- querying ---------------------------------------------------------------------------------------------------

    async def _query(
        self, spec: DumpSpec, sql: str, params: Sequence[Any] | Mapping[str, Any]
    ) -> list[tuple[Any, ...]]:
        for attempt in (1, 2):
            await self._ready(spec)
            try:
                return await asyncio.to_thread(self._select, sql, params)
            except sqlite3.Error as exc:
                # the file was removed or damaged under us: forget it and rebuild it once
                log.warning("the instrument cache could not be read (%s)%s", exc, "; reloading" if attempt == 1 else "")
                self._opened = False
                for state in self._dumps.values():
                    state.info = None
        raise InstrumentsUnavailable("the instrument cache file cannot be read even after reloading it")

    def _select(self, sql: str, params: Sequence[Any] | Mapping[str, Any]) -> list[tuple[Any, ...]]:
        with contextlib.closing(sqlite3.connect(_uri(self._path, "ro"), uri=True)) as con:
            return con.execute(sql, params).fetchall()
