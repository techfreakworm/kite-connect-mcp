"""What one read of a Kite order means, and the events built from it.

An order row (GET /orders, GET /orders/{id}) becomes an OrderView: the documented fields worth keeping, the status
reduced to OPEN or one of the three final statuses, `gtt` when a GTT placed the order, and the status message clipped
and stripped of account identifiers. Nothing else from the row survives: no user_id, placed_by, app_id, checksum, guid
or the rest of meta.

judge() compares a view with the order's last stored state and names the change it reports, or None when it reports
nothing new. Progress only moves forward: after a final status nothing more is reported, and a read whose filled
quantity, or exchange update time, is behind the stored one is dropped (see judge() for the one case it cannot tell).

An order event's id chains to the order's previous event: it digests the order id, the id of the event the store holds
last for the order, and the new state. Two reads judged against the same stored state, such as a postback and the poll
reporting one change, get the same id and are stored once; an order that goes back to an earlier state (a price moved
and moved back) gets a new id each time. `state_id` is the plain digest of the order and its state.

Every event is a JSON object with `kind`, `event_id`, `source`, `received_at` and `text`, a one-line English summary
that never holds an id, a user or a link. The store adds `seq` when it reads an event back.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any

from ..timeutil import parse_kite_datetime, to_ist

FINAL_STATUSES = frozenset({"COMPLETE", "CANCELLED", "REJECTED"})
STATUSES = frozenset({"OPEN", *FINAL_STATUSES})
CHANGES = ("placed", "partial_fill", "modified", "filled", "cancelled", "rejected", "seen_final")
KINDS = ("order", "session", "router", "selftest")
ORDER_SOURCES = ("postback", "poll")
TRANSACTION_TYPES = ("BUY", "SELL")
SESSION_TEXT = {
    "token_rejected": "Kite rejected the stored access token: log in again to resume the order events.",
}
ORDER_ID_RE = re.compile(r"[A-Za-z0-9_\-]{1,64}")  # the only order-id shape passed to GET /orders/{id}
SELFTEST_PREFIX = "selftest-"
MAX_REASON_CHARS = 160
MAX_ERROR_CHARS = 200
MAX_EXPONENT = 30  # no price or quantity needs more digits; a number further out is not one

_FINAL_CHANGE = {"COMPLETE": "filled", "CANCELLED": "cancelled", "REJECTED": "rejected"}
_ACCOUNT_RE = re.compile(r"(?i)\baccount-[A-Za-z0-9]+")
_LINK_RE = re.compile(r"(?i)\b(?:https?://|www\.)\S+")
_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")  # a terminal's control sequence (colour, cursor): ESC [ ... letter


def normalize_status(raw: object) -> str:
    """OPEN, COMPLETE, CANCELLED or REJECTED. Kite's "CANCEL" is CANCELLED; every other word (UPDATE, OPEN, TRIGGER
    PENDING, AMO REQ RECEIVED, the *PENDING states, MODIFIED) is OPEN."""
    word = str(raw or "").strip().upper()
    if word in ("CANCEL", "CANCELLED"):
        return "CANCELLED"
    if word in ("COMPLETE", "REJECTED"):
        return word
    return "OPEN"


def canonical(value: object) -> str:
    """A number as canonical decimal text, so that 5, 5.0 and "5.00" are all "5". Anything else, including a number
    with more than MAX_EXPONENT digits before or after the point, is ""."""
    if value is None or isinstance(value, bool):
        return ""
    try:
        number = Decimal(str(value).strip())
        if not number.is_finite():
            return ""
        if number == 0:
            return "0"
        if abs(number.adjusted()) > MAX_EXPONENT:
            return ""
        return format(number.normalize(), "f")
    except (ArithmeticError, ValueError):  # decimal's InvalidOperation and Overflow are ArithmeticErrors
        return ""


def _number(value: object) -> int | float | None:
    text = canonical(value)
    if not text:
        return None
    number = Decimal(text)
    return int(number) if number == number.to_integral_value() else float(number)


def _word(value: object, limit: int = 80) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()[:limit]
    return None


def clean_reason(message: object, row: Mapping[str, Any]) -> str | None:
    """Kite's status message, short and without account identifiers or links. RMS messages name the account
    ("... for entity account-AB1234 ..."), so the row's own user ids are removed, and that pattern too."""
    if not isinstance(message, str) or not message.strip():
        return None
    text = " ".join(message.split())
    for key in ("user_id", "placed_by"):
        ident = row.get(key)
        if isinstance(ident, str) and len(ident.strip()) >= 3:
            text = re.sub(re.escape(ident.strip()), "[account]", text, flags=re.IGNORECASE)
    text = _ACCOUNT_RE.sub("account-[account]", text)
    text = _LINK_RE.sub("[link]", text)
    if len(text) > MAX_REASON_CHARS:
        text = text[: MAX_REASON_CHARS - 3].rstrip() + "..."
    return text or None


