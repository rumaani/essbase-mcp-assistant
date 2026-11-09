"""Turn Settings into a working MCP connection (built-in mock or remote server)."""
from __future__ import annotations

import dataclasses

import httpx

from essbase_mcp.auth import providers
from essbase_mcp.config import Settings
from essbase_mcp.mcp.client import MCPClient, Metrics

MOCK_BASE = "http://mock.local"


def effective(s: Settings) -> Settings:
    """In mock mode, point at the in-process mock, keeping the user's remote values untouched."""
    if s.mode != "mock":
        return s
    from essbase_mcp.mock import server as mock

    return dataclasses.replace(
        s, mcp_url=f"{MOCK_BASE}/essbase/rest/v1/ess-mcp",
        token_url=f"{MOCK_BASE}/oauth2/v1/token", auth_mode="client_credentials",
        client_id=mock.CLIENT_ID, client_secret=mock.CLIENT_SECRET,
        authorize_url="/mock/oauth2/v1/authorize")  # served by this app in mock mode


def authorize_url(s: Settings) -> str:
    eff = effective(s)
    return eff.authorize_url or eff.token_url.replace("/oauth2/v1/token", "/oauth2/v1/authorize")


def http_client(s: Settings) -> httpx.Client:
    if s.mode == "mock":
        from fastapi.testclient import TestClient

        from essbase_mcp.mock import server as mock

        return TestClient(mock.app, base_url=MOCK_BASE)
    # pool sized to the concurrency cap, so queued work waits in our queue, not in httpx
    limits = httpx.Limits(max_connections=s.max_concurrent_calls + 5,
                          max_keepalive_connections=s.max_concurrent_calls)
    return httpx.Client(timeout=s.request_timeout_s, verify=s.verify_tls, limits=limits)


def connect(s: Settings, metrics: Metrics | None = None, http: httpx.Client | None = None,
            slots=None, auth=None) -> MCPClient:
    """A client for the shared identity, or (auth=...) for one signed-in user. Pass the same
    http pool and slots to every client so all users share one pool and one call cap."""
    eff = effective(s)
    http = http or http_client(s)
    return MCPClient(eff.mcp_url, eff.profile, auth or providers.from_settings(eff, http), http,
                     max_concurrent=s.max_concurrent_calls, queue_timeout=s.queue_timeout_s,
                     metrics=metrics, slots=slots)
