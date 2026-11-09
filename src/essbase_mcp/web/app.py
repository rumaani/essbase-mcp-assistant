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
import html
import os
import secrets
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import anyio.to_thread
import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from essbase_mcp import runtime, settings_store
from essbase_mcp.auth.user_sessions import (
    SessionStore,
    SignInRequired,
    UserSession,
    UserTokens,
    claims_of,
)
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
COOKIE = "emcp_session"
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
    """The connection to Essbase shared by every request: one pool, one cap on parallel calls,
    and (in shared mode) one identity. In per_user mode each signed-in user gets their own
    MCP client on the same pool and cap."""

    def __init__(self, settings: Settings, metrics: Metrics):
        self.settings = settings
        self.metrics = metrics
        self.http = runtime.http_client(settings)
        self.slots = threading.BoundedSemaphore(max(1, settings.max_concurrent_calls))
        self.client = runtime.connect(settings, metrics, http=self.http, slots=self.slots)
        self.contexts = ContextCache(settings.context_dir)
        self.users = SessionStore(settings.session_hours)
        if settings.mode == "mock":  # stands in for the identity domain's IdP policy
            from essbase_mcp.mock import server as mock_server

            mock_server.IDP_POLICY = "entra" if settings.sso_provider == "entra" else "local"
        self.last_error: str | None = None
        self._tools: list[dict] | None = None
        self._tools_lock = threading.Lock()

    @property
    def per_user(self) -> bool:
        return self.settings.identity_mode == "per_user"

    def user_client(self, tokens: UserTokens) -> MCPClient:
        return runtime.connect(self.settings, self.metrics, http=self.http, slots=self.slots,
                               auth=tokens)

    def probe(self) -> dict:
        """per_user mode has no identity to test with; check both endpoints answer."""
        eff = runtime.effective(self.settings)
        try:
            t = self.http.post(eff.token_url, data={"grant_type": "client_credentials",
                                                    "client_id": eff.client_id,
                                                    "client_secret": eff.client_secret})
            m = self.http.post(eff.mcp_url, json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
        except httpx.HTTPError as exc:
            return {"ok": False, "error": describe_error(exc)}
        if t.status_code >= 500 or m.status_code >= 500:
            return {"ok": False, "error": f"Server error (token {t.status_code}, MCP {m.status_code})"}
        if t.status_code == 401 and "invalid_client" in t.text:
            return {"ok": False, "error": "Identity domain rejected the client ID / secret"}
        return {"ok": True, "per_user": True, "server": {"name": "endpoints reachable"},
                "tool_count": 0, "note": "Both endpoints answer. Sign in to finish the check."}

    def connect(self) -> dict:
        if self.per_user:
            return self.probe()
        try:
            self.client.initialize()
            with self._tools_lock:
                self._tools = self.client.list_tools()
            self.last_error = None
            return {"ok": True, "server": self.client.server_info, "tool_count": len(self._tools)}
        except CONNECT_ERRORS as exc:
            self.last_error = describe_error(exc)
            return {"ok": False, "error": self.last_error}

    def tools(self, user: UserSession | None = None) -> list[dict]:
        holder = user or self
        cached = holder.tools if user else self._tools
        if cached is not None:
            return cached
        client = user.client if user else self.client
        try:
            tools = client.list_tools()
        except CONNECT_ERRORS as exc:
            self.last_error = describe_error(exc)
            raise HTTPException(502, self.last_error) from exc
        if user:
            user.tools = tools
        else:
            self._tools = tools
        return tools

    def call_intent(self, intent: str, user: UserSession | None = None, **canonical) -> Any:
        tools = self.tools(user)
        mapping = builder.suggest_intents(tools)
        tool = {t["name"]: t for t in tools}.get(mapping.get(intent, ""))
        if not tool:
            raise HTTPException(501, f"The server has no tool for '{intent}'.")
        client = user.client if user else self.client
        res = client.call_tool(tool["name"], fit(tool, canonical))
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
        new = Runtime(settings, self.metrics)  # fresh session store: everyone signs in again
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

    @app.exception_handler(SignInRequired)
    async def signin_required(_req, exc: SignInRequired):
        return JSONResponse({"detail": str(exc), "signin": True}, status_code=401)

    def cube_of(name: str | None) -> str:
        return name or session.settings.active_cube

    def current_user(request: Request, rt: Runtime) -> UserSession | None:
        """per_user mode: the signed-in user (or 401). shared mode: None."""
        if not rt.per_user:
            return None
        user = rt.users.get(request.cookies.get(COOKIE))
        if not user:
            raise SignInRequired("Please sign in to use Essbase.")
        return user

    def client_for(request: Request, rt: Runtime) -> tuple[MCPClient, UserSession | None]:
        user = current_user(request, rt)
        return (user.client if user else rt.client), user

    # ----- per-user sign-in ---------------------------------------------------------------
    def redirect_uri(request: Request) -> str:
        return session.settings.redirect_url or str(request.url_for("auth_callback"))

    @app.get("/auth/login")
    def auth_login(request: Request):
        rt = session.rt
        if not rt.per_user:
            return RedirectResponse("/", status_code=302)
        eff = runtime.effective(rt.settings)
        url = rt.users.begin(runtime.authorize_url(rt.settings), eff.client_id,
                             redirect_uri(request), rt.settings.user_scope)
        return RedirectResponse(url, status_code=302)

    @app.get("/auth/callback", name="auth_callback")
    def auth_callback(request: Request, code: str = "", state: str = "", error: str = "",
                      error_description: str = ""):
        rt = session.rt
        if error:
            return HTMLResponse(f"<p>Sign-in failed: {html.escape(error_description or error)}</p>"
                                '<p><a href="/">Back</a></p>', status_code=400)
        pending = rt.users.take_pending(state)
        if not pending or not code:
            return HTMLResponse('<p>Sign-in expired or was already used. <a href="/auth/login">'
                                "Try again</a></p>", status_code=400)
        verifier, _nonce = pending
        eff = runtime.effective(rt.settings)
        resp = rt.http.post(eff.token_url, data={
            "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri(request),
            "code_verifier": verifier, "client_id": eff.client_id,
            "client_secret": eff.client_secret})
        if resp.status_code != 200:
            return HTMLResponse(f"<p>The identity domain refused the sign-in (HTTP {resp.status_code})."
                                ' <a href="/auth/login">Try again</a></p>', status_code=400)
        body = resp.json()
        claims = claims_of(body.get("id_token"))
        tokens = UserTokens(eff.token_url, eff.client_id, eff.client_secret, rt.http, body)
        user = UserSession(sid=secrets.token_urlsafe(32), subject=claims.get("sub", "user"),
                           name=claims.get("user_displayname") or claims.get("name")
                           or claims.get("sub", "Signed-in user"),
                           tokens=tokens, client=rt.user_client(tokens))
        rt.users.add(user)
        session.metrics.inc("sign_ins")
        out = RedirectResponse("/", status_code=302)
        out.set_cookie(COOKIE, user.sid, httponly=True, samesite="lax",
                       secure=request.url.scheme == "https",
                       max_age=rt.settings.session_hours * 3600)
        return out

    @app.post("/auth/logout")
    def auth_logout(request: Request):
        """Ends the app session. With SSO the Microsoft session stays (so the next sign-in is
        instant) unless a sign-out URL is configured; the browser is sent there next."""
        session.rt.users.remove(request.cookies.get(COOKIE))
        nxt = runtime.effective(session.settings).signout_url or None
        out = JSONResponse({"signed_out": True, "redirect": nxt})
        out.delete_cookie(COOKIE)
        return out

    @app.get("/api/me")
    def me(request: Request):
        rt = session.rt
        if not rt.per_user:
            return {"identity_mode": "shared", "signed_in": False}
        user = rt.users.get(request.cookies.get(COOKIE))
        return {"identity_mode": "per_user", "signed_in": bool(user),
                "sso_provider": rt.settings.sso_provider, "auto_signin": rt.settings.auto_signin,
                "name": user.name if user else None, "subject": user.subject if user else None}

    # ----- status ----------------------------------------------------------------------
    @app.get("/api/status")
    def status(request: Request):
        rt = session.rt
        s, eff = rt.settings, runtime.effective(rt.settings)
        user = rt.users.get(request.cookies.get(COOKIE)) if rt.per_user else None
        client = user.client if user else rt.client
        expires = getattr(client.auth, "expires_at", None)
        connected = client.server_info is not None or (rt.per_user and rt.last_error is None)
        return {
            "identity_mode": s.identity_mode, "user": user.name if user else None,
            "mode": s.mode, "mcp_url": eff.mcp_url, "profile": s.profile,
            "auth_mode": eff.auth_mode, "client_id": eff.client_id or None,
            "token_expires_in": int(expires - time.time()) if expires else None,
            "llm_provider": s.llm_provider, "connected": connected,
            "server": client.server_info, "error": rt.last_error,
            "default_cube": s.active_cube, "active_cube": s.active_cube,
            "has_context": rt.contexts.get(s.active_cube) is not None,
            "admin_protected": bool(admin_password),
        }

    @app.get("/api/metrics")
    def metrics():
        rt = session.rt
        return {**session.metrics.snapshot(),
                "token_fetches": getattr(rt.client.auth, "fetches", 0),
                "signed_in_users": rt.users.count(),
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
    def catalog(request: Request):
        rt = session.rt
        _, user = client_for(request, rt)
        apps = _list(rt.call_intent("list_applications", user), "applications")
        saved = {c["cube"]: c for c in rt.contexts.list()}
        out = []
        for a in apps:
            try:
                dbs = _list(rt.call_intent("list_databases", user, application=a), "databases")
            except HTTPException as exc:
                out.append({"application": a, "databases": [], "error": exc.detail})
                continue
            out.append({"application": a, "databases": [
                {"name": d, "cube": f"{a}.{d}", "saved": f"{a}.{d}" in saved,
                 "generated_at": saved.get(f"{a}.{d}", {}).get("generated_at")} for d in dbs]})
        return {"applications": out, "default_cube": rt.settings.active_cube}

    # ----- contexts ---------------------------------------------------------------------
    @app.get("/api/contexts")
    def contexts(request: Request):
        rt = session.rt
        current_user(request, rt)
        return {"contexts": rt.contexts.list(), "default_cube": rt.settings.active_cube,
                "active_cube": rt.settings.active_cube}

    @app.post("/api/context/build")
    def build(req: CubeRequest, request: Request):
        rt = session.rt
        client, _ = client_for(request, rt)  # per_user: loads with the signed-in user's access
        cube = f"{req.application}.{req.database}"
        with rt.contexts.lock_for(cube):  # one build per cube at a time
            existing = rt.contexts.get(cube)
            try:
                generated = builder.build(client, req.application, req.database,
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
    def get_context(request: Request, cube: str | None = None):
        current_user(request, session.rt)
        entry = session.rt.contexts.get(cube_of(cube))
        if not entry:
            raise HTTPException(404, f"{cube_of(cube)} isn't loaded yet.")
        return entry.ctx

    @app.patch("/api/context")
    def patch_context(patch: ContextPatch, request: Request, cube: str | None = None):
        rt, name = session.rt, cube_of(cube)
        current_user(request, rt)
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
    def ask(req: AskRequest, request: Request):
        rt, cube = session.rt, cube_of(req.cube)
        client, _ = client_for(request, rt)
        entry = rt.contexts.get(cube)
        if not entry:
            raise HTTPException(409, f"{cube} isn't loaded yet (Cubes page).")
        started = time.perf_counter()
        session.metrics.inc("questions")
        planner = Planner(entry.ctx, llm.from_settings(rt.settings), index=entry.index)
        # per_user: a name not found for one user may exist for another (metadata filters),
        # so misses aren't remembered; found members are shared (the outline is shared anyway)
        outcome = Executor(planner, client, entry, remember_misses=not rt.per_user).run(
            req.question, req.execute)
        plan = outcome.plan
        return {"plan": plan.to_dict(), "ok": plan.ok, "result": outcome.result,
                "error": outcome.error, "cube": cube,
                "steps": [s.__dict__ for s in outcome.steps],
                "elapsed_ms": round((time.perf_counter() - started) * 1000)}

    # mock identity domain + Essbase, reachable by the browser for the sign-in page
    from essbase_mcp.mock import server as mock_server

    @app.middleware("http")
    async def mock_only(request: Request, call_next):
        if request.url.path.startswith("/mock") and session.settings.mode != "mock":
            return JSONResponse({"detail": "Not found"}, status_code=404)
        return await call_next(request)

    app.mount("/mock", mock_server.app)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():
        return Response(status_code=204)

    return app