def digest(*parts: str) -> str:
    """A stable id: the first 32 hex digits of SHA-256 over the parts."""
    return hashlib.sha256("\x1f".join(parts).encode("utf-8", "replace")).hexdigest()[:32]


def _tag(row: Mapping[str, Any]) -> str | None:
    tag = row.get("tag")
    if not (isinstance(tag, str) and tag.strip()):
        tags = row.get("tags")
        tag = tags[0] if isinstance(tags, list) and tags and isinstance(tags[0], str) else None
    return _word(tag, 40)


@dataclass(frozen=True)
class OrderView:
    order_id: str
    status: str  # OPEN, COMPLETE, CANCELLED or REJECTED
    status_raw: str  # Kite's own word
    status_message: str | None  # see clean_reason
    tag: str | None
    gtt: bool  # placed by a GTT trigger (the row carries meta.gtt)
    tradingsymbol: str | None
    exchange: str | None
    transaction_type: str | None
    order_type: str | None
    product: str | None
    variety: str | None
    validity: str | None
    quantity: int | float | None
    filled_quantity: int | float | None
    pending_quantity: int | float | None
    cancelled_quantity: int | float | None
    price: int | float | None
    trigger_price: int | float | None
    average_price: int | float | None
    order_timestamp: str | None
    exchange_update_timestamp: str | None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> OrderView:
        """Raises ValueError when the row has no valid order_id."""
        order_id = row.get("order_id")
        if not isinstance(order_id, str) or not ORDER_ID_RE.fullmatch(order_id):
            raise ValueError("the order row has no valid order_id")
        meta = row.get("meta")
        status_raw = (_word(row.get("status"), 40) or "").upper()
        return cls(
            order_id=order_id,
            status=normalize_status(status_raw),
            status_raw=status_raw,
            status_message=clean_reason(row.get("status_message"), row),
            tag=_tag(row),
            gtt=isinstance(meta, Mapping) and bool(meta.get("gtt")),
            tradingsymbol=_word(row.get("tradingsymbol")),
            exchange=_word(row.get("exchange")),
            transaction_type=_word(row.get("transaction_type")),
            order_type=_word(row.get("order_type")),
            product=_word(row.get("product")),
            variety=_word(row.get("variety")),
            validity=_word(row.get("validity")),
            quantity=_number(row.get("quantity")),
            filled_quantity=_number(row.get("filled_quantity")),
            pending_quantity=_number(row.get("pending_quantity")),
            cancelled_quantity=_number(row.get("cancelled_quantity")),
            price=_number(row.get("price")),
            trigger_price=_number(row.get("trigger_price")),
            average_price=_number(row.get("average_price")),
            order_timestamp=_word(row.get("order_timestamp"), 40),
            exchange_update_timestamp=_word(row.get("exchange_update_timestamp"), 40),
        )

    @property
    def final(self) -> bool:
        return self.status in FINAL_STATUSES

    @property
    def filled(self) -> Decimal:
        text = canonical(self.filled_quantity)
        return Decimal(text) if text else Decimal(0)

    def state(self) -> tuple[str, str, str, str, str]:
        """(status, filled, quantity, price, trigger price) in canonical text: what deduplication compares."""
        return (
            self.status,
            canonical(self.filled_quantity) or "0",
            canonical(self.quantity),
            canonical(self.price),
            canonical(self.trigger_price),
        )

    def state_id(self) -> str:
        return digest("order", self.order_id, *self.state())

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Previous:
    """What the store holds for an order: its last state, and the event_id of its last stored event ("" when the state
    was recorded without an event, as the first poll's baseline is)."""

    state: tuple[str, ...]
    final: bool
    filled: Decimal
    exchange_update_timestamp: str | None
    event_id: str


