"""Per-user sign-in: OAuth authorization code + PKCE against the identity domain.

Each person who signs in gets a server-side session:
- the browser holds only a random session id in an HttpOnly cookie (no tokens in the browser)
- the session holds that user's access + refresh token and their own MCP client
- every MCP client shares the app's connection pool and the cap on parallel Essbase calls,
  so 100 signed-in users still can't send more than max_concurrent_calls at once
- tokens refresh single-flight per user; if the refresh token is rejected, the user is asked
  to sign in again (SignInRequired -> HTTP 401)

Sessions are in memory: a restart or a settings change signs everyone out.
"""
from __future__ import annotations

import base64
import json
import secrets
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import urlencode

import httpx

from essbase_mcp.auth.pkce import challenge_s256, new_verifier
from essbase_mcp.auth.providers import REFRESH_MARGIN_S

PENDING_TTL_S = 600


class SignInRequired(Exception):
    pass


class UserTokens:
    """TokenProvider for one signed-in user."""

    def __init__(self, token_url: str, client_id: str, client_secret: str, http: httpx.Client,
                 body: dict):
        self.token_url, self.client_id, self.client_secret, self._http = (
            token_url, client_id, client_secret, http)
        self._lock = threading.Lock()
        self.fetches = 0
        self._apply(body)

    def _apply(self, body: dict) -> None:
        self._token = body["access_token"]
        self._expires_at = time.time() + int(body.get("expires_in", 3600))
        self._refresh = body.get("refresh_token") or getattr(self, "_refresh", None)

    def _valid(self) -> bool:
        lifetime_left = self._expires_at - time.time()
        return bool(self._token) and lifetime_left > min(REFRESH_MARGIN_S, lifetime_left / 2 + 0.5)

    @property
    def expires_at(self) -> float:
        return self._expires_at

    def get_token(self) -> str:
        if self._valid():
            return self._token
        with self._lock:
            if self._valid():
                return self._token
            if not self._refresh:
                raise SignInRequired("Your sign-in expired. Please sign in again.")
            resp = self._http.post(self.token_url, data={
                "grant_type": "refresh_token", "refresh_token": self._refresh,
                "client_id": self.client_id, "client_secret": self.client_secret})
            self.fetches += 1
            if resp.status_code != 200:
                self._token, self._refresh = None, None
                raise SignInRequired("Your sign-in expired. Please sign in again.")
            self._apply(resp.json())
            return self._token

    def invalidate(self, token: str | None = None) -> None:
        with self._lock:
            if token is None or token == self._token:
                self._expires_at = 0


@dataclass
class UserSession:
    sid: str
    subject: str
    name: str
    tokens: UserTokens
    client: object  # MCPClient
    created: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    tools: list | None = None


def claims_of(id_token: str | None) -> dict:
    """Read display claims from the ID token. Used for showing the name only: authorization
    is enforced by Essbase on the access token, never by these unverified claims."""
    if not id_token or id_token.count(".") < 2:
        return {}
    try:
        part = id_token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except (ValueError, json.JSONDecodeError):
        return {}


class SessionStore:
    def __init__(self, idle_hours: int):
        self.idle_s = idle_hours * 3600
        self._lock = threading.Lock()
        self._sessions: dict[str, UserSession] = {}
        self._pending: dict[str, tuple[str, float, str]] = {}  # state -> (verifier, t, nonce)

    # --- sign-in round trip ---------------------------------------------------------------
    def begin(self, authorize_url: str, client_id: str, redirect_uri: str, scope: str) -> str:
        state, nonce, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(16), new_verifier()
        with self._lock:
            now = time.time()
            self._pending = {k: v for k, v in self._pending.items() if now - v[1] < PENDING_TTL_S}
            self._pending[state] = (verifier, now, nonce)
        sep = "&" if "?" in authorize_url else "?"
        return authorize_url + sep + urlencode({
            "client_id": client_id, "response_type": "code", "redirect_uri": redirect_uri,
            "scope": scope, "state": state, "nonce": nonce,
            "code_challenge": challenge_s256(verifier), "code_challenge_method": "S256"})

    def take_pending(self, state: str) -> tuple[str, str] | None:
        with self._lock:
            entry = self._pending.pop(state, None)  # single use
        if not entry or time.time() - entry[1] > PENDING_TTL_S:
            return None
        return entry[0], entry[2]

    # --- sessions -------------------------------------------------------------------------
    def add(self, session: UserSession) -> None:
        with self._lock:
            self._sessions[session.sid] = session

    def get(self, sid: str | None) -> UserSession | None:
        if not sid:
            return None
        with self._lock:
            s = self._sessions.get(sid)
            if s and time.time() - s.last_seen > self.idle_s:
                del self._sessions[sid]
                return None
            if s:
                s.last_seen = time.time()
            return s

    def remove(self, sid: str | None) -> None:
        with self._lock:
            self._sessions.pop(sid or "", None)

    def clear(self) -> None:
        with self._lock:
            self._sessions.clear()
            self._pending.clear()

    def count(self) -> int:
        with self._lock:
            return len(self._sessions)
