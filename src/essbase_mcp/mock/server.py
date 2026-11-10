"""Mock OCI identity domain + Essbase MCP server for development without a live 26ai.

IMPORTANT: tool names and schemas here are PLACEHOLDERS shaped like a typical Essbase MCP
catalog. They are not Oracle's published list. The assistant never hard-codes them: it
discovers tools via tools/list and maps them in the context file, so swapping in the real
26ai catalog is a context rebuild, not a code change.

Run:  python -m essbase_mcp.mock.server   (port 9100)
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import html
import json
import os
import re
import secrets
import time
import uuid
from urllib.parse import urlencode

from fastapi import FastAPI, Form, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

CLIENT_ID, CLIENT_SECRET = "essbase-assistant", "change-me"
PROFILE_RANK = {"viewer": 0, "analyst": 1, "admin": 2}

# --- Sample.Basic (abridged) -------------------------------------------------
OUTLINE = {
    "Year": {"type": "time", "members": [
        ("Year", None, None), ("Qtr1", "Year", "Quarter 1"), ("Qtr2", "Year", "Quarter 2"),
        ("Qtr3", "Year", "Quarter 3"), ("Qtr4", "Year", "Quarter 4"),
        ("Jan", "Qtr1", "January"), ("Feb", "Qtr1", "February"), ("Mar", "Qtr1", "March")]},
    "Measures": {"type": "accounts", "members": [
        ("Measures", None, None), ("Profit", "Measures", None), ("Margin", "Profit", None),
        ("Sales", "Margin", "Revenue"), ("COGS", "Margin", "Cost of Goods Sold"),
        ("Total Expenses", "Profit", None), ("Marketing", "Total Expenses", None)]},
    "Product": {"type": "regular", "members": [
        ("Product", None, "All Products"), ("100", "Product", "Colas"),
        ("200", "Product", "Root Beer"), ("300", "Product", "Cream Soda"),
        ("400", "Product", "Fruit Soda"), ("100-10", "100", "Cola"),
        ("100-20", "100", "Diet Cola")]},
    "Market": {"type": "regular", "members": [
        ("Market", None, "All Markets"), ("East", "Market", None), ("West", "Market", None),
        ("South", "Market", None), ("Central", "Market", None),
        ("New York", "East", None), ("California", "West", None), ("Texas", "South", None)]},
    "Scenario": {"type": "regular", "members": [
        ("Scenario", None, None), ("Actual", "Scenario", None), ("Budget", "Scenario", None),
        ("Variance", "Scenario", None)]},
}

# --- ASOSamp.Basic (abridged aggregate-storage sample) -----------------------
ASO_OUTLINE = {
    "Measures": {"type": "accounts", "members": [
        ("Measures", None, None), ("Original Price", "Measures", None),
        ("Price Paid", "Measures", "Net Revenue"), ("Units", "Measures", "Units Sold"),
        ("Avg Sale Price", "Measures", None)]},
    "Years": {"type": "regular", "members": [
        ("Years", None, None), ("Curr Year", "Years", "Current Year"),
        ("Prev Year", "Years", "Prior Year")]},
    "Time": {"type": "time", "members": [
        ("Time", None, None), ("MTD", "Time", None), ("Qtr1", "MTD", "Q1"),
        ("Qtr2", "MTD", "Q2"), ("Qtr3", "MTD", "Q3"), ("Qtr4", "MTD", "Q4")]},
    "Geography": {"type": "regular", "members": [
        ("Geography", None, None), ("East", "Geography", "East Region"),
        ("West", "Geography", "West Region"), ("Central", "Geography", "Central Region")]},
    "Products": {"type": "regular", "members": [
        ("Products", None, "All Merchandise"), ("Personal Electronics", "Products", None),
        ("Home Entertainment", "Products", None), ("Computers and Peripherals", "Products", None),
        ("Digital Cameras/Camcorders", "Personal Electronics", "Cameras")]},
    "Stores": {"type": "regular", "members": [
        ("Stores", None, "All Stores"), ("Mall", "Stores", None), ("Outlet", "Stores", None),
        ("Online", "Stores", None)] + [
        (f"Store {i:03d}", ("Mall", "Outlet", "Online")[i % 3], None) for i in range(1, 121)]},
}

OUTLINES = {("Sample", "Basic"): OUTLINE, ("ASOSamp", "Basic"): ASO_OUTLINE}


def _outline(args: dict) -> dict:
    key = (args.get("application"), args.get("database"))
    if key not in OUTLINES:
        raise ValueError(f"Cube {key[0]}.{key[1]} not found")
    return OUTLINES[key]

TOOLS = [
    {"name": "list_applications", "min_profile": "viewer",
     "description": "List Essbase applications the user can access.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "list_databases", "min_profile": "viewer",
     "description": "List databases (cubes) in an application.",
     "inputSchema": {"type": "object", "required": ["application"],
                     "properties": {"application": {"type": "string"}}}},
    {"name": "get_dimensions", "min_profile": "viewer",
     "description": "Get the dimensions of a cube with type and member count.",
     "inputSchema": {"type": "object", "required": ["application", "database"],
                     "properties": {"application": {"type": "string"},
                                    "database": {"type": "string"}}}},
    {"name": "get_members", "min_profile": "viewer",
     "description": "Get members of a dimension (optionally children of a parent) with aliases.",
     "inputSchema": {"type": "object", "required": ["application", "database", "dimension"],
                     "properties": {"application": {"type": "string"},
                                    "database": {"type": "string"},
                                    "dimension": {"type": "string"},
                                    "parent": {"type": "string"}}}},
    {"name": "search_members", "min_profile": "viewer",
     "description": "Find members whose name or alias contains a search string.",
     "inputSchema": {"type": "object", "required": ["application", "database", "pattern"],
                     "properties": {"application": {"type": "string"},
                                    "database": {"type": "string"},
                                    "pattern": {"type": "string"},
                                    "dimension": {"type": "string"}}}},
    {"name": "execute_mdx_query", "min_profile": "viewer",
     "description": "Execute an MDX query against a cube and return a grid of data values.",
     "inputSchema": {"type": "object", "required": ["application", "database", "query"],
                     "properties": {"application": {"type": "string"},
                                    "database": {"type": "string"},
                                    "query": {"type": "string"}}}},
    {"name": "run_calculation", "min_profile": "admin",
     "description": "Run a calculation script on a cube.",
     "inputSchema": {"type": "object", "required": ["application", "database", "script"],
                     "properties": {"application": {"type": "string"},
                                    "database": {"type": "string"},
                                    "script": {"type": "string"}}}},
]

app = FastAPI(title="Mock Essbase MCP")

# --- demo users and their Essbase security filters (read access) ---------------------
# A cell is readable only if, for each filtered dimension, the cell's member is the allowed
# member or one of its descendants - like an Essbase filter on @IDESCENDANTS("East").
USERS = {
    "alice": {"name": "Alice Analyst", "note": "East only", "upn": "alice@contoso.com",
              "filters": {"Market": ["East"], "Geography": ["East"]}},
    "bob": {"name": "Bob Buyer", "note": "West only", "upn": "bob@contoso.com",
            "filters": {"Market": ["West"], "Geography": ["West"]}},
    "carol": {"name": "Carol Controller", "note": "all data", "upn": "carol@contoso.com",
              "filters": {}},
}

# --- mock Microsoft Entra ID, federated with the mock identity domain -------------------
# Simulates: identity domain IdP policy -> Entra ID (SAML) -> back to the identity domain,
# which maps the Entra user (UPN / email) to its own user and issues the code as usual.
# IDP_POLICY is what an admin sets in the identity domain; the app sets it in mock mode.
IDP_POLICY = "local"  # local = identity domain sign-in page | entra = send users to Entra ID
ENTRA_ACCOUNTS = {d["upn"]: u for u, d in USERS.items()}
ENTRA_ACCOUNTS["dave@contoso.com"] = None  # exists in Entra, never provisioned to the domain
ENTRA_COOKIE = "mock_entra_session"
_fed_requests: dict[str, dict] = {}  # pending sign-ins waiting for Entra
SERVICE_USER = {"name": "Service account", "note": "client credentials", "filters": {}}
ACCESS_TTL = 3600  # seconds; tests shorten it to exercise refresh

_tokens: dict[str, dict] = {}        # access token -> {"exp", "user"}
_refresh: dict[str, str] = {}        # refresh token -> username
_codes: dict[str, dict] = {}         # auth code -> request details
SESSIONS: set[str] = set()  # tests clear this to simulate the server dropping sessions
STATS = {"tokens_issued": 0, "calls": 0}
LATENCY_S = int(os.environ.get("EMCP_MOCK_LATENCY_MS", "0")) / 1000


def _issue(user: str | None, with_refresh: bool) -> dict:
    tok = secrets.token_urlsafe(24)
    _tokens[tok] = {"exp": time.time() + ACCESS_TTL, "user": user}
    STATS["tokens_issued"] += 1
    out = {"access_token": tok, "token_type": "Bearer", "expires_in": ACCESS_TTL}
    if user:
        claims = {"sub": user, "user_displayname": USERS[user]["name"], "iss": "mock-idcs"}
        out["id_token"] = ".".join(base64.urlsafe_b64encode(json.dumps(x).encode()).decode()
                                   .rstrip("=") for x in ({"alg": "none"}, claims)) + "."
    if user and with_refresh:
        rt = secrets.token_urlsafe(24)
        _refresh[rt] = user
        out["refresh_token"] = rt
    return out


@app.get("/oauth2/v1/authorize", response_class=HTMLResponse)
def authorize(client_id: str, redirect_uri: str, response_type: str = "code", state: str = "",
              scope: str = "", code_challenge: str = "", code_challenge_method: str = "",
              nonce: str = ""):
    if client_id != CLIENT_ID or response_type != "code":
        raise HTTPException(400, "invalid_request")
    if code_challenge_method != "S256" or not code_challenge:
        raise HTTPException(400, "PKCE (S256) is required")
    if IDP_POLICY == "entra":  # IdP policy routes this app's sign-ins straight to Entra ID
        rid = secrets.token_urlsafe(12)
        _fed_requests[rid] = {"redirect_uri": redirect_uri, "state": state, "scope": scope,
                              "challenge": code_challenge, "exp": time.time() + 600}
        return RedirectResponse(f"../../entra/saml?{urlencode({'request': rid})}", status_code=302)
    hidden = "".join(f'<input type="hidden" name="{k}" value="{html.escape(v)}">' for k, v in {
        "redirect_uri": redirect_uri, "state": state, "scope": scope,
        "code_challenge": code_challenge, "nonce": nonce}.items())
    buttons = "".join(
        f'<button name="username" value="{u}"><b>{html.escape(d["name"])}</b>'
        f'<span>{html.escape(d["note"])}</span></button>' for u, d in USERS.items())
    return f"""<!doctype html><title>Sign in · Mock identity domain</title>
