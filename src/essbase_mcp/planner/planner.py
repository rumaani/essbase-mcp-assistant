"""Turn a question into a validated, possibly multi-step, plan.

pipeline: link entities -> classify intent (rules, or an optional LLM hint)
          -> pick tool from context.intents -> fill args -> validate against inputSchema

Multi-step plans (run by planner.executor.Executor):
- lookup: name-like words that match nothing in the cached outline (e.g. "Store 117" in a
  partially loaded dimension) are searched for first; the plan is then rebuilt with them.
- top/bottom N: "top 3 markets by sales" ranks the dimension's members by the measure, then
  (if more was asked, e.g. "...with profit" or "...by quarter") queries the winners.

The LLM, if configured, may only *suggest* an intent and group-by; members are always
resolved against the context file, so it cannot invent outline members or tool names.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from essbase_mcp.context.args import fit, missing_required
from essbase_mcp.planner.intents import classify
from essbase_mcp.planner.linker import Entity, MemberIndex
from essbase_mcp.planner.mdx import build_mdx

TOP_N = re.compile(r"\b(top|bottom|best|worst|highest|lowest)\s+(\d{1,3})\s+([A-Za-z][\w\-]*)", re.IGNORECASE)
MAX_TOP_N = 100


@dataclass
class Step:
    kind: str                 # lookup | rank | query | list
    purpose: str
    tool: str | None = None
    arguments: dict = field(default_factory=dict)
    status: str = "planned"   # planned | done | cached | failed | skipped
    ms: float | None = None
    summary: str | None = None


@dataclass
class Plan:
    question: str
    intent: str
    confidence: float
    tool: str | None = None
    arguments: dict = field(default_factory=dict)
    entities: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    clarification: str | None = None
    unresolved: list[str] = field(default_factory=list)
    top_n: dict | None = None
    steps: list[Step] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.tool is not None and self.clarification is None

    def to_dict(self) -> dict:
        return asdict(self)


class Planner:
    def __init__(self, ctx: dict, llm=None, index: MemberIndex | None = None):
        self.ctx = ctx
        self.index = index or MemberIndex(ctx)
        self.tools = {t["name"]: t for t in ctx.get("tools", [])}
        self.llm = llm
        self.cube = ctx["cube"]

    def tool_for(self, intent: str) -> dict | None:
        name = (self.ctx.get("intents") or {}).get(intent)
        return self.tools.get(name) if name else None

    def _canonical(self, **extra) -> dict:
        return {"application": self.cube["application"], "database": self.cube["database"], **extra}

    def plan(self, question: str) -> Plan:
        link = self.index.link(question)
        entities, group_by, group_parent = link.entities, link.group_by, link.group_parent
        intent, conf = classify(question, bool(entities or group_by or link.unresolved))
        top = TOP_N.search(question)
        if top and intent in ("data_query", "unknown") and (entities or group_by):
            intent, conf = "data_query", max(conf, 0.75)

        if self.llm and conf < 0.8:
            hint = self.llm.suggest(question, self.ctx)
            if hint and hint.get("intent") in self.ctx.get("intents", {}):
                intent, conf = hint["intent"], max(conf, 0.75)
                group_by += [self.index.dimensions[g.lower()] for g in hint.get("group_by", [])
                             if g.lower() in self.index.dimensions and
                             self.index.dimensions[g.lower()] not in group_by]

        plan = Plan(question, intent, conf, entities=[e.__dict__ for e in entities],
                    unresolved=link.unresolved)
        if intent == "unknown":
            plan.clarification = ("I couldn't match that to cube members or an action. Try naming "
                                  "a measure, member or dimension (e.g. 'Sales for East by Year').")
            return plan

        tool = self.tool_for(intent)
        if not tool:
            plan.clarification = f"No tool is mapped to intent '{intent}' in the context file."
            return plan
        canonical = self._canonical()
        defaults = self.ctx.get("defaults") or {}

        if intent == "get_members":
            dims = group_by or [e.dimension for e in entities]
            dim = next((d for d in self.index.dimensions.values()
                        if d.lower() in question.lower()), dims[0] if dims else None)
            if not dim:
                plan.clarification = "Which dimension or member should I list members for?"
                return plan
            canonical["dimension"] = dim
            parent = next((e.member for e in entities if e.dimension == dim
                           and e.member != self.index.top.get(dim)), None)
            if parent:
                canonical["parent"] = parent
        elif intent == "data_query" and top:
            return self._plan_top_n(plan, tool, top, entities, group_by, defaults)
        elif intent == "data_query":
            mdx, warns = build_mdx(entities, group_by, self.index, self.cube, defaults, group_parent)
            canonical["query"] = mdx
            plan.warnings += warns
        elif intent == "calculation":
            plan.warnings.append("Calculation changes data; requires admin profile and confirmation.")

        return self._finish(plan, tool, canonical, "query" if intent == "data_query" else "list")

    def _finish(self, plan: Plan, tool: dict, canonical: dict, kind: str) -> Plan:
        args = fit(tool, canonical)
        missing = missing_required(tool, args)
        if missing:
            plan.clarification = f"Tool {tool['name']} needs: {', '.join(missing)}"
            return plan
        plan.tool, plan.arguments = tool["name"], args
        purpose = {"query": "Get the numbers", "list": "Read from the outline"}[kind]
        plan.steps.append(Step(kind, purpose, tool["name"], args))
        return plan

    # ---------------- top / bottom N ----------------------------------------------------
    def _plan_top_n(self, plan, tool, top, entities, group_by, defaults) -> Plan:
        order = "asc" if top.group(1).lower() in ("bottom", "worst", "lowest") else "desc"
        n = min(int(top.group(2)), MAX_TOP_N)
        word = top.group(3).lower()
        dim = (self.index.dimensions.get(word) or self.index.dimensions.get(word.rstrip("s"))
               or self.index.dimensions.get(word.removesuffix("es")))
        if not dim:  # "top 3 colas" -> member with children
            hits = [e for e in entities if e.text.lower() == word]
            dim = hits[0].dimension if hits else None
        if not dim:
            plan.clarification = f"Top {n} of what? I couldn't match “{top.group(3)}” to a dimension."
            return plan
        accounts = next((d for d, t in self.index.dim_type.items() if t == "accounts"), None)
        measures = [e for e in entities if e.dimension == accounts]
        rank_measure = self._rank_measure(plan.question, measures) or (
            defaults.get(accounts) or self.index.top.get(accounts))
        # scope: a parent member of the ranked dimension mentioned in the question, else the top
        scope = next((e.member for e in entities if e.dimension == dim
                      and e.member in self.index.parents and e.member != self.index.top[dim]),
                     self.index.top[dim])
        others = [e for e in entities if e.dimension not in (dim, accounts)]
        rank_entities = [*others, Entity(rank_measure, accounts, rank_measure, "rank")]
        rank_mdx, warns = build_mdx(rank_entities, [dim], self.index, self.cube, defaults,
                                    {dim: scope})
        plan.warnings += warns
        extra_measures = [e.member for e in measures if e.member != rank_measure]
        breakdown = next((g for g in group_by if g not in (dim, accounts)), None)
        plan.top_n = {"n": n, "order": order, "dimension": dim, "scope": scope,
                      "measure": rank_measure, "extra_measures": extra_measures,
                      "breakdown": breakdown, "others": [e.__dict__ for e in others]}
        word_order = "highest" if order == "desc" else "lowest"
        rank_args = fit(tool, self._canonical(query=rank_mdx))
        plan.steps.append(Step("rank", f"Rank {scope} children by {rank_measure} ({word_order} first)",
                               tool["name"], rank_args))
        if extra_measures or breakdown:
            plan.steps.append(Step("query", f"Get the numbers for the {n} {'top' if order == 'desc' else 'bottom'} {dim} members",
                                   tool["name"], {"query": "(built from the ranking result)"}))
        plan.tool, plan.arguments = tool["name"], rank_args
        return plan

    @staticmethod
    def _rank_measure(question: str, measures: list[Entity]) -> str | None:
        """'top 3 markets by sales with profit' -> Sales (the measure right after 'by')."""
        for e in measures:
            if re.search(rf"\b(by|in terms of|on)\s+{re.escape(e.text)}\b", question, re.IGNORECASE):
                return e.member
        return measures[0].member if measures else None

    def top_n_followup(self, plan: Plan, winners: list[str]) -> dict:
        """Arguments for the second step of a top-N plan, given the ranked winners."""
        t = plan.top_n
        accounts = next((d for d, ty in self.index.dim_type.items() if ty == "accounts"), None)
        others = [Entity(**o) for o in t["others"]]
        winner_set = "{" + ", ".join(f"[{w}]" for w in winners) + "}"
        if t["breakdown"] and not t["extra_measures"]:
            # one measure, broken down: winners on rows, breakdown members on columns
            cols = f"[{self.index.top[t['breakdown']]}].Children"
            slicer = [f"[{e.member}]" for e in others] + [f"[{t['measure']}]"]
            defaults = self.ctx.get("defaults") or {}
            slicer += [f"[{m}]" for d, m in defaults.items()
                       if d not in (t["dimension"], t["breakdown"], accounts)
                       and d not in {e.dimension for e in others} and d in self.index.top]
            mdx = (f"SELECT {cols} ON COLUMNS, NON EMPTY {winner_set} ON ROWS "
                   f"FROM [{self.cube['application']}].[{self.cube['database']}]")
            if slicer:
                mdx += " WHERE (" + ", ".join(slicer) + ")"
        else:
            if t["breakdown"]:
                plan.warnings.append(f"With several measures, the breakdown by {t['breakdown']} "
                                     "is not applied; showing totals.")
            measures = [t["measure"], *t["extra_measures"]]
            ents = [*others, *[Entity(m, accounts, m, "rank") for m in measures],
                    *[Entity(w, t["dimension"], w, "rank") for w in winners]]
            mdx, warns = build_mdx(ents, [], self.index, self.cube, self.ctx.get("defaults") or {})
            plan.warnings += [w for w in warns if w not in plan.warnings]
        return fit(self.tools[plan.tool], self._canonical(query=mdx))
