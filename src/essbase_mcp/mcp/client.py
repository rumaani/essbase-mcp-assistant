"""MCP client: JSON-RPC 2.0 over Streamable HTTP (JSON or SSE responses).

Built to be shared by many request threads:
- one connection pool (httpx is thread-safe), sized by the caller
- at most `max_concurrent` calls in flight to Essbase; extra callers queue up to
  `queue_timeout` seconds, then get MCPBusy (the web layer turns that into HTTP 503)
- one MCP session, initialized once (single-flight); if the server forgets it (HTTP 404 on
  a request that carried a session id) it is re-initialized once and the call retried
- an expired/rejected token (HTTP 401) is refreshed once and the call retried
"""
from __future__ import annotations

import itertools
import json
import threading
import time
from collections import deque
from typing import Any

import httpx

from essbase_mcp.auth.providers import NoAuth, TokenProvider

PROTOCOL_VERSION = "2025-06-18"


class MCPError(RuntimeError):
    pass


class MCPBusy(MCPError):
    """Too many calls in flight; the caller waited queue_timeout seconds."""


class Metrics:
    """Small thread-safe counters for /api/metrics."""

    def __init__(self, window: int = 2000):
        self._lock = threading.Lock()
        self.counts: dict[str, int] = {}
        self.in_flight = 0
        self.peak_in_flight = 0
        self._lat: deque[float] = deque(maxlen=window)

    def inc(self, key: str, n: int = 1) -> None:
        with self._lock:
            self.counts[key] = self.counts.get(key, 0) + n

    def enter(self) -> None:
        with self._lock:
            self.in_flight += 1
            self.peak_in_flight = max(self.peak_in_flight, self.in_flight)

    def leave(self, seconds: float) -> None:
        with self._lock:
            self.in_flight -= 1
            self._lat.append(seconds)

    def snapshot(self) -> dict:
        with self._lock:
            lat = sorted(self._lat)
        pct = (lambda p: round(lat[min(len(lat) - 1, int(p * len(lat)))] * 1000, 1)) if lat else (
            lambda p: None)
        return {**self.counts, "in_flight": self.in_flight, "peak_in_flight": self.peak_in_flight,
                "call_ms_p50": pct(0.5), "call_ms_p95": pct(0.95), "call_ms_max": pct(0.999)}


class MCPClient:
    def __init__(self, url: str, profile: str = "viewer", auth: TokenProvider | None = None,
                 http: httpx.Client | None = None, max_concurrent: int = 20,
                 queue_timeout: float = 30.0, metrics: Metrics | None = None,
                 slots: threading.BoundedSemaphore | None = None):
        self.url = url
        self.profile = profile
        self.auth = auth or NoAuth()
        self.http = http or httpx.Client(timeout=60)
        self.session_id: str | None = None
        self.server_info: dict | None = None
        self._ids = itertools.count(1)
        self._init_lock = threading.Lock()
        # shared across all users' clients when passed in: one cap on calls to Essbase
        self._slots = slots or threading.BoundedSemaphore(max(1, max_concurrent))
        self.queue_timeout = queue_timeout
        self.metrics = metrics or Metrics()

    # -- transport ---------------------------------------------------------
    def _headers(self, token: str | None, session_id: str | None) -> dict[str, str]:
        h = {"Accept": "application/json, text/event-stream",
             "Content-Type": "application/json",
             "MCP-Protocol-Version": PROTOCOL_VERSION}
        if token:
            h["Authorization"] = f"Bearer {token}"
        if session_id:
            h["Mcp-Session-Id"] = session_id
        return h

    def _send(self, payload: dict, use_session: bool = True) -> tuple[httpx.Response, str | None]:
        """Returns the response and the session id it was sent with (needed to tell whether
        a 404 is about our session or one another thread already replaced)."""
        token = self.auth.get_token()
        sid = self.session_id if use_session else None
        resp = self.http.post(self.url, params={"profile": self.profile},
                              headers=self._headers(token, sid), json=payload)
        if resp.status_code == 401:  # token expired or revoked -> refresh once
            self.metrics.inc("auth_retries")
            self.auth.invalidate(token)
            token = self.auth.get_token()
            resp = self.http.post(self.url, params={"profile": self.profile},
                                  headers=self._headers(token, sid), json=payload)
        return resp, sid

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

    def _rpc(self, method: str, params: dict, use_session: bool = True) -> Any:
        payload = {"jsonrpc": "2.0", "id": next(self._ids), "method": method, "params": params}
        resp, sent_sid = self._send(payload, use_session)
        if resp.status_code == 404 and use_session and sent_sid:
            # server dropped the session we used: re-initialize once (single-flight) and retry
            self._reinitialize(stale=sent_sid)
            resp, _ = self._send(payload)
        resp.raise_for_status()
        msg = self._parse(resp)
        if "error" in msg:
            raise MCPError(f"{msg['error'].get('code')}: {msg['error'].get('message')}")
        return msg["result"], resp

    def request(self, method: str, params: dict | None = None) -> Any:
        if not self._slots.acquire(timeout=self.queue_timeout):
            self.metrics.inc("busy_rejections")
            raise MCPBusy(f"Essbase is busy (more than {self.queue_timeout:.0f}s in queue)")
        started = time.perf_counter()
        self.metrics.enter()
        try:
            self.ensure_initialized()
            result, _ = self._rpc(method, params or {})
            self.metrics.inc("tool_calls" if method == "tools/call" else "rpc_calls")
            return result
        except Exception:
            self.metrics.inc("errors")
            raise
        finally:
            self.metrics.leave(time.perf_counter() - started)
            self._slots.release()

    # -- session -----------------------------------------------------------
    def _do_initialize(self) -> dict:
        result, resp = self._rpc("initialize", {
            "protocolVersion": PROTOCOL_VERSION, "capabilities": {},
            "clientInfo": {"name": "essbase-mcp-assistant", "version": "0.2.0"},
        }, use_session=False)
        self.session_id = resp.headers.get("Mcp-Session-Id")
        note = {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}
        self._send(note)  # response ignored by design (202 Accepted)
        self.server_info = result.get("serverInfo", {})
        self.metrics.inc("initializations")
        return result

    def ensure_initialized(self) -> None:
        if self.server_info is not None:
            return
        with self._init_lock:
            if self.server_info is None:
                self._do_initialize()

    def _reinitialize(self, stale: str | None) -> None:
        with self._init_lock:
            if self.session_id == stale:  # nobody else re-initialized yet
                self.metrics.inc("session_reinits")
                self.server_info = None
                self._do_initialize()

    def initialize(self) -> dict:
        """(Re)connect explicitly; used by 'Test connection'."""
        with self._init_lock:
            self.server_info = None
            return self._do_initialize()

    # -- MCP methods -------------------------------------------------------
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
