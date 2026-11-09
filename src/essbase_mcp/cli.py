"""emcp: command-line entry point.

  emcp web                                       # browser UI (built-in mock by default)
  emcp apps                                      # list applications and cubes on the server
  emcp context build --app Sample --db Basic     # discover tools + outline -> context file
  emcp ask "Sales for East by Year" --dry-run    # show the plan only
  emcp ask "Sales for East by Year"              # plan + call the tool
  emcp tools                                     # list tools from the server

Settings come from .env and from what was saved in the Admin tab (data/settings.json).
--mock / --remote override the saved mode for one run.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path

from essbase_mcp import runtime, settings_store
from essbase_mcp.config import Settings
from essbase_mcp.context import builder, store
from essbase_mcp.context.args import fit
from essbase_mcp.llm import base as llm
from essbase_mcp.mcp.client import MCPClient
from essbase_mcp.planner.planner import Planner


def _client(s: Settings) -> MCPClient:
    c = runtime.connect(s)
    c.initialize()
    return c


def cmd_build(a, s: Settings) -> int:
    out = a.out or store.path_for(s.context_dir, a.app, a.db)
    existing = store.load(out) if Path(out).exists() else None
    ctx = store.merge(builder.build(runtime.connect(s), a.app, a.db), existing)
    store.save(ctx, out)
    if not a.out:
        settings_store.save(dataclasses.replace(Settings.load(), active_cube=f"{a.app}.{a.db}"))
    print(f"Wrote {out}: {len(ctx['tools'])} tools, {len(ctx['dimensions'])} dimensions, "
          f"intents={ctx['intents']}")
    return 0


def cmd_tools(a, s: Settings) -> int:
    for t in _client(s).list_tools():
        print(f"{t['name']:28} {t.get('description', '')}")
    return 0


def cmd_apps(a, s: Settings) -> int:
    c = _client(s)
    tools = {t["name"]: t for t in c.list_tools()}
    intents = builder.suggest_intents(list(tools.values()))

    def call(intent, **kw):
        tool = tools[intents[intent]]
        return MCPClient.tool_payload(c.call_tool(tool["name"], fit(tool, kw)))

    saved = {x["cube"] for x in store.list_saved(s.context_dir)}
    for app in call("list_applications").get("applications", []):
        for db in call("list_databases", application=app).get("databases", []):
            cube = f"{app}.{db}"
            flags = ("*" if cube == s.active_cube else " ") + ("saved" if cube in saved else "")
            print(f"{cube:30} {flags}")
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
    result = _client(s).call_tool(plan.tool, plan.arguments)
    print(json.dumps(MCPClient.tool_payload(result), indent=2))
    return 1 if result.get("isError") else 0


def cmd_web(a, s: Settings) -> int:
    import uvicorn

    from essbase_mcp.web.app import Session, create_app

    session = Session(s)
    session.connect()
    where = "built-in mock server" if s.mode == "mock" else s.mcp_url
    print(f"Essbase MCP Assistant -> http://{a.host}:{a.port}   ({where})")
    uvicorn.run(create_app(session), host=a.host, port=a.port, log_level="warning")
    return 0


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="emcp")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--mock", action="store_true", help="use the built-in mock for this run")
    mode.add_argument("--remote", action="store_true", help="use the configured server")
    sub = p.add_subparsers(dest="cmd", required=True)

    ctxp = sub.add_parser("context").add_subparsers(dest="sub", required=True)
    b = ctxp.add_parser("build")
    b.add_argument("--app", required=True)
    b.add_argument("--db", required=True)
    b.add_argument("--out")
    b.set_defaults(fn=cmd_build)
    sub.add_parser("tools").set_defaults(fn=cmd_tools)
    sub.add_parser("apps", help="list applications and cubes").set_defaults(fn=cmd_apps)
    q = sub.add_parser("ask")
    q.add_argument("question")
    q.add_argument("--context")
    q.add_argument("--dry-run", action="store_true")
    q.add_argument("--yes", action="store_true", help="confirm calculations")
    q.set_defaults(fn=cmd_ask)
    w = sub.add_parser("web", help="start the browser UI")
    w.add_argument("--demo", action="store_true", help="alias for --mock")
    w.add_argument("--host", default="127.0.0.1")
    w.add_argument("--port", type=int, default=8000)
    w.set_defaults(fn=cmd_web)

    a = p.parse_args(argv)
    s = Settings.load()
    if a.mock or getattr(a, "demo", False):
        s = dataclasses.replace(s, mode="mock")
    elif a.remote:
        s = dataclasses.replace(s, mode="remote")
    sys.exit(a.fn(a, s))
