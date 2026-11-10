"""Build a context file from a live (or mock) Essbase MCP server.

1. tools/list            -> tool catalog with schemas
2. suggest_intents()     -> intent -> tool mapping (heuristic; humans can override)
3. mapped tools          -> dimensions + members + aliases for one cube
4. merge with existing   -> curated glossary / intents / examples are preserved
"""
from __future__ import annotations

import datetime as dt

from essbase_mcp.context.args import fit
from essbase_mcp.mcp.client import MCPClient

INTENT_KEYWORDS: dict[str, tuple[list[str], list[str]]] = {
    # intent: (must contain one of, must not contain any of)  -- matched on name + description
    "list_applications": (["application"], ["database", "cube", "dimension", "member"]),
    "list_databases": (["database", "cube"], ["dimension", "member", "mdx", "query", "calc"]),
    "get_dimensions": (["dimension"], ["member"]),
    "get_members": (["member"], ["mdx", "search", "find"]),
    "search_members": (["search", "find"], ["mdx", "calc"]),
    "data_query": (["mdx", "query", "grid", "report"], ["calc"]),
    "calculation": (["calc"], []),
}


def suggest_intents(tools: list[dict]) -> dict[str, str]:
    mapping = {}
    for intent, (need, avoid) in INTENT_KEYWORDS.items():
        best, best_score = None, 0
        for t in tools:
            text = f"{t['name']} {t.get('description', '')}".lower()
            if any(a in t["name"].lower() for a in avoid):
                continue
            score = sum(2 if n in t["name"].lower() else 1 for n in need if n in text)
            if score > best_score:
                best, best_score = t["name"], score
        if best:
            mapping[intent] = best
    return mapping


def build(client: MCPClient, application: str, database: str,
          max_members: int = 5000) -> dict:
    """Read one cube. Dimensions larger than max_members are cached top-down and marked
    `truncated`; members beyond the cache are found at question time via search_members."""
    client.ensure_initialized()
    server = client.server_info or {}
    tools = client.list_tools()
    by_name = {t["name"]: t for t in tools}
    intents = suggest_intents(tools)

    def call(intent: str, **canonical):
        tool = by_name[intents[intent]]
        return MCPClient.tool_payload(client.call_tool(tool["name"], fit(tool, canonical)))

    dimensions = []
    if "get_dimensions" in intents:
        for d in call("get_dimensions", application=application, database=database)["dimensions"]:
            members = []
            if "get_members" in intents:
                members = call("get_members", application=application, database=database,
                               dimension=d["name"]).get("members", [])
            entry = {"name": d["name"], "type": d.get("type", "regular"),
                     "members": [{k: v for k, v in m.items() if v is not None}
                                 for m in members[:max_members]]}
            if len(members) > max_members:
                entry["truncated"] = True
                entry["total_members"] = len(members)
            dimensions.append(entry)

    return {
        "version": 1,
        "revision": 1,
        "generated_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "source": {"server": server.get("name"), "url": client.url, "profile": client.profile},
        "cube": {"application": application, "database": database},
        "tools": [{"name": t["name"], "description": t.get("description", ""),
                   "input_schema": t.get("inputSchema", {})} for t in tools],
        "intents": intents,
        "dimensions": dimensions,
        "glossary": {},
        "defaults": {},
        "examples": [],
        "notes": "",
    }
