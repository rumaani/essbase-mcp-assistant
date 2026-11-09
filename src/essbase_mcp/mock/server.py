"""Mock OCI identity domain + Essbase MCP server for development without a live 26ai.

IMPORTANT: tool names and schemas here are PLACEHOLDERS shaped like a typical Essbase MCP
catalog. They are not Oracle's published list. The assistant never hard-codes them: it
discovers tools via tools/list and maps them in the context file, so swapping in the real
26ai catalog is a context rebuild, not a code change.

Run:  python -m essbase_mcp.mock.server   (port 9100)
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import time
import uuid

from fastapi import FastAPI, Form, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response

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
_tokens: dict[str, float] = {}
SESSIONS: set[str] = set()  # tests clear this to simulate the server dropping sessions
STATS = {"tokens_issued": 0, "calls": 0}
LATENCY_S = int(os.environ.get("EMCP_MOCK_LATENCY_MS", "0")) / 1000


@app.post("/oauth2/v1/token")
def token(grant_type: str = Form(...), client_id: str = Form(...),
          client_secret: str = Form(...), scope: str = Form("")):
    if grant_type != "client_credentials":
        raise HTTPException(400, "unsupported_grant_type")
    if (client_id, client_secret) != (CLIENT_ID, CLIENT_SECRET):
        raise HTTPException(401, "invalid_client")
    tok = secrets.token_urlsafe(24)
    _tokens[tok] = time.time() + 3600
    STATS["tokens_issued"] += 1
    return {"access_token": tok, "token_type": "Bearer", "expires_in": 3600}


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


def _run_mdx(q: str, outline: dict | None = None) -> dict:
    outline = outline or OUTLINE
    m = re.search(r"SELECT\s+(.+?)\s+ON COLUMNS(?:,\s*(?:NON EMPTY\s+)?(.+?)\s+ON ROWS)?\s+FROM",
                  q, re.IGNORECASE | re.DOTALL)
    if not m:
        raise ValueError("MDX syntax error (mock parser)")
    cols = _members(m.group(1), outline)
    rows = _members(m.group(2), outline) if m.group(2) else ["(slice)"]
    return {"query": q, "columns": cols,
            "rows": [{"member": r, "values": [_fake_value(r, c, q) for c in cols]} for r in rows]}


def _call(name: str, args: dict, profile: str) -> dict:
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
        return _text(_run_mdx(args["query"], _outline(args)))
    return _text({"status": "submitted", "jobId": str(uuid.uuid4())})


@app.post("/essbase/rest/v1/ess-mcp")
async def mcp(request: Request, profile: str = "viewer",
              authorization: str | None = Header(None)):
    tok = (authorization or "").removeprefix("Bearer ").strip()
    if _tokens.get(tok, 0) < time.time():
        return JSONResponse({"error": "invalid_token"}, status_code=401)
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
            result = _call(params["name"], params.get("arguments", {}), profile)
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