<style>body{{font:15px system-ui;background:#f2f4f7;display:grid;place-items:center;height:100vh;margin:0}}
form{{background:#fff;padding:28px 32px;border-radius:12px;box-shadow:0 6px 24px #0001;width:340px}}
h1{{font-size:18px;margin:0 0 4px}}p{{color:#667;margin:0 0 18px;font-size:13px}}
button{{display:block;width:100%;text-align:left;padding:10px 14px;margin:8px 0;border:1px solid #d0d5dd;
border-radius:8px;background:#fff;cursor:pointer;font:inherit}}button:hover{{border-color:#2f5bd3}}
button span{{display:block;color:#667;font-size:12.5px}}</style>
<form method="post" action="approve"><h1>Mock identity domain</h1>
<p>Pick a demo user. Each has a different Essbase security filter.</p>{hidden}{buttons}</form>"""


def _new_code(user: str, redirect_uri: str, state: str, scope: str, challenge: str):
    code = secrets.token_urlsafe(18)
    _codes[code] = {"user": user, "redirect_uri": redirect_uri, "scope": scope,
                    "challenge": challenge, "exp": time.time() + 120}
    sep = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(f"{redirect_uri}{sep}{urlencode({'code': code, 'state': state})}",
                            status_code=302)


@app.get("/entra/saml", response_class=HTMLResponse)
def entra_saml(request: Request, req: str = Query("", alias="request")):
    """Entra ID receiving the identity domain's SAML request. Already signed in to Microsoft
    (session cookie)? Answer immediately - no page, no typing. Otherwise: account picker."""
    if req not in _fed_requests:
        raise HTTPException(400, "unknown or expired SAML request")
    upn = request.cookies.get(ENTRA_COOKIE)
    if upn in ENTRA_ACCOUNTS:
        return RedirectResponse(f"../oauth2/v1/federated?{urlencode({'request': req, 'upn': upn})}",
                                status_code=302)
    accounts = "".join(
        f'<button name="upn" value="{a}"><b>{html.escape(a)}</b><span>'
        f'{html.escape(USERS[u]["name"] + " · " + USERS[u]["note"]) if u else "not provisioned in the identity domain"}'
        f"</span></button>" for a, u in ENTRA_ACCOUNTS.items())
    return f"""<!doctype html><title>Sign in to your account · Mock Microsoft Entra ID</title>
<style>body{{font:15px "Segoe UI",system-ui;background:#f3f2f1;display:grid;place-items:center;height:100vh;margin:0}}
form{{background:#fff;padding:36px 40px;width:400px;box-shadow:0 2px 6px #0003}}
.brand{{font-weight:600;color:#5e5e5e;margin-bottom:16px}}h1{{font-size:22px;font-weight:600;margin:0 0 6px}}
p{{color:#555;font-size:13px;margin:0 0 16px}}button{{display:block;width:100%;text-align:left;padding:10px 12px;
margin:4px 0;border:0;background:#fff;cursor:pointer;font:inherit}}button:hover{{background:#f2f2f2}}
button span{{display:block;color:#666;font-size:12.5px}}</style>
<form method="post" action="signin"><div class="brand">Mock Microsoft Entra ID</div><h1>Pick an account</h1>
<p>Your organization signs in with Microsoft. In a real company this step is usually skipped,
because Windows or your browser is already signed in.</p>
<input type="hidden" name="request" value="{html.escape(req)}">{accounts}</form>"""


@app.post("/entra/signin")
def entra_signin(upn: str = Form(...), req: str = Form(..., alias="request")):
    if upn not in ENTRA_ACCOUNTS or req not in _fed_requests:
        raise HTTPException(400, "invalid sign-in")
    out = RedirectResponse(f"../oauth2/v1/federated?{urlencode({'request': req, 'upn': upn})}",
                           status_code=302)
    out.set_cookie(ENTRA_COOKIE, upn, path="/", httponly=True, samesite="lax")
    return out


@app.get("/entra/signout")
def entra_signout(return_to: str = Query("/", alias="return")):
    out = RedirectResponse(return_to if return_to.startswith("/") else "/", status_code=302)
    out.delete_cookie(ENTRA_COOKIE, path="/")
    return out


@app.get("/oauth2/v1/federated", response_class=HTMLResponse)
def federated(req: str = Query(..., alias="request"), upn: str = ""):
    """The identity domain consuming Entra's (here: unsigned, mock-only) assertion: map the
    Entra user to a domain user by UPN/email, then issue the authorization code as usual."""
    pending = _fed_requests.pop(req, None)
    if not pending or pending["exp"] < time.time():
        raise HTTPException(400, "expired sign-in")
    user = ENTRA_ACCOUNTS.get(upn)
    if not user:
        return HTMLResponse(f"<h2>Can't sign you in</h2><p>{html.escape(upn)} signed in with "
                            "Microsoft, but has no user in the identity domain. Ask an administrator "
                            "to provision the account (SCIM or just-in-time provisioning).</p>",
                            status_code=403)
    return _new_code(user, pending["redirect_uri"], pending["state"], pending["scope"],
                     pending["challenge"])


@app.post("/oauth2/v1/approve")
def approve(username: str = Form(...), redirect_uri: str = Form(...), state: str = Form(""),
            scope: str = Form(""), code_challenge: str = Form(...), nonce: str = Form("")):
    if username not in USERS:
        raise HTTPException(400, "unknown user")
    return _new_code(username, redirect_uri, state, scope, code_challenge)


@app.post("/oauth2/v1/token")
def token(grant_type: str = Form(...), client_id: str = Form(""), client_secret: str = Form(""),
          scope: str = Form(""), code: str = Form(""), redirect_uri: str = Form(""),
          code_verifier: str = Form(""), refresh_token: str = Form("")):
    if (client_id, client_secret) != (CLIENT_ID, CLIENT_SECRET):
        raise HTTPException(401, "invalid_client")
    if grant_type == "client_credentials":
        return _issue(None, False)
    if grant_type == "authorization_code":
        req = _codes.pop(code, None)  # single use
        if not req or req["exp"] < time.time() or req["redirect_uri"] != redirect_uri:
            raise HTTPException(400, "invalid_grant")
        challenge = base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode()).digest()
                                             ).rstrip(b"=").decode()
        if challenge != req["challenge"]:
            raise HTTPException(400, "invalid_grant: PKCE verification failed")
        return _issue(req["user"], "offline_access" in req["scope"].split())
    if grant_type == "refresh_token":
        user = _refresh.pop(refresh_token, None)  # rotation: old refresh token is spent
        if not user:
            raise HTTPException(400, "invalid_grant")
        return _issue(user, True)
    raise HTTPException(400, "unsupported_grant_type")


def _text(obj) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(obj)}], "structuredContent": obj}


def _fake_value(*members: str) -> float:
    return float(sum(ord(c) for m in members for c in m) % 9000 + 1000)


def _members(set_expr: str, outline: dict) -> list[str]:
    """Resolve '{[a], [b]}' or '[X].Children' against the mock outline."""
    set_expr = set_expr.strip()
    if set_expr.endswith(".Children"):
        parent = set_expr[: -len(".Children")].strip("[] ")
        return [n for d in outline.values() for n, p, _ in d["members"] if p == parent]
    return [m.strip(" []") for m in set_expr.strip("{} ").split(",") if m.strip(" []")]


def _allowed(member: str, allowed: list[str], parents: dict) -> bool:
    while member:
        if member in allowed:
            return True
        member = parents.get(member)
    return False


def _readable(tuple_members: list[str], outline: dict, filters: dict) -> bool:
    for dim, allowed in (filters or {}).items():
        if dim not in outline:
            continue
        members = outline[dim]["members"]
        names = {n for n, _, _ in members}
        parents = {n: p for n, p, _ in members}
        cell = next((m for m in tuple_members if m in names), members[0][0])  # else top member
        if not _allowed(cell, allowed, parents):
            return False
    return True


def _run_mdx(q: str, outline: dict | None = None, filters: dict | None = None) -> dict:
    outline = outline or OUTLINE
    m = re.search(r"SELECT\s+(.+?)\s+ON COLUMNS(?:,\s*(?:NON EMPTY\s+)?(.+?)\s+ON ROWS)?\s+FROM",
                  q, re.IGNORECASE | re.DOTALL)
    if not m:
        raise ValueError("MDX syntax error (mock parser)")
    cols = _members(m.group(1), outline)
    rows = _members(m.group(2), outline) if m.group(2) else ["(slice)"]
    w = re.search(r"WHERE\s*\((.*)\)\s*$", q, re.IGNORECASE | re.DOTALL)
    slicer = [x.strip(" []") for x in w.group(1).split("],") if x.strip(" []")] if w else []
    no_access = 0

    def cell(r, c):
        nonlocal no_access
        if not _readable([r, c, *slicer], outline, filters):
            no_access += 1
            return None  # Essbase shows #NoAccess
        return _fake_value(r, c, q)

    out = {"query": q, "columns": cols,
           "rows": [{"member": r, "values": [cell(r, c) for c in cols]} for r in rows]}
    if no_access:
        out["no_access_cells"] = no_access
    return out


def _call(name: str, args: dict, profile: str, user: dict | None = None) -> dict:
    tool = next((t for t in TOOLS if t["name"] == name), None)
    if not tool:
        raise ValueError(f"Unknown tool {name}")
    if PROFILE_RANK[profile] < PROFILE_RANK[tool["min_profile"]]:
        raise PermissionError(f"Tool {name} requires profile {tool['min_profile']}")
    if name == "list_applications":
        return _text({"applications": sorted({a for a, _ in OUTLINES})})
    if name == "list_databases":
        return _text({"databases": sorted(d for a, d in OUTLINES if a == args["application"])})
    if name == "get_dimensions":
        return _text({"dimensions": [{"name": d, "type": v["type"], "memberCount": len(v["members"])}
                                     for d, v in _outline(args).items()]})
    if name == "get_members":
        outline = _outline(args)
        if args["dimension"] not in outline:
            raise ValueError(f"Dimension {args['dimension']} not found")
        dim = outline[args["dimension"]]["members"]
        parent = args.get("parent")
        rows = [m for m in dim if parent is None or m[1] == parent]
        return _text({"members": [{"name": n, "parent": p, "alias": a} for n, p, a in rows]})
    if name == "search_members":
        outline, pat = _outline(args), args["pattern"].lower()
        hits = [{"name": n, "dimension": d, "parent": par, "alias": a}
                for d, v in outline.items() if not args.get("dimension") or d == args["dimension"]
                for n, par, a in v["members"] if pat in n.lower() or (a and pat in a.lower())]
        return _text({"members": hits[:25], "total": len(hits)})
    if name == "execute_mdx_query":
        return _text(_run_mdx(args["query"], _outline(args), (user or SERVICE_USER)["filters"]))
    return _text({"status": "submitted", "jobId": str(uuid.uuid4())})


@app.post("/essbase/rest/v1/ess-mcp")
async def mcp(request: Request, profile: str = "viewer",
              authorization: str | None = Header(None)):
    tok = (authorization or "").removeprefix("Bearer ").strip()
    info = _tokens.get(tok)
    if not info or info["exp"] < time.time():
        return JSONResponse({"error": "invalid_token"}, status_code=401)
    user = USERS.get(info["user"]) if info["user"] else SERVICE_USER
    msg = await request.json()
    sid = request.headers.get("Mcp-Session-Id")
    if msg.get("method") != "initialize" and sid and sid not in SESSIONS:
        return JSONResponse({"error": "unknown session"}, status_code=404)
    if "id" not in msg:  # notification
        return Response(status_code=202)
    rid, method, params = msg["id"], msg["method"], msg.get("params", {})
    STATS["calls"] += 1
    try:
        if method == "initialize":
            sid = str(uuid.uuid4())
            SESSIONS.add(sid)
            result = {"protocolVersion": params.get("protocolVersion"),
                      "capabilities": {"tools": {}},
                      "serverInfo": {"name": "mock-essbase-mcp", "version": "0.0.1"}}
        elif method == "tools/list":
            visible = [t for t in TOOLS if PROFILE_RANK[profile] >= PROFILE_RANK[t["min_profile"]]]
            result = {"tools": [{k: v for k, v in t.items() if k != "min_profile"}
                                for t in visible]}
        elif method == "tools/call":
            if LATENCY_S:  # simulate a real server's response time (EMCP_MOCK_LATENCY_MS)
                await asyncio.sleep(LATENCY_S)
            result = _call(params["name"], params.get("arguments", {}), profile, user)
        else:
            return JSONResponse({"jsonrpc": "2.0", "id": rid,
                                 "error": {"code": -32601, "message": "Method not found"}})
    except Exception as exc:  # noqa: BLE001 - surfaced as a tool error, like a real server would
        result = {"isError": True, "content": [{"type": "text", "text": str(exc)}]}
    headers = {"Mcp-Session-Id": sid} if sid else {}
    return JSONResponse({"jsonrpc": "2.0", "id": rid, "result": result}, headers=headers)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=9100)
