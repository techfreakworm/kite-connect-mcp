"""Tools: the user's profile and funds/margins (https://kite.trade/docs/connect/v3/user/)."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field

from ._base import Registrar


def register(reg: Registrar) -> None:
    rt = reg.rt

    @reg.tool("get_profile", title="User profile", toolset="account", kind="read")
    async def get_profile() -> Any:
        """The logged-in user's Kite profile: user id, name, email, broker, the exchanges, products and order types
        enabled for the account, and whether it is a demat consent / POA account (`meta.demat_consent`)."""
        return await rt.client.get("/user/profile")

    @reg.tool("get_margins", title="Funds and margins", toolset="account", kind="read")
    async def get_margins(
        segment: Annotated[
            Literal["equity", "commodity"] | None,
            Field(description="equity or commodity; omit for both"),
        ] = None,
    ) -> Any:
        """Funds and margins: `available` (cash, opening balance, live balance, collateral, intraday payin) and
        `utilised` (debits, exposure, span, option premium, holding sales, turnover, ...) per segment, plus `net`
        (what is left to trade with). Amounts in INR."""
        path = f"/user/margins/{segment}" if segment else "/user/margins"
        return await rt.client.get(path)
