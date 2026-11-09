"""Map canonical argument names to whatever a tool's inputSchema actually calls them."""
from __future__ import annotations

SYNONYMS = {
    "application": ["application", "applicationName", "appName", "app"],
    "database": ["database", "databaseName", "dbName", "db", "cube", "cubeName"],
    "dimension": ["dimension", "dimensionName", "dimName"],
    "parent": ["parent", "parentName", "member", "memberName"],
    "query": ["query", "mdx", "mdxQuery", "statement"],
    "script": ["script", "calcScript", "scriptName"],
}


def fit(tool: dict, canonical: dict) -> dict:
    """Return arguments renamed to the tool's schema, dropping ones it does not accept."""
    props = (tool.get("inputSchema") or tool.get("input_schema") or {}).get("properties", {})
    out = {}
    for key, value in canonical.items():
        if value is None:
            continue
        for name in SYNONYMS.get(key, [key]):
            if name in props:
                out[name] = value
                break
    return out


def missing_required(tool: dict, args: dict) -> list[str]:
    schema = tool.get("inputSchema") or tool.get("input_schema") or {}
    return [r for r in schema.get("required", []) if r not in args]
