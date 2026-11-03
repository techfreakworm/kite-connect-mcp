"""Exception hierarchy. Messages never contain credentials: the HTTP client scrubs every value it holds."""

from __future__ import annotations

from typing import Any


class KiteMCPError(Exception):
    """Base class for every error this package raises on purpose."""


class ConfigError(KiteMCPError):
    """The configuration (environment, files) is missing or invalid."""


class ToolInputError(KiteMCPError):
    """A tool was called with arguments that cannot work (caught before any request is sent)."""


class NotLoggedIn(KiteMCPError):
    """There is no usable access token: log in first (or wait for the external login to refresh the token file)."""


class SafetyRefusal(KiteMCPError):
    """The safety policy refused a write. `reasons` lists every violated rule."""

    def __init__(self, message: str, reasons: list[str] | None = None):
        super().__init__(message)
        self.reasons = list(reasons or [])


class ForbiddenCall(KiteMCPError):
    """A request outside the client's endpoint allow-list, or a write without a ticket from the safety pipeline.
    This is always a bug in this package, never something the caller did."""


class KiteAPIError(KiteMCPError):
    """Kite answered with an error. `error_type` is Kite's exception name (TokenException, InputException, ...)."""

    def __init__(self, message: str, *, status: int | None = None, error_type: str | None = None, data: Any = None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.error_type = error_type or "GeneralException"
        self.data = data

    def __str__(self) -> str:
        status = f" HTTP {self.status}" if self.status else ""
        return f"{self.error_type}{status}: {self.message}"


class TokenError(KiteAPIError):
    """403 / TokenException: the session expired, was logged out, or the token is invalid. Log in again."""


class InputError(KiteAPIError):
    """InputException: missing or bad parameters."""


class OrderError(KiteAPIError):
    """OrderException: order placement / modification / fetch failed."""


class MarginError(KiteAPIError):
    """MarginException: insufficient funds."""


class HoldingError(KiteAPIError):
    """HoldingException: insufficient holdings for a sell."""


class UserError(KiteAPIError):
    """UserException: account related error."""


class PermissionError_(KiteAPIError):
    """PermissionException: the app or account is not permitted to do this."""


class NetworkError(KiteAPIError):
    """NetworkException (Kite could not reach its OMS), or a transport error reaching Kite."""


class DataError(KiteAPIError):
    """DataException: Kite could not understand its own backend's answer."""


class GeneralError(KiteAPIError):
    """GeneralException or anything unclassified."""


class RateLimited(KiteAPIError):
    """HTTP 429: too many requests."""


class AuthorisationRequired(KiteAPIError):
    """HTTP 428: the sell needs a CDSL (TPIN) authorisation first. See the authorise_holdings tool."""


class ServerError(KiteAPIError):
    """HTTP 5xx from Kite."""


class OutcomeUnknown(KiteAPIError):
    """A write was sent but no answer came back (timeout, dropped connection). It may or may not have taken effect:
    check the order book before trying again. Writes are never retried automatically."""


ERROR_TYPES: dict[str, type[KiteAPIError]] = {
    "TokenException": TokenError,
    "InputException": InputError,
    "OrderException": OrderError,
    "MarginException": MarginError,
    "HoldingException": HoldingError,
    "UserException": UserError,
    "PermissionException": PermissionError_,
    "NetworkException": NetworkError,
    "DataException": DataError,
    "GeneralException": GeneralError,
}


def error_for(status: int | None, error_type: str | None, message: str, data: Any = None) -> KiteAPIError:
    """The exception class for a Kite error answer. 428 and 429 decide by status; otherwise Kite's error_type decides
    (a 403 can be a PermissionException, e.g. market data on the free Personal plan, which is not a dead session);
    a bare 403 without an error_type is a session problem."""
    if status == 428:
        cls: type[KiteAPIError] = AuthorisationRequired
    elif status == 429:
        cls = RateLimited
    elif error_type in ERROR_TYPES:
        cls = ERROR_TYPES[error_type]
    elif status == 403:
        cls = TokenError
    elif status is not None and status >= 500:
        cls = ServerError
    else:
        cls = GeneralError
    return cls(message, status=status, error_type=error_type or cls.__name__, data=data)
