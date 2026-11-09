"""Web UI backend (Backend-for-Frontend).

The browser only talks to /api/*. OAuth client secrets and tokens stay on the server, and
secrets saved from the Admin tab are encrypted at rest (see settings_store).

Security defaults for a reference deployment:
- binds to 127.0.0.1 unless --host says otherwise
- state-changing requests need the X-Requested-With header (blocks cross-site form posts)
- /api/admin/* requires EMCP_ADMIN_PASSWORD when that variable is set
"""
from __future__ import annotations

import hmac
import os
import threading
import time
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from essbase_mcp import runtime, settings_store
from essbase_mcp.config import Settings
from essbase_mcp.context import builder, store
from essbase_mcp.llm import base as llm
from essbase_mcp.mcp.client import MCPClient, MCPError
from essbase_mcp.planner.planner import Planner

STATIC = Path(__file__).parent / "static"
CSRF_HEADER, CSRF_VALUE = "X-Requested-With", "essbase-mcp-assistant"
CONNECT_ERRORS = (httpx.HTTPError, MCPError, KeyError, ValueError)


def describe_error(exc: Exception) -> str:
    """Readable connection errors: say which step and which host failed."""
    if isinstance(exc, httpx.HTTPStatusError):
        url = exc.request.url
        step = "Token request" if "token" in url.path else "MCP request"
        hint = {400: "check scope / grant type", 401: "check client ID and secret or token",
                403: "the identity has no access", 404: "check the URL path"}.get(
            exc.response.status_code, "")
        return f"{step} to {url.host} failed: HTTP {exc.response.status_code}" + (
            f" ({hint})" if hint else "")
    if isinstance(exc, httpx.RequestError):
        return f"Could not reach {exc.request.url.host}: {type(exc).__name__} {exc}".strip()
    return f"{type(exc).__name__}: {exc}"


class Session:
    """The one connection + active context the UI works with. Rebuilt when settings change."""

    def __init__(self, settings: Settings):
        self.lock = threading.RLock()
        self.configure(settings)

    def configure(self, settings: Settings) -> None:
        with self.lock:
            self.settings = settings
            self.client = runtime.connect(settings)
            self.server_info: dict | None = None
            self.last_error: str | None = None
            self.tools: list[dict] = []
            self.load_active_context()

    def load_active_context(self) -> None:
        path = Path(self.settings.context_file)
        self.ctx = store.load(path) if path.exists() else None

    def connect(self) -> dict:
        with self.lock:
            try:
                self.server_info = self.client.initialize().get("serverInfo", {})
                self.tools = self.client.list_tools()
                self.last_error = None
                return {"ok": True, "server": self.server_info, "tool_count": len(self.tools)}
            except CONNECT_ERRORS as exc:
                self.server_info, self.last_error = None, describe_error(exc)
                return {"ok": False, "error": self.last_error}

    def ensure_connected(self) -> None:
        if not self.server_info and not self.connect()["ok"]:
            raise HTTPException(502, self.last_error)

    def call(self, intent: str, **canonical) -> Any:
        """Call the tool mapped to an intent, using the live catalog (no context needed)."""
        from essbase_mcp.context.args import fit

        self.ensure_connected()
        mapping = (self.ctx or {}).get("intents") or builder.suggest_intents(self.tools)
        by_name = {t["name"]: t for t in self.tools}
        tool = by_name.get(mapping.get(intent, ""))
        if not tool:
            raise HTTPException(501, f"The server has no tool for '{intent}'.")
        res = self.client.call_tool(tool["name"], fit(tool, canonical))
        payload = MCPClient.tool_payload(res)
        if res.get("isError"):
            raise HTTPException(502, str(payload))
        return payload


# ----- request models ------------------------------------------------------------------
class CubeRequest(BaseModel):
    application: str
    database: str


class ActivateRequest(BaseModel):
    cube: str


class ContextPatch(BaseModel):
    glossary: dict[str, str | list[str]] | None = None
    defaults: dict[str, str] | None = None
    intents: dict[str, str] | None = None
    notes: str | None = None
    examples: list[dict] | None = None


class AskRequest(BaseModel):
    question: str
    execute: bool = True


def _list(payload: Any, key: str) -> list[str]:
    items = payload.get(key, []) if isinstance(payload, dict) else payload or []
    return [i if isinstance(i, str) else i.get("name", "") for i in items]


