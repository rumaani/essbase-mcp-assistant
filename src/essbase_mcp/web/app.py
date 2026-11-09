"""Web UI backend (Backend-for-Frontend).

The browser only talks to /api/*. OAuth client secrets and tokens stay on the server.
In --demo mode the mock identity domain + Essbase MCP run in-process (no network, no ports).
"""
from __future__ import annotations

import time
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from essbase_mcp.auth import providers
from essbase_mcp.config import Settings
from essbase_mcp.context import builder, store
from essbase_mcp.llm import base as llm
from essbase_mcp.mcp.client import MCPClient, MCPError
from essbase_mcp.planner.planner import Planner

STATIC = Path(__file__).parent / "static"


class Session:
    """Holds the one connection the UI works with."""

    def __init__(self, settings: Settings, http: httpx.Client | None = None, demo: bool = False):
        self.settings, self.demo = settings, demo
        self.auth = providers.from_settings(settings, http)
        self.client = MCPClient(settings.mcp_url, settings.profile, self.auth, http)
        self.server_info: dict | None = None
        self.last_error: str | None = None
        self.ctx: dict | None = None
        path = Path(settings.context_file)
        if path.exists():
            self.ctx = store.load(path)

    def connect(self) -> dict:
        try:
            self.server_info = self.client.initialize().get("serverInfo", {})
            tools = self.client.list_tools()
            self.last_error = None
            return {"ok": True, "server": self.server_info, "tool_count": len(tools)}
        except (httpx.HTTPError, MCPError, KeyError) as exc:
            self.server_info, self.last_error = None, f"{type(exc).__name__}: {exc}"
            return {"ok": False, "error": self.last_error}

    def ensure_connected(self) -> None:
        if not self.server_info and not self.connect()["ok"]:
            raise HTTPException(502, self.last_error)


class BuildRequest(BaseModel):
    application: str
    database: str


class GlossaryRequest(BaseModel):
    glossary: dict[str, str]


class AskRequest(BaseModel):
    question: str
    execute: bool = True


def create_app(session: Session) -> FastAPI:
    app = FastAPI(title="Essbase MCP Assistant")
    s = session.settings

    @app.get("/api/status")
    def status():
        token = getattr(session.auth, "_expires_at", None)
        return {
            "demo": session.demo,
            "mcp_url": s.mcp_url, "profile": s.profile, "auth_mode": s.auth_mode,
            "token_url": s.token_url if s.auth_mode == "client_credentials" else None,
            "client_id": s.client_id or None,
            "token_expires_in": int(token - time.time()) if token else None,
            "llm_provider": s.llm_provider,
            "connected": session.server_info is not None,
            "server": session.server_info, "error": session.last_error,
            "context_file": s.context_file, "has_context": session.ctx is not None,
        }

    @app.post("/api/connect")
    def connect():
        return session.connect()

    @app.get("/api/context")
    def get_context():
        if not session.ctx:
            raise HTTPException(404, "No context file yet. Build one first.")
        return session.ctx

    @app.post("/api/context/build")
    def build(req: BuildRequest):
        session.ensure_connected()
        existing = session.ctx if session.ctx and session.ctx.get("cube") == {
            "application": req.application, "database": req.database} else None
        try:
            ctx = store.merge(builder.build(session.client, req.application, req.database),
                              existing)
        except (MCPError, KeyError, TypeError) as exc:
            raise HTTPException(502, f"Context build failed: {exc}") from exc
        store.save(ctx, s.context_file)
        session.ctx = ctx
        return ctx

    @app.put("/api/context/glossary")
    def save_glossary(req: GlossaryRequest):
        if not session.ctx:
            raise HTTPException(404, "No context file yet.")
        known = {m["name"] for d in session.ctx["dimensions"] for m in d["members"]}
        bad = sorted({v for v in req.glossary.values() if v not in known})
        if bad:
            raise HTTPException(422, f"Not members of the cube: {', '.join(bad)}")
        session.ctx["glossary"] = {k.strip(): v for k, v in req.glossary.items() if k.strip()}
        store.save(session.ctx, s.context_file)
        return session.ctx["glossary"]

    @app.post("/api/ask")
    def ask(req: AskRequest):
        if not session.ctx:
            raise HTTPException(409, "Build the context file first (Context tab).")
        started = time.perf_counter()
        plan = Planner(session.ctx, llm.from_settings(s)).plan(req.question)
        out = {"plan": plan.to_dict(), "ok": plan.ok, "result": None, "error": None}
        if plan.ok and req.execute:
            if plan.intent == "calculation":
                out["error"] = "Calculations are not run from the UI. Use the CLI with --yes."
            else:
                session.ensure_connected()
                try:
                    res = session.client.call_tool(plan.tool, plan.arguments)
                    payload = MCPClient.tool_payload(res)
                    if res.get("isError"):
                        out["error"] = payload if isinstance(payload, str) else str(payload)
                    else:
                        out["result"] = payload
                except (httpx.HTTPError, MCPError) as exc:
                    out["error"] = f"{type(exc).__name__}: {exc}"
        out["elapsed_ms"] = round((time.perf_counter() - started) * 1000)
        return out

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    return app


def demo_session(settings: Settings) -> Session:
    """Point everything at the in-process mock (no network, no ports)."""
    from fastapi.testclient import TestClient

    from essbase_mcp.mock import server as mock

    base = "http://mock.local"
    settings.mcp_url = f"{base}/essbase/rest/v1/ess-mcp"
    settings.token_url = f"{base}/oauth2/v1/token"
    settings.auth_mode = "client_credentials"
    settings.client_id, settings.client_secret = mock.CLIENT_ID, mock.CLIENT_SECRET
    return Session(settings, TestClient(mock.app, base_url=base), demo=True)
