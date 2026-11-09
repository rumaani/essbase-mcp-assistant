"""Web UI backend (Backend-for-Frontend), built for many concurrent users.

The browser only talks to /api/*. OAuth client secrets and tokens stay on the server, and
secrets saved from the Admin tab are encrypted at rest (see settings_store).

Concurrency model (see docs/CONCURRENCY.md):
- Request handlers are plain functions run in a thread pool (size: web_threads).
- All users share ONE Essbase connection (`Runtime`): one OAuth token (single-flight refresh),
  one MCP session, one connection pool, and a cap on parallel Essbase calls with a queue.
- Each request names its cube; nothing a user does changes another user's view.
- Contexts are cached and shared read-only; saves are revisioned (409 on conflict).
- Changing settings builds a new Runtime and swaps it in atomically; requests already
  running finish on the old one.

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
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import anyio.to_thread
import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from essbase_mcp import runtime, settings_store
from essbase_mcp.config import Settings
from essbase_mcp.context import builder, store
from essbase_mcp.context.args import fit
from essbase_mcp.context.cache import Conflict, ContextCache
from essbase_mcp.llm import base as llm
from essbase_mcp.mcp.client import MCPBusy, MCPClient, MCPError, Metrics
from essbase_mcp.planner.executor import Executor
from essbase_mcp.planner.planner import Planner

STATIC = Path(__file__).parent / "static"
CSRF_HEADER, CSRF_VALUE = "X-Requested-With", "essbase-mcp-assistant"
CONNECT_ERRORS = (httpx.HTTPError, MCPError, KeyError, ValueError)
WEB_THREADS = int(os.environ.get("EMCP_WEB_THREADS", "128"))


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


class Runtime:
    """One connection to Essbase, shared by every request. Immutable once built."""

    def __init__(self, settings: Settings, metrics: Metrics):
        self.settings = settings
        self.client = runtime.connect(settings, metrics)
        self.contexts = ContextCache(settings.context_dir)
        self.last_error: str | None = None
        self._tools: list[dict] | None = None
        self._tools_lock = threading.Lock()

    def connect(self) -> dict:
        try:
            self.client.initialize()
            with self._tools_lock:
                self._tools = self.client.list_tools()
            self.last_error = None
            return {"ok": True, "server": self.client.server_info, "tool_count": len(self._tools)}
        except CONNECT_ERRORS as exc:
            self.last_error = describe_error(exc)
            return {"ok": False, "error": self.last_error}

    def tools(self) -> list[dict]:
        if self._tools is None:
            with self._tools_lock:
                if self._tools is None:
                    try:
                        self._tools = self.client.list_tools()
                    except CONNECT_ERRORS as exc:
                        self.last_error = describe_error(exc)
                        raise HTTPException(502, self.last_error) from exc
        return self._tools

    def call_intent(self, intent: str, mapping: dict | None = None, **canonical) -> Any:
        tools = self.tools()
        mapping = mapping or builder.suggest_intents(tools)
        tool = {t["name"]: t for t in tools}.get(mapping.get(intent, ""))
        if not tool:
            raise HTTPException(501, f"The server has no tool for '{intent}'.")
        res = self.client.call_tool(tool["name"], fit(tool, canonical))
        payload = MCPClient.tool_payload(res)
        if res.get("isError"):
            raise HTTPException(502, str(payload))
        return payload


class Session:
    """Holds the current Runtime; swapping it is a single atomic assignment."""

    def __init__(self, settings: Settings):
        self.metrics = Metrics()
        self.rt = Runtime(settings, self.metrics)
        self._swap_lock = threading.Lock()

    @property
    def settings(self) -> Settings:
        return self.rt.settings

    def reconfigure(self, settings: Settings) -> dict:
        new = Runtime(settings, self.metrics)
        result = new.connect()
        with self._swap_lock:
            self.rt = new
        return result

    def connect(self) -> dict:
        return self.rt.connect()


# ----- request models ------------------------------------------------------------------
class CubeRequest(BaseModel):
    application: str
    database: str


class ContextPatch(BaseModel):
    revision: int | None = None
    glossary: dict[str, str | list[str]] | None = None
    defaults: dict[str, str] | None = None
    intents: dict[str, str] | None = None
    notes: str | None = None
    examples: list[dict] | None = None


class AskRequest(BaseModel):
    question: str
    cube: str | None = None
    execute: bool = True


def _list(payload: Any, key: str) -> list[str]:
    items = payload.get(key, []) if isinstance(payload, dict) else payload or []
    return [i if isinstance(i, str) else i.get("name", "") for i in items]


def create_app(session: Session) -> FastAPI:
    admin_password = os.environ.get("EMCP_ADMIN_PASSWORD", "")

    @asynccontextmanager
    async def lifespan(_app):
        anyio.to_thread.current_default_thread_limiter().total_tokens = WEB_THREADS
        yield

    app = FastAPI(title="Essbase MCP Assistant", lifespan=lifespan)

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

    @app.exception_handler(MCPBusy)
    async def busy(_req, exc: MCPBusy):
        return JSONResponse({"detail": str(exc)}, status_code=503, headers={"Retry-After": "5"})

    def cube_of(name: str | None) -> str:
        return name or session.settings.active_cube

    # ----- status ----------------------------------------------------------------------
    @app.get("/api/status")
    def status():
        rt = session.rt
        s, eff = rt.settings, runtime.effective(rt.settings)
        expires = getattr(rt.client.auth, "expires_at", None)
        return {
            "mode": s.mode, "mcp_url": eff.mcp_url, "profile": s.profile,
            "auth_mode": eff.auth_mode, "client_id": eff.client_id or None,
            "token_expires_in": int(expires - time.time()) if expires else None,
            "llm_provider": s.llm_provider, "connected": rt.client.server_info is not None,
            "server": rt.client.server_info, "error": rt.last_error,
            "default_cube": s.active_cube, "active_cube": s.active_cube,
            "has_context": rt.contexts.get(s.active_cube) is not None,
            "admin_protected": bool(admin_password),
        }

    @app.get("/api/metrics")
    def metrics():
        rt = session.rt
        return {**session.metrics.snapshot(),
                "token_fetches": getattr(rt.client.auth, "fetches", 0),
                "max_concurrent_calls": rt.settings.max_concurrent_calls,
                "web_threads": WEB_THREADS}

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
        """Try the unsaved settings without touching the live connection."""
        return Runtime(_candidate(updates), Metrics()).connect()

    @app.put("/api/admin/settings")
    def save_settings(updates: dict[str, Any]):
        new = _candidate(updates)
        settings_store.save(new)
        result = session.reconfigure(new)
        return {"saved": True, "connection": result, "settings": settings_store.public_view(new)}

    # ----- catalog ----------------------------------------------------------------------
    @app.get("/api/catalog")
    def catalog():
        rt = session.rt
        apps = _list(rt.call_intent("list_applications"), "applications")
        saved = {c["cube"]: c for c in rt.contexts.list()}
        out = []
        for a in apps:
            try:
                dbs = _list(rt.call_intent("list_databases", application=a), "databases")
            except HTTPException as exc:
                out.append({"application": a, "databases": [], "error": exc.detail})
                continue
            out.append({"application": a, "databases": [
                {"name": d, "cube": f"{a}.{d}", "saved": f"{a}.{d}" in saved,
                 "generated_at": saved.get(f"{a}.{d}", {}).get("generated_at")} for d in dbs]})
        return {"applications": out, "default_cube": rt.settings.active_cube}

    # ----- contexts ---------------------------------------------------------------------
    @app.get("/api/contexts")
    def contexts():
        rt = session.rt
        return {"contexts": rt.contexts.list(), "default_cube": rt.settings.active_cube,
                "active_cube": rt.settings.active_cube}

    @app.post("/api/context/build")
    def build(req: CubeRequest):
        rt = session.rt
        cube = f"{req.application}.{req.database}"
        with rt.contexts.lock_for(cube):  # one build per cube at a time
            existing = rt.contexts.get(cube)
            try:
                generated = builder.build(rt.client, req.application, req.database,
                                          rt.settings.max_members_per_dimension)
            except MCPBusy:
                raise
            except (MCPError, KeyError, TypeError, httpx.HTTPError) as exc:
                raise HTTPException(502, f"Loading {cube} failed: {describe_error(exc)}") from exc
            ctx = store.merge(generated, existing.ctx if existing else None)
            entry = rt.contexts.save(cube, ctx)
        if not rt.contexts.get(rt.settings.active_cube):  # first cube becomes the default
            new = settings_store.merge(rt.settings, {"active_cube": cube})
            settings_store.save(new)
            rt.settings = new
        return entry.ctx

    @app.get("/api/context")
    def get_context(cube: str | None = None):
        entry = session.rt.contexts.get(cube_of(cube))
        if not entry:
            raise HTTPException(404, f"{cube_of(cube)} isn't loaded yet.")
        return entry.ctx

    @app.patch("/api/context")
    def patch_context(patch: ContextPatch, cube: str | None = None):
        rt, name = session.rt, cube_of(cube)
        entry = rt.contexts.get(name)
        if not entry:
            raise HTTPException(404, f"{name} isn't loaded yet.")
        updates = patch.model_dump(exclude_none=True)
        expected = updates.pop("revision", None)
        if "glossary" in updates:
            updates["glossary"] = {k.strip(): v for k, v in updates["glossary"].items()}
        if "intents" in updates:
            updates["intents"] = {k: v for k, v in updates["intents"].items() if v}
        errors = store.validate_curated(entry.ctx, updates)
        if errors:
            raise HTTPException(422, "; ".join(errors))
        try:
            return rt.contexts.save(name, {**entry.ctx, **updates}, expected).ctx
        except Conflict as exc:
            raise HTTPException(409, f"{exc} Reload to see their changes, then edit again.") from exc

    # ----- ask --------------------------------------------------------------------------
    @app.post("/api/ask")
    def ask(req: AskRequest):
        rt, cube = session.rt, cube_of(req.cube)
        entry = rt.contexts.get(cube)
        if not entry:
            raise HTTPException(409, f"{cube} isn't loaded yet (Cubes page).")
        started = time.perf_counter()
        session.metrics.inc("questions")
        planner = Planner(entry.ctx, llm.from_settings(rt.settings), index=entry.index)
        outcome = Executor(planner, rt.client, entry).run(req.question, req.execute)
        plan = outcome.plan
        return {"plan": plan.to_dict(), "ok": plan.ok, "result": outcome.result,
                "error": outcome.error, "cube": cube,
                "steps": [s.__dict__ for s in outcome.steps],
                "elapsed_ms": round((time.perf_counter() - started) * 1000)}

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():
        return Response(status_code=204)

    return app
