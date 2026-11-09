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
        # term -> [(dimension, member, via)]; glossary terms may map to several members
        self._idx: dict[str, list[tuple[str, str, str]]] = {}
        self.parents = {m.get("parent") for d in ctx.get("dimensions", [])
                        for m in d.get("members", []) if m.get("parent")}
        member_dim = {}
        for d in ctx.get("dimensions", []):
            for m in d.get("members", []):
                member_dim[m["name"]] = d["name"]
                self._idx.setdefault(m["name"].lower(), [(d["name"], m["name"], "name")])
                if m.get("alias"):
                    self._idx.setdefault(m["alias"].lower(), [(d["name"], m["name"], "alias")])
        for term, target in (ctx.get("glossary") or {}).items():
            targets = target if isinstance(target, list) else [target]
            hits = [(member_dim[t], t, "glossary") for t in targets if t in member_dim]
            if hits:
                self._idx[str(term).lower()] = hits

    def link(self, text: str) -> tuple[list[Entity], list[str], dict[str, str]]:
        """Return (entities, group_by_dimensions, group_parent). Longest match wins.

        "by <dimension>" groups by the dimension's top-level children; "by <member or glossary
        term>" groups by that member's children (e.g. glossary quarter -> Year, "by East").
        """
        tokens = TOKEN.findall(text)
        low = [t.lower().rstrip(".") for t in tokens]
        entities, group_by, group_parent, i = [], [], {}, 0
        while i < len(tokens):
            if low[i] in ("by", "per", "each") and i + 1 < len(tokens):
                nxt = low[i + 1]
                dim = self.dimensions.get(nxt) or self.dimensions.get(nxt.rstrip("s"))
                hits = self._idx.get(nxt) or self._idx.get(nxt.rstrip("s")) or []
                hit = hits[0] if len(hits) == 1 else None
                if dim:
                    group_by.append(dim)
                    i += 2
                    continue
                if hit and hit[1] in self.parents:
                    group_by.append(hit[0])
                    group_parent[hit[0]] = hit[1]
                    i += 2
                    continue
            for n in range(min(MAX_NGRAM, len(tokens) - i), 0, -1):
                key = " ".join(low[i:i + n])
                if key in self._idx:
                    for dim, member, via in self._idx[key]:
                        entities.append(Entity(" ".join(tokens[i:i + n]), dim, member, via))
                    i += n
                    break
            else:
                i += 1
        return entities, group_by, group_parent
