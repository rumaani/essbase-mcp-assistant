"""Token providers. The MCP client only asks for `get_token()`; how it is obtained is pluggable.

- client_credentials: OAuth 2.1 client-credentials grant against the OCI IAM identity domain
  token endpoint (or the local mock). One token is shared by all requests and refreshed
  shortly before expiry.
- token: a bearer token pasted in from elsewhere (handy for testing against a real server).
- none: no Authorization header (mock without auth).

Concurrency: many request threads can call get_token() at once. Refresh is single-flight:
one thread fetches a new token while the others wait for it, so 100 simultaneous requests
with an expired token cause one token request, not 100. invalidate(token) only discards the
token that actually failed, so a late 401 can't throw away a token another thread just fetched.
"""
from __future__ import annotations

import threading
import time
from typing import Protocol

import httpx

from essbase_mcp.config import Settings

REFRESH_MARGIN_S = 60


class TokenProvider(Protocol):
    def get_token(self) -> str | None: ...
    def invalidate(self, token: str | None = None) -> None: ...


class NoAuth:
    fetches = 0

    def get_token(self) -> None:
        return None

    def invalidate(self, token: str | None = None) -> None:
        pass


class StaticToken:
    fetches = 0

    def __init__(self, token: str):
        self._token = token

    def get_token(self) -> str:
        return self._token

    def invalidate(self, token: str | None = None) -> None:
        pass


class ClientCredentials:
    def __init__(self, token_url: str, client_id: str, client_secret: str, scope: str,
                 http: httpx.Client | None = None):
        self.token_url, self.client_id, self.client_secret, self.scope = (
            token_url, client_id, client_secret, scope)
        self._http = http or httpx.Client(timeout=30)
        self._lock = threading.Lock()
        self._token: str | None = None
        self._expires_at = 0.0
        self.fetches = 0  # number of token requests made (exposed in /api/metrics)

    def _valid(self) -> bool:
        return bool(self._token) and time.time() < self._expires_at - REFRESH_MARGIN_S

    def get_token(self) -> str:
        if self._valid():  # fast path, no lock
            return self._token  # type: ignore[return-value]
        with self._lock:  # single-flight refresh
            if self._valid():  # another thread refreshed while we waited
                return self._token  # type: ignore[return-value]
            resp = self._http.post(self.token_url, data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,  # client_secret_post
                "scope": self.scope,
            })
            self.fetches += 1
            resp.raise_for_status()
            body = resp.json()
            self._token = body["access_token"]
            self._expires_at = time.time() + int(body.get("expires_in", 3600))
            return self._token

    @property
    def expires_at(self) -> float | None:
        return self._expires_at if self._token else None

    def invalidate(self, token: str | None = None) -> None:
        with self._lock:
            if token is None or token == self._token:
                self._token = None


def from_settings(s: Settings, http: httpx.Client | None = None) -> TokenProvider:
    if s.auth_mode == "client_credentials":
        return ClientCredentials(s.token_url, s.client_id, s.client_secret, s.scope, http)
    if s.auth_mode == "token":
        return StaticToken(s.access_token)
    return NoAuth()
