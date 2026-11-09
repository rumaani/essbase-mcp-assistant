"""Token providers. The MCP client only asks for `get_token()`; how it is obtained is pluggable.

- client_credentials: OAuth 2.1 client-credentials grant against the OCI IAM identity domain
  token endpoint (or the local mock). Tokens are cached until shortly before expiry.
- token: a bearer token pasted in from elsewhere (handy for testing against a real server).
- none: no Authorization header (mock without auth).
"""
from __future__ import annotations

import time
from typing import Protocol

import httpx

from essbase_mcp.config import Settings


class TokenProvider(Protocol):
    def get_token(self) -> str | None: ...
    def invalidate(self) -> None: ...


class NoAuth:
    def get_token(self) -> None:
        return None

    def invalidate(self) -> None:
        pass


class StaticToken:
    def __init__(self, token: str):
        self._token = token

    def get_token(self) -> str:
        return self._token

    def invalidate(self) -> None:
        pass


class ClientCredentials:
    def __init__(self, token_url: str, client_id: str, client_secret: str, scope: str,
                 http: httpx.Client | None = None):
        self.token_url, self.client_id, self.client_secret, self.scope = (
            token_url, client_id, client_secret, scope)
        self._http = http or httpx.Client(timeout=30)
        self._token: str | None = None
        self._expires_at = 0.0

    def get_token(self) -> str:
        if self._token and time.time() < self._expires_at - 60:
            return self._token
        resp = self._http.post(self.token_url, data={
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret,  # client_secret_post
            "scope": self.scope,
        })
        resp.raise_for_status()
        body = resp.json()
        self._token = body["access_token"]
        self._expires_at = time.time() + int(body.get("expires_in", 3600))
        return self._token

    def invalidate(self) -> None:
        self._token = None


def from_settings(s: Settings, http: httpx.Client | None = None) -> TokenProvider:
    if s.auth_mode == "client_credentials":
        return ClientCredentials(s.token_url, s.client_id, s.client_secret, s.scope, http)
    if s.auth_mode == "token":
        return StaticToken(s.access_token)
    return NoAuth()
