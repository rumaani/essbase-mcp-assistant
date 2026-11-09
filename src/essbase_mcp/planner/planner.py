"""Turn a question into a validated tool call.

pipeline: link entities -> classify intent (rules, or an optional LLM hint)
          -> pick tool from context.intents -> fill args -> validate against inputSchema
The LLM, if configured, may only *suggest* an intent and group-by; members are always
resolved against the context file, so it cannot invent outline members or tool names.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from essbase_mcp.context.args import fit, missing_required
from essbase_mcp.planner.intents import classify
from essbase_mcp.planner.linker import MemberIndex
from essbase_mcp.planner.mdx import build_mdx


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

    @property
    def ok(self) -> bool:
        return self.tool is not None and self.clarification is None

    def to_dict(self) -> dict:
        return asdict(self)


class Planner:
    def __init__(self, ctx: dict, llm=None):
        self.ctx = ctx
        self.index = MemberIndex(ctx)
        self.tools = {t["name"]: t for t in ctx.get("tools", [])}
        self.llm = llm

    def plan(self, question: str) -> Plan:
        entities, group_by, group_parent = self.index.link(question)
        intent, conf = classify(question, bool(entities or group_by))

        if self.llm and conf < 0.8:
            hint = self.llm.suggest(question, self.ctx)
            if hint and hint.get("intent") in self.ctx.get("intents", {}):
                intent, conf = hint["intent"], max(conf, 0.75)
                group_by += [self.index.dimensions[g.lower()] for g in hint.get("group_by", [])
                             if g.lower() in self.index.dimensions and
                             self.index.dimensions[g.lower()] not in group_by]

        plan = Plan(question, intent, conf, entities=[e.__dict__ for e in entities])
        if intent == "unknown":
            plan.clarification = ("I couldn't match that to cube members or an action. Try naming "
                                  "a measure, member or dimension (e.g. 'Sales for East by Year').")
            return plan

        tool_name = self.ctx.get("intents", {}).get(intent)
        if not tool_name or tool_name not in self.tools:
            plan.clarification = f"No tool is mapped to intent '{intent}' in the context file."
            return plan
        tool = self.tools[tool_name]
        cube = self.ctx["cube"]
        canonical = {"application": cube["application"], "database": cube["database"]}

        if intent == "get_members":
            dims = group_by or [e.dimension for e in entities]
            parents = [e.member for e in entities]
            dim = next((d for d in self.index.dimensions.values()
                        if d.lower() in question.lower()), dims[0] if dims else None)
            if not dim:
                plan.clarification = "Which dimension or member should I list members for?"
                return plan
            canonical["dimension"] = dim
            parent = next((p for p in parents if p != self.index.top.get(dim)), None)
            if parent:
                canonical["parent"] = parent
        elif intent == "data_query":
            mdx, warns = build_mdx(entities, group_by, self.index, cube,
                                   self.ctx.get("defaults") or {}, group_parent)
            canonical["query"] = mdx
            plan.warnings += warns
        elif intent == "calculation":
            plan.warnings.append("Calculation changes data; requires admin profile and confirmation.")

        args = fit(tool, canonical)
        missing = missing_required(tool, args)
        if missing:
            plan.clarification = f"Tool {tool_name} needs: {', '.join(missing)}"
            return plan
        plan.tool, plan.arguments = tool_name, args
        return plan
