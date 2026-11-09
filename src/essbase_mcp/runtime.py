"""Turn Settings into a working MCP connection (built-in mock or remote server)."""
from __future__ import annotations

import dataclasses

import httpx

from essbase_mcp.auth import providers
from essbase_mcp.config import Settings
from essbase_mcp.mcp.client import MCPClient

MOCK_BASE = "http://mock.local"


def effective(s: Settings) -> Settings:
    """In mock mode, point at the in-process mock, keeping the user's remote values untouched."""
    if s.mode != "mock":
        return s
    from essbase_mcp.mock import server as mock

    return dataclasses.replace(
        s, mcp_url=f"{MOCK_BASE}/essbase/rest/v1/ess-mcp",
        token_url=f"{MOCK_BASE}/oauth2/v1/token", auth_mode="client_credentials",
        client_id=mock.CLIENT_ID, client_secret=mock.CLIENT_SECRET)


def http_client(s: Settings) -> httpx.Client:
    if s.mode == "mock":
        from fastapi.testclient import TestClient

        from essbase_mcp.mock import server as mock

        return TestClient(mock.app, base_url=MOCK_BASE)
    return httpx.Client(timeout=60, verify=s.verify_tls)


def connect(s: Settings) -> MCPClient:
    eff = effective(s)
    http = http_client(s)
    return MCPClient(eff.mcp_url, eff.profile, providers.from_settings(eff, http), http)
