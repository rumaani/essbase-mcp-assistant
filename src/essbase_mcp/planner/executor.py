"""Run a plan's steps against Essbase and record what happened.

Every step in one question uses the same MCP client, so the same OAuth token and MCP
session (see mcp/client.py for how those are shared safely between concurrent users).
Results of member lookups are remembered per cube for all users (context/cache.py).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from essbase_mcp.context.args import fit
from essbase_mcp.mcp.client import MCPBusy, MCPClient
from essbase_mcp.planner.planner import Plan, Planner, Step

MAX_LOOKUPS = 3


@dataclass
class Outcome:
    plan: Plan
    result: Any = None
    error: str | None = None
    steps: list[Step] = field(default_factory=list)


class Executor:
    def __init__(self, planner: Planner, client: MCPClient | None, cube_entry=None):
        self.planner, self.client, self.entry = planner, client, cube_entry

    def run(self, question: str, execute: bool = True) -> Outcome:
        plan = self.planner.plan(question)
        steps: list[Step] = []

        # 1) look up names the cached outline doesn't know, then plan again
        if plan.unresolved and (plan.intent == "data_query" or plan.intent == "unknown"
                                or plan.top_n):
            lookups, missing = self._lookups(plan.unresolved, execute)
            steps += lookups
            if any(s.status in ("done", "cached") and s.summary and not s.summary.startswith(
                    "No match") for s in lookups):
                plan = self.planner.plan(question)
                plan.unresolved = [u for u in plan.unresolved if u not in missing]
            if missing:
                cube = f"{self.planner.cube['application']}.{self.planner.cube['database']}"
                plan.clarification = (
                    f"I couldn't find {', '.join(f'“{m}”' for m in missing)} in {cube}. "
                    "Check the spelling, or add the word to the Vocabulary.")
                plan.tool = None
        for s in steps:
            if s.status == "skipped":
                plan.warnings.append(f"{s.purpose.replace('Find', 'Couldn’t look up')}: {s.summary}.")
        steps += plan.steps
        out = Outcome(plan, steps=steps)
        if not plan.ok or not execute:
            return out
        if plan.intent == "calculation":
            out.error = "Calculations are not run from the UI. Use the CLI with --yes."
            return out

        # 2) run the plan's own steps
        if plan.top_n:
            return self._run_top_n(out)
        out.result, out.error = self._call(plan.steps[0])
        return out

    # ----------------------------------------------------------------------------------
    def _call(self, step: Step) -> tuple[Any, str | None]:
        started = time.perf_counter()
        try:
            res = self.client.call_tool(step.tool, step.arguments)
        except MCPBusy:
            step.status = "failed"
            raise
        except Exception as exc:  # noqa: BLE001 - reported to the user, not swallowed
            step.status, step.summary = "failed", f"{type(exc).__name__}: {exc}"
            return None, step.summary
        finally:
            step.ms = round((time.perf_counter() - started) * 1000, 1)
        payload = MCPClient.tool_payload(res)
        if res.get("isError"):
            step.status, step.summary = "failed", str(payload)
            return None, step.summary
        step.status = "done"
        if isinstance(payload, dict) and "rows" in payload:
            step.summary = f"{len(payload['rows'])} row(s)"
        return payload, None

    def _lookups(self, phrases: list[str], execute: bool) -> tuple[list[Step], list[str]]:
        tool = self.planner.tool_for("search_members")
        steps, missing = [], []
        for phrase in phrases[:MAX_LOOKUPS]:
            step = Step("lookup", f"Find “{phrase}” in the outline",
                        tool["name"] if tool else None)
            lock = self.entry.lookup_lock(phrase) if self.entry and execute else None
            if lock:
                lock.acquire()
            try:
                self._lookup_one(phrase, step, tool, execute, missing)
            finally:
                if lock:
                    lock.release()
            steps.append(step)
        return steps, missing

    def _lookup_one(self, phrase, step, tool, execute, missing) -> None:
        cached = self.entry.cached_lookup(phrase) if self.entry else None
        if cached is not None:
            step.status = "cached"
            step.summary = (", ".join(f"{d}:{m}" for d, m, _ in cached) if cached
                            else "No match (remembered)")
            if not cached:
                missing.append(phrase)
            return
        if not tool:
            step.status, step.summary = "skipped", "The server has no member-search tool"
            return
        step.arguments = fit(tool, self.planner._canonical(pattern=phrase))
        if not execute:
            return
        payload, err = self._call(step)
        if err:
            return
        hits = self._best_matches(phrase, (payload or {}).get("members", []))
        if self.entry:
            self.entry.remember(phrase, hits)
        else:
            for d, m, p in hits:
                self.planner.index.learn(phrase, d, m, p)
        step.summary = ", ".join(f"{d}:{m}" for d, m, _ in hits) if hits else "No match"
        if not hits:
            missing.append(phrase)

    @staticmethod
    def _best_matches(phrase: str, members: list[dict]) -> list[tuple[str, str, str | None]]:
        """Exact name/alias match wins; otherwise accept a single partial match only, so
        'Store 1' never silently becomes 'Store 117'."""
        p = phrase.lower()
        exact = [m for m in members if m["name"].lower() == p or (m.get("alias") or "").lower() == p]
        chosen = exact or (members if len(members) == 1 else [])
        return [(m["dimension"], m["name"], m.get("parent")) for m in chosen[:5] if m.get("dimension")]

    def _run_top_n(self, out: Outcome) -> Outcome:
        plan, t = out.plan, out.plan.top_n
        rank_step = plan.steps[0]
        data, err = self._call(rank_step)
        if err:
            out.error = err
            return out
        rows = [r for r in data.get("rows", []) if r.get("values")]
        rows.sort(key=lambda r: (r["values"][0] is None, r["values"][0]),
                  reverse=t["order"] == "desc")
        winners = rows[: t["n"]]
        rank_step.summary = (f"{len(rows)} ranked → " +
                             ", ".join(r["member"] for r in winners))
        if len(plan.steps) == 1:  # ranking already answers the question
            out.result = {**data, "rows": winners}
            return out
        follow = plan.steps[1]
        follow.arguments = self.planner.top_n_followup(plan, [r["member"] for r in winners])
        result, err = self._call(follow)
        out.result, out.error = result, err
        return out