def _older(new: str | None, old: str | None) -> bool:
    if not new or not old:
        return False
    try:
        return parse_kite_datetime(new) < parse_kite_datetime(old)
    except ValueError:
        return False


def judge(view: OrderView, previous: Previous | None) -> str | None:
    """The change this read reports against the order's last stored state, or None when it reports nothing new.

    The first sight of an order is `placed`, `partial_fill` when it is open with a fill already (the service started
    after the fill), or `seen_final`. A read whose exchange update time is older than the stored one is dropped. Known
    limit: when either read lacks that time, or both carry the same second, a late read of an earlier state cannot be
    told from a real modification back to it, and is reported as `modified`.
    """
    if previous is None:
        if view.final:
            return "seen_final"
        return "partial_fill" if view.filled > 0 else "placed"
    if previous.final or view.filled < previous.filled:
        return None
    if _older(view.exchange_update_timestamp, previous.exchange_update_timestamp):
        return None
    if view.state() == tuple(previous.state):
        return None
    if view.final:
        return _FINAL_CHANGE[view.status]
    if view.filled > previous.filled:
        return "partial_fill"
    return "modified"


def _qty(value: int | float | None) -> str:
    return canonical(value) or "?"


def _progress(view: OrderView) -> str:
    return f"{_qty(view.filled_quantity)} of {_qty(view.quantity)}"


def _price(value: int | float | None) -> str:
    text = canonical(value)
    if not text:
        return "?"
    number = Decimal(text)
    exponent = number.as_tuple().exponent
    if isinstance(exponent, int) and exponent < -2:
        return text  # e.g. a currency future quoted to four places
    return f"{number:.2f}"


def _terms(view: OrderView) -> str:
    kind = (view.order_type or "").upper()
    if kind == "LIMIT":
        return f"LIMIT {_price(view.price)}"
    if kind == "MARKET":
        return "MARKET"
    if kind == "SL":
        return f"SL {_price(view.price)}, trigger {_price(view.trigger_price)}"
    if kind == "SL-M":
        return f"SL-M, trigger {_price(view.trigger_price)}"
    return kind or "?"


def text_for(view: OrderView, change: str, tag_labels: Mapping[str, str] | None = None) -> str:
    """One line of English that a consumer can forward as it is. Never an id, a user or a link."""
    head = f"{view.transaction_type or '?'} {_qty(view.quantity)} {view.tradingsymbol or '?'} ({view.exchange or '?'})"
    if view.gtt:
        head += " (GTT)"
    if change == "seen_final" and not view.final:
        raise ValueError("seen_final needs a final status")
    what = _FINAL_CHANGE[view.status] if change == "seen_final" else change
    if what == "placed":
        line = f"{head} placed: {_terms(view)}"
    elif what == "modified":
        line = f"{head} modified: {_terms(view)}"
    elif what == "partial_fill":
        line = f"{head} partly filled: {_progress(view)} at {_price(view.average_price)}"
    elif what == "filled":
        line = f"{head} filled at {_price(view.average_price)}: complete"
    elif what == "cancelled":
        line = f"{head} cancelled"
        if view.filled > 0:
            line += f" after {_progress(view)} filled at {_price(view.average_price)}"
        if view.status_message:
            line += f": {view.status_message}"
    elif what == "rejected":
        line = f"{head} rejected" + (f": {view.status_message}" if view.status_message else "")
    else:
        raise ValueError(f"unknown change {change!r}")
    if view.tag:
        line += f" [{(tag_labels or {}).get(view.tag, view.tag)}]"
    return line


def stamp(moment: dt.datetime) -> str:
    """An event's received_at: ISO 8601 in IST, to the second."""
    return to_ist(moment).isoformat(timespec="seconds")


def order_event(
    view: OrderView,
    *,
    change: str,
    source: str,
    received_at: dt.datetime,
    tag_labels: Mapping[str, str] | None,
    previous_event_id: str,
) -> dict[str, Any]:
    """`previous_event_id` is Previous.event_id of the state the change was judged against ("" for a first sight). It
    has no default: left out, a state the order goes back to would get its earlier event's id and be dropped."""
    if change not in CHANGES:
        raise ValueError(f"unknown change {change!r}")
    if source not in ORDER_SOURCES:
        raise ValueError(f"unknown order event source {source!r}")
    return {
        "kind": "order",
        "event_id": digest("order", view.order_id, previous_event_id, *view.state()),
        "state_id": view.state_id(),
        "source": source,
        "received_at": stamp(received_at),
        "change": change,
        "text": text_for(view, change, tag_labels),
        "order": view.as_dict(),
    }