def create_app(session: Session) -> FastAPI:
    app = FastAPI(title="Essbase MCP Assistant")
    admin_password = os.environ.get("EMCP_ADMIN_PASSWORD", "")

    @app.middleware("http")
    async def guard(request: Request, call_next):
        state_changing = request.method not in ("GET", "HEAD")
        if (request.url.path.startswith("/api/") and state_changing
                and request.headers.get(CSRF_HEADER) != CSRF_VALUE):
            return JSONResponse({"detail": "Missing CSRF header"}, status_code=403)
        if request.url.path.startswith("/api/admin/") and admin_password:
            given = request.headers.get("X-Admin-Password", "")
            if not hmac.compare_digest(given.encode(), admin_password.encode()):
                return JSONResponse({"detail": "Admin password required"}, status_code=401)
        return await call_next(request)

    # ----- status ----------------------------------------------------------------------
    @app.get("/api/status")
    def status():
        s, eff = session.settings, runtime.effective(session.settings)
        expires = getattr(session.client.auth, "_expires_at", None)
        return {
            "mode": s.mode, "mcp_url": eff.mcp_url, "profile": s.profile,
            "auth_mode": eff.auth_mode, "client_id": eff.client_id or None,
            "token_url": eff.token_url if eff.auth_mode == "client_credentials" else None,
            "token_expires_in": int(expires - time.time()) if expires else None,
            "llm_provider": s.llm_provider, "connected": session.server_info is not None,
            "server": session.server_info, "error": session.last_error,
            "active_cube": s.active_cube, "has_context": session.ctx is not None,
            "admin_protected": bool(admin_password),
        }

    @app.post("/api/connect")
    def connect():
        return session.connect()

    # ----- admin: persistent settings --------------------------------------------------
    @app.get("/api/admin/settings")
    def get_settings():
        return settings_store.public_view(session.settings)

    def _candidate(updates: dict) -> Settings:
        errors = settings_store.validate(updates)
        if errors:
            raise HTTPException(422, "; ".join(errors))
        return settings_store.merge(session.settings, updates)

    @app.post("/api/admin/test")
    def test_settings(updates: dict[str, Any]):
        """Try the unsaved settings without changing the live session."""
        trial = Session(_candidate(updates))
        return trial.connect()

    @app.put("/api/admin/settings")
    def save_settings(updates: dict[str, Any]):
        new = _candidate(updates)
        settings_store.save(new)
        session.configure(new)
        result = session.connect()
        return {"saved": True, "connection": result,
                "settings": settings_store.public_view(new)}

    # ----- catalog: applications and cubes on the server --------------------------------
    @app.get("/api/catalog")
    def catalog():
        apps = _list(session.call("list_applications"), "applications")
        saved = {c["cube"]: c for c in store.list_saved(session.settings.context_dir)}
        out = []
        for a in apps:
            try:
                dbs = _list(session.call("list_databases", application=a), "databases")
            except HTTPException as exc:
                out.append({"application": a, "databases": [], "error": exc.detail})
                continue
            out.append({"application": a, "databases": [
                {"name": d, "cube": f"{a}.{d}", "saved": f"{a}.{d}" in saved,
                 "generated_at": saved.get(f"{a}.{d}", {}).get("generated_at")} for d in dbs]})
        return {"applications": out, "active_cube": session.settings.active_cube}

    # ----- contexts ---------------------------------------------------------------------
    @app.get("/api/contexts")
    def contexts():
        return {"contexts": store.list_saved(session.settings.context_dir),
                "active_cube": session.settings.active_cube}

    def _activate(cube: str) -> None:
        new = settings_store.merge(session.settings, {"active_cube": cube})
        settings_store.save(new)
        session.settings = new
        session.load_active_context()

    @app.post("/api/context/build")
    def build(req: CubeRequest, activate: bool = True):
        session.ensure_connected()
        path = store.path_for(session.settings.context_dir, req.application, req.database)
        existing = store.load(path) if path.exists() else None
        try:
            ctx = store.merge(builder.build(session.client, req.application, req.database),
                              existing)
        except (MCPError, KeyError, TypeError) as exc:
            raise HTTPException(502, f"Context build failed: {exc}") from exc
        store.save(ctx, path)
        if activate:
            _activate(f"{req.application}.{req.database}")
        return ctx

    @app.post("/api/context/activate")
    def activate(req: ActivateRequest):
        if req.cube not in {c["cube"] for c in store.list_saved(session.settings.context_dir)}:
            raise HTTPException(404, f"No saved context for {req.cube}. Build it first.")
        _activate(req.cube)
        return session.ctx

    @app.get("/api/context")
    def get_context():
        if not session.ctx:
            raise HTTPException(404, "No context for the active cube yet. Build one first.")
        return session.ctx

    @app.patch("/api/context")
    def patch_context(patch: ContextPatch):
        if not session.ctx:
            raise HTTPException(404, "No context for the active cube yet.")
        updates = patch.model_dump(exclude_none=True)
        if "glossary" in updates:
            updates["glossary"] = {k.strip(): v for k, v in updates["glossary"].items()}
        if "intents" in updates:
            updates["intents"] = {k: v for k, v in updates["intents"].items() if v}
        errors = store.validate_curated(session.ctx, updates)
        if errors:
            raise HTTPException(422, "; ".join(errors))
        session.ctx.update(updates)
        store.save(session.ctx, session.settings.context_file)
        return session.ctx

    # ----- ask --------------------------------------------------------------------------
    @app.post("/api/ask")
    def ask(req: AskRequest):
        if not session.ctx:
            raise HTTPException(409, "Build a context for this cube first (Context tab).")
        started = time.perf_counter()
        plan = Planner(session.ctx, llm.from_settings(session.settings)).plan(req.question)
        out = {"plan": plan.to_dict(), "ok": plan.ok, "result": None, "error": None,
               "cube": session.settings.active_cube}
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

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():
        return Response(status_code=204)

    return app
