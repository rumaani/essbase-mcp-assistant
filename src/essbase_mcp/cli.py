"""emcp: command-line entry point.

  emcp context build --app Sample --db Basic     # discover tools + outline -> context file
  emcp ask "Sales for East by Year" --dry-run    # show the plan only
  emcp ask "Sales for East by Year"              # plan + call the tool
  emcp tools                                     # list tools from the server
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from essbase_mcp.auth import providers
from essbase_mcp.config import Settings
from essbase_mcp.context import builder, store
from essbase_mcp.llm import base as llm
from essbase_mcp.mcp.client import MCPClient
from essbase_mcp.planner.planner import Planner


def _client(s: Settings) -> MCPClient:
    return MCPClient(s.mcp_url, s.profile, providers.from_settings(s))


def cmd_build(a, s: Settings) -> int:
    out = a.out or s.context_file
    existing = store.load(out) if Path(out).exists() else None
    ctx = store.merge(builder.build(_client(s), a.app, a.db), existing)
    store.save(ctx, out)
    print(f"Wrote {out}: {len(ctx['tools'])} tools, {len(ctx['dimensions'])} dimensions, "
          f"intents={ctx['intents']}")
    return 0


def cmd_tools(a, s: Settings) -> int:
    c = _client(s)
    c.initialize()
    for t in c.list_tools():
        print(f"{t['name']:28} {t.get('description', '')}")
    return 0


def cmd_ask(a, s: Settings) -> int:
    ctx = store.load(a.context or s.context_file)
    plan = Planner(ctx, llm.from_settings(s)).plan(a.question)
    print(json.dumps(plan.to_dict(), indent=2))
    if not plan.ok or a.dry_run:
        return 0 if plan.ok else 2
    if plan.intent == "calculation" and not a.yes:
        print("Refusing to run a calculation without --yes.", file=sys.stderr)
        return 3
    c = _client(s)
    c.initialize()
    result = c.call_tool(plan.tool, plan.arguments)
    print(json.dumps(MCPClient.tool_payload(result), indent=2))
    return 1 if result.get("isError") else 0


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="emcp")
    sub = p.add_subparsers(dest="cmd", required=True)
    ctxp = sub.add_parser("context").add_subparsers(dest="sub", required=True)
    b = ctxp.add_parser("build")
    b.add_argument("--app", required=True)
    b.add_argument("--db", required=True)
    b.add_argument("--out")
    b.set_defaults(fn=cmd_build)
    t = sub.add_parser("tools")
    t.set_defaults(fn=cmd_tools)
    q = sub.add_parser("ask")
    q.add_argument("question")
    q.add_argument("--context")
    q.add_argument("--dry-run", action="store_true")
    q.add_argument("--yes", action="store_true", help="confirm calculations")
    q.set_defaults(fn=cmd_ask)
    a = p.parse_args(argv)
    sys.exit(a.fn(a, Settings.from_env()))