def token_fingerprint(access_token: str) -> str:
    """A stand-in for an access token that is safe to keep and compare (its SHA-256, in hex): the seed of the token's
    session events, and how the poll tells a new token from the one it saw. Never the token itself."""
    return hashlib.sha256(access_token.encode("utf-8", "replace")).hexdigest()


def session_event(name: str, *, seed: str, received_at: dt.datetime) -> dict[str, Any]:
    """`seed` makes the id: the same seed (e.g. a fingerprint of one token) gives the same event, stored once."""
    if name not in SESSION_TEXT:
        raise ValueError(f"unknown session event {name!r}")
    return {
        "kind": "session",
        "event_id": digest("session", name, seed),
        "source": "service",
        "received_at": stamp(received_at),
        "session": name,
        "text": SESSION_TEXT[name],
    }


def clean_error_text(text: str) -> str:
    """Text from a command (its stderr) made fit for a log, a terminal and other consumers: terminal control sequences
    and other unprintable characters dropped, whitespace and line breaks run into single spaces, links as "[link]".
    Nothing is clipped: clip after cleaning, so a clipped text never shows part of a link. Cleaning twice changes
    nothing. A secret without a scheme (a token in a path) is not recognised: commands must not print secrets."""
    text = _ESCAPE_RE.sub("", text)
    text = "".join(ch for ch in text if ch.isprintable() or ch.isspace())
    return _LINK_RE.sub("[link]", " ".join(text.split()))


def _clean_error(error: str | None) -> str | None:
    """A failed delivery's error, often a command's stderr: clean_error_text, clipped to MAX_ERROR_CHARS."""
    if not error:
        return None
    return clean_error_text(error)[:MAX_ERROR_CHARS].rstrip() or None


def router_event(
    name: str, *, consumer: str, about: Mapping[str, Any], error: str | None, seed: str, received_at: dt.datetime
) -> dict[str, Any]:
    about_text = str(about.get("text") or about.get("kind") or "an event")
    clean_error = _clean_error(error)
    if name == "delivery_delayed":
        text = f"Delivery to {consumer} is delayed and still retrying: {about_text}"
    elif name == "delivery_failed":
        text = f"Delivery to {consumer} failed: {about_text}" + (f" ({clean_error})" if clean_error else "")
    else:
        raise ValueError(f"unknown router event {name!r}")
    return {
        "kind": "router",
        "event_id": digest("router", name, consumer, str(about.get("event_id", "")), seed),
        "source": "router",
        "received_at": stamp(received_at),
        "router": name,
        "consumer": consumer,
        "about_seq": about.get("seq"),
        "about_event_id": about.get("event_id"),
        "error": clean_error,
        "text": text,
    }


def is_selftest_id(order_id: object) -> bool:
    """True for the id of a selftest postback: SELFTEST_PREFIX and at least one more character, in the order-id shape
    (ORDER_ID_RE). The postback route and selftest_event both decide with this, so they cannot drift apart."""
    return (
        isinstance(order_id, str)
        and len(order_id) > len(SELFTEST_PREFIX)
        and order_id.startswith(SELFTEST_PREFIX)
        and ORDER_ID_RE.fullmatch(order_id) is not None
    )


def selftest_event(order_id: str, *, received_at: dt.datetime) -> dict[str, Any]:
    if not is_selftest_id(order_id):
        raise ValueError("not a self-test order id")
    return {
        "kind": "selftest",
        "event_id": digest("selftest", order_id),
        "source": "selftest",
        "received_at": stamp(received_at),
        "order_id": order_id,
        "text": "Self-test postback received and verified: the postback path works.",
    }


def order_key(event: Mapping[str, Any]) -> str:
    """Deliveries with the same key go one at a time, in order: an order's events. Other events are independent."""
    if event.get("kind") == "order":
        return str(event["order"]["order_id"])
    return str(event["event_id"])
