"""Helpers for the login tests (test_login.py, test_session_tools.py): a fake Kite session endpoint and small URL
helpers. Every value here is fake."""

from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx2

from conftest import API_KEY, API_SECRET, FakeKite
from kite_connect_mcp.client.http import checksum

REQUEST_TOKEN = "TestRequestToken00000000000001"
NEW_TOKEN = "testnewaccesstoken000000000000002"
USER_ID = "AB1234"
PROFILE_FIELDS = {  # what Kite's exchange answer carries besides the tokens: none of it may be kept or logged
    "user_name": "Test Person",
    "user_shortname": "Test",
    "email": "person@example.invalid",
    "public_token": "testpublictoken000000000003",
    "enctoken": "testenctoken000000000000000004",
    "refresh_token": "",
    "login_time": "2026-11-04 10:00:00",
}
FORBIDDEN_IN_LOGS = ("Test Person", "person@example.invalid", "testpublictoken", "testenctoken")


class KiteSession:
    """Kite's /session/token for the tests. A request token works once, like the real one. Every exchange (the form
    that was posted) and every logout (the query string) is recorded."""

    def __init__(
        self,
        fake: FakeKite,
        *,
        user_id: str | None = USER_ID,
        access_token: str = NEW_TOKEN,
        request_token: str = REQUEST_TOKEN,
        delay: float = 0.0,
    ):
        self.user_id = user_id
        self.access_token = access_token
        self.request_token = request_token
        self.delay = delay
        self.spent = False
        self.fail_next = False
        self.logout_error: tuple[int, str] | None = None  # (HTTP status, Kite error_type) for DELETE
        self.exchanges: list[dict[str, str]] = []
        self.logouts: list[dict[str, str]] = []
        fake.on("POST", "/session/token", handler=self._exchange)
        fake.on("DELETE", "/session/token", handler=self._logout)

    async def _exchange(self, request: httpx2.Request) -> httpx2.Response:
        if self.delay:
            await asyncio.sleep(self.delay)
        form = dict(FakeKite.form(request))
        self.exchanges.append(form)
        good = (
            not self.spent
            and not self.fail_next
            and form.get("api_key") == API_KEY
            and form.get("request_token") == self.request_token
            and form.get("checksum") == checksum(API_KEY, self.request_token, API_SECRET)
        )
        self.fail_next = False
        if not good:
            return httpx2.Response(
                403,
                json={
                    "status": "error",
                    "message": "Token is invalid or has expired.",
                    "error_type": "TokenException",
                    "data": None,
                },
            )
        self.spent = True
        data: dict[str, Any] = {**PROFILE_FIELDS, "access_token": self.access_token, "api_key": API_KEY}
        if self.user_id is not None:
            data["user_id"] = self.user_id
        return httpx2.Response(200, json={"status": "success", "data": data})

    def _logout(self, request: httpx2.Request) -> httpx2.Response:
        self.logouts.append(dict(request.url.params))
        if self.logout_error is not None:
            status, error_type = self.logout_error
            body = {"status": "error", "message": "nope", "error_type": error_type, "data": None}
            return httpx2.Response(status, json=body)
        return httpx2.Response(200, json={"status": "success", "data": True})


def state_of(url: str) -> str:
    """The state carried in a login link's redirect_params."""
    query = parse_qs(urlsplit(url).query)
    return parse_qs(query["redirect_params"][0])["state"][0]


def callback_params(state: str, request_token: str = REQUEST_TOKEN, **extra: str) -> dict[str, str]:
    """The query Kite's redirect carries: request_token, action=login, status=success and our redirect_params."""
    return {
        "action": "login",
        "type": "login",
        "status": "success",
        "request_token": request_token,
        "state": state,
        **extra,
    }
