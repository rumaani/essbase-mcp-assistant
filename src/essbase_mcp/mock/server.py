"""Mock OCI identity domain + Essbase MCP server for development without a live 26ai.

IMPORTANT: tool names and schemas here are PLACEHOLDERS shaped like a typical Essbase MCP
catalog. They are not Oracle's published list. The assistant never hard-codes them: it
discovers tools via tools/list and maps them in the context file, so swapping in the real
26ai catalog is a context rebuild, not a code change.

Run:  python -m essbase_mcp.mock.server   (port 9100)
"""
from __future__ import annotations

import json
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


@app.post("/oauth2/v1/token")
def token(grant_type: str = Form(...), client_id: str = Form(...),
          client_secret: str = Form(...), scope: str = Form("")):
    if grant_type != "client_credentials":
        raise HTTPException(400, "unsupported_grant_type")
    if (client_id, client_secret) != (CLIENT_ID, CLIENT_SECRET):
        raise HTTPException(401, "invalid_client")
    tok = secrets.token_urlsafe(24)
    _tokens[tok] = time.time() + 3600
    return {"access_token": tok, "token_type": "Bearer", "expires_in": 3600}


def _text(obj) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(obj)}], "structuredContent": obj}


def _fake_value(*members: str) -> float:
    return float(sum(ord(c) for m in members for c in m) % 9000 + 1000)


def _call(name: str, args: dict, profile: str) -> dict:
    tool = next((t for t in TOOLS if t["name"] == name), None)
    if not tool:
        raise ValueError(f"Unknown tool {name}")
    if PROFILE_RANK[profile] < PROFILE_RANK[tool["min_profile"]]:
        raise PermissionError(f"Tool {name} requires profile {tool['min_profile']}")
    if name == "list_applications":
        return _text({"applications": ["Sample"]})
    if name == "list_databases":
        return _text({"databases": ["Basic"] if args["application"] == "Sample" else []})
    if name == "get_dimensions":
        return _text({"dimensions": [{"name": d, "type": v["type"], "memberCount": len(v["members"])}
                                     for d, v in OUTLINE.items()]})
    if name == "get_members":
        dim = OUTLINE[args["dimension"]]["members"]
        parent = args.get("parent")
        rows = [m for m in dim if parent is None or m[1] == parent]
        return _text({"members": [{"name": n, "parent": p, "alias": a} for n, p, a in rows]})
    if name == "execute_mdx_query":
        q = args["query"]
        cols = q.split("ON COLUMNS")[0].split("SELECT", 1)[-1]
        names = [s.strip(" []{}") for s in cols.replace("},", ",").split(",") if s.strip(" []{}")]
        return _text({"query": q, "columns": names,
                      "rows": [{"member": "(slice)", "values": [_fake_value(n, q) for n in names]}]})
    return _text({"status": "submitted", "jobId": str(uuid.uuid4())})


@app.post("/essbase/rest/v1/ess-mcp")
async def mcp(request: Request, profile: str = "viewer",
              authorization: str | None = Header(None)):
    tok = (authorization or "").removeprefix("Bearer ").strip()
    if _tokens.get(tok, 0) < time.time():
        return JSONResponse({"error": "invalid_token"}, status_code=401)
    msg = await request.json()
    if "id" not in msg:  # notification
        return Response(status_code=202)
    rid, method, params = msg["id"], msg["method"], msg.get("params", {})
    try:
        if method == "initialize":
            result = {"protocolVersion": params.get("protocolVersion"),
                      "capabilities": {"tools": {}},
                      "serverInfo": {"name": "mock-essbase-mcp", "version": "0.0.1"}}
        elif method == "tools/list":
            visible = [t for t in TOOLS if PROFILE_RANK[profile] >= PROFILE_RANK[t["min_profile"]]]
            result = {"tools": [{k: v for k, v in t.items() if k != "min_profile"}
                                for t in visible]}
        elif method == "tools/call":
            result = _call(params["name"], params.get("arguments", {}), profile)
        else:
            return JSONResponse({"jsonrpc": "2.0", "id": rid,
                                 "error": {"code": -32601, "message": "Method not found"}})
    except Exception as exc:  # noqa: BLE001 - surfaced as a tool error, like a real server would
        result = {"isError": True, "content": [{"type": "text", "text": str(exc)}]}
    headers = {"Mcp-Session-Id": "mock-session"}
    return JSONResponse({"jsonrpc": "2.0", "id": rid, "result": result}, headers=headers)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=9100)
