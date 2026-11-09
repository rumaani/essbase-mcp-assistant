"""Minimal MCP client: JSON-RPC 2.0 over Streamable HTTP (JSON or SSE responses)."""
from __future__ import annotations

import itertools
import json
from typing import Any

import httpx

from essbase_mcp.auth.providers import NoAuth, TokenProvider

PROTOCOL_VERSION = "2025-06-18"


class MCPError(RuntimeError):
    pass


class MCPClient:
    def __init__(self, url: str, profile: str = "viewer", auth: TokenProvider | None = None,
                 http: httpx.Client | None = None):
        self.url = url
        self.profile = profile
        self.auth = auth or NoAuth()
        self.http = http or httpx.Client(timeout=60)
        self.session_id: str | None = None
        self._ids = itertools.count(1)

    # -- transport ---------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        h = {"Accept": "application/json, text/event-stream",
             "Content-Type": "application/json",
             "MCP-Protocol-Version": PROTOCOL_VERSION}
        token = self.auth.get_token()
        if token:
            h["Authorization"] = f"Bearer {token}"
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        return h

    def _post(self, payload: dict, retry: bool = True) -> httpx.Response:
        resp = self.http.post(self.url, params={"profile": self.profile},
                              headers=self._headers(), json=payload)
        if resp.status_code == 401 and retry:  # expired token -> refresh once
            self.auth.invalidate()
            return self._post(payload, retry=False)
        resp.raise_for_status()
        if sid := resp.headers.get("Mcp-Session-Id"):
            self.session_id = sid
        return resp

    @staticmethod
    def _parse(resp: httpx.Response) -> dict:
        if resp.headers.get("content-type", "").startswith("text/event-stream"):
            for line in resp.text.splitlines():
                if line.startswith("data:"):
                    msg = json.loads(line[5:].strip())
                    if "result" in msg or "error" in msg:
                        return msg
            raise MCPError("SSE stream ended without a result")
        return resp.json()

    def request(self, method: str, params: dict | None = None) -> Any:
        msg = self._parse(self._post({"jsonrpc": "2.0", "id": next(self._ids),
                                      "method": method, "params": params or {}}))
        if "error" in msg:
            raise MCPError(f"{msg['error'].get('code')}: {msg['error'].get('message')}")
        return msg["result"]

    def notify(self, method: str, params: dict | None = None) -> None:
        self._post({"jsonrpc": "2.0", "method": method, "params": params or {}})

    # -- MCP methods -------------------------------------------------------
    def initialize(self) -> dict:
        result = self.request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "essbase-mcp-assistant", "version": "0.1.0"},
        })
        self.notify("notifications/initialized")
        return result

    def list_tools(self) -> list[dict]:
        tools, cursor = [], None
        while True:
            res = self.request("tools/list", {"cursor": cursor} if cursor else {})
            tools += res.get("tools", [])
            cursor = res.get("nextCursor")
            if not cursor:
                return tools

    def call_tool(self, name: str, arguments: dict) -> dict:
        return self.request("tools/call", {"name": name, "arguments": arguments})

    @staticmethod
    def tool_payload(result: dict) -> Any:
        """Return structuredContent if present, else parse the first text block as JSON."""
        if "structuredContent" in result:
            return result["structuredContent"]
        for block in result.get("content", []):
            if block.get("type") == "text":
                try:
                    return json.loads(block["text"])
                except json.JSONDecodeError:
                    return block["text"]
        return None
