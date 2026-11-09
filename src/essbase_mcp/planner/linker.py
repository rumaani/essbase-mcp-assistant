"""Entity linking: find cube members in free text using names, aliases and the glossary."""
from __future__ import annotations

import re
from dataclasses import dataclass

TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-\.&']*")
MAX_NGRAM = 4


@dataclass(frozen=True)
class Entity:
    text: str        # what the user typed
    dimension: str
    member: str      # canonical member name
    via: str         # name | alias | glossary


class MemberIndex:
    def __init__(self, ctx: dict):
        self.dimensions = {d["name"].lower(): d["name"] for d in ctx.get("dimensions", [])}
        self.dim_type = {d["name"]: d.get("type", "regular") for d in ctx.get("dimensions", [])}
        self.top = {d["name"]: next((m["name"] for m in d["members"] if not m.get("parent")),
                                    d["name"]) for d in ctx.get("dimensions", [])}
        self._idx: dict[str, tuple[str, str, str]] = {}
        member_dim = {}
        for d in ctx.get("dimensions", []):
            for m in d.get("members", []):
                member_dim[m["name"]] = d["name"]
                self._idx.setdefault(m["name"].lower(), (d["name"], m["name"], "name"))
                if m.get("alias"):
                    self._idx.setdefault(m["alias"].lower(), (d["name"], m["name"], "alias"))
        for term, member in (ctx.get("glossary") or {}).items():
            if member in member_dim:
                self._idx[term.lower()] = (member_dim[member], member, "glossary")

    def link(self, text: str) -> tuple[list[Entity], list[str]]:
        """Return (entities, group_by_dimensions). Longest match wins."""
        tokens = TOKEN.findall(text)
        low = [t.lower().rstrip(".") for t in tokens]
        entities, group_by, i = [], [], 0
        while i < len(tokens):
            # "by <dimension>" / "per <dimension>" / "for each <dimension>"
            if low[i] in ("by", "per", "each") and i + 1 < len(tokens):
                dim = self.dimensions.get(low[i + 1].rstrip("s")) or self.dimensions.get(low[i + 1])
                if dim:
                    group_by.append(dim)
                    i += 2
                    continue
            for n in range(min(MAX_NGRAM, len(tokens) - i), 0, -1):
                key = " ".join(low[i:i + n])
                if key in self._idx:
                    dim, member, via = self._idx[key]
                    entities.append(Entity(" ".join(tokens[i:i + n]), dim, member, via))
                    i += n
                    break
            else:
                i += 1
        return entities, group_by
