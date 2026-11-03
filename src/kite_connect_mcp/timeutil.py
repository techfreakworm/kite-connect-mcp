"""Indian Standard Time helpers. IST has no daylight saving, so a fixed offset is exact (no tzdata needed)."""

from __future__ import annotations

import datetime as dt

IST = dt.timezone(dt.timedelta(hours=5, minutes=30), "IST")
TOKEN_RESET = dt.time(6, 0)  # Kite access tokens expire at 06:00 IST the next day (regulatory requirement)


def now_ist() -> dt.datetime:
    return dt.datetime.now(IST)


def to_ist(moment: dt.datetime) -> dt.datetime:
    """`moment` in IST. A naive datetime is taken to be IST already."""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=IST)
    return moment.astimezone(IST)


def ist_date(moment: dt.datetime | None = None) -> dt.date:
    return to_ist(moment or now_ist()).date()


def next_token_reset(moment: dt.datetime) -> dt.datetime:
    """The first 06:00 IST strictly after `moment`: when a token issued at `moment` stops working."""
    local = to_ist(moment)
    reset = dt.datetime.combine(local.date(), TOKEN_RESET, tzinfo=IST)
    return reset if reset > local else reset + dt.timedelta(days=1)


def parse_kite_datetime(value: str) -> dt.datetime:
    """Kite timestamps are 'YYYY-MM-DD HH:MM:SS' in IST (or ISO 8601 with an offset)."""
    text = value.strip().replace("T", " ")
    if len(text) >= 19 and text[19:20] in ("+", "-"):
        return dt.datetime.fromisoformat(text)
    return dt.datetime.fromisoformat(text[:19]).replace(tzinfo=IST)


def format_kite_datetime(moment: dt.datetime | dt.date) -> str:
    """'YYYY-MM-DD HH:MM:SS' in IST, the form Kite's historical API takes. A date means its midnight."""
    if not isinstance(moment, dt.datetime):
        return f"{moment.isoformat()} 00:00:00"
    return to_ist(moment).strftime("%Y-%m-%d %H:%M:%S")
