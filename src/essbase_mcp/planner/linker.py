"""Entity linking: find cube members in free text using names, aliases and the glossary."""
from __future__ import annotations

import re
from dataclasses import dataclass

TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-\.&']*")
MAX_NGRAM = 4


# Words that never name a member; leftover words outside this list may be looked up.
STOPWORDS = {"a", "an", "the", "and", "or", "of", "for", "in", "on", "at", "to", "from", "with", "without", "by", "per", "each", "vs", "versus", "than", "as", "is", "are", "was", "were", "be", "been", "being", "what", "whats", "which", "who", "how", "much", "many", "show", "list", "give", "get", "tell", "me", "us", "our", "my", "please", "can", "could", "would", "will", "do", "does", "did", "value", "values", "number", "numbers", "total", "totals", "amount", "amounts", "all", "any", "some", "this", "that", "these", "those", "it", "its", "there", "their", "them", "top", "bottom", "best", "worst", "highest", "lowest", "first", "last", "next", "prior", "previous", "compare", "compared", "year-over-year", "breakdown", "split", "across", "children", "child", "members", "member", "descendants", "hierarchy", "dimensions", "dimension", "under", "below", "above", "data", "cube", "report", "figures", "figure", "performance", "results", "result", "about", "where", "when", "sum"}


@dataclass
class LinkResult:
    entities: list
    group_by: list
    group_parent: dict
    unresolved: list  # name-like phrases that matched nothing (candidates for a lookup)


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

    def learn(self, phrase: str, dimension: str, member: str, parent: str | None = None) -> None:
        """Add a member found by search at question time (shared by all users of this cube).
        Single dict assignments are atomic, so concurrent readers never see a partial update."""
        hit = (dimension, member, "lookup")
        if phrase.lower() != member.lower():
            existing = [h for h in self._idx.get(phrase.lower(), []) if h[2] == "lookup"]
            self._idx[phrase.lower()] = [*existing, hit] if hit not in existing else existing
        self._idx.setdefault(member.lower(), [hit])
        if parent:
            self.parents = self.parents | {parent}

    def link(self, text: str) -> tuple[list[Entity], list[str], dict[str, str]]:
        """Return (entities, group_by_dimensions, group_parent). Longest match wins.

        "by <dimension>" groups by the dimension's top-level children; "by <member or glossary
        term>" groups by that member's children (e.g. glossary quarter -> Year, "by East").
        """
        quoted = re.findall(r'"([^"]+)"|\u201c([^\u201d]+)\u201d', text)
        quoted = [a or b for a, b in quoted]
        tokens = TOKEN.findall(text)
        low = [t.lower().rstrip(".") for t in tokens]
        used = [False] * len(tokens)
        entities, group_by, group_parent, i = [], [], {}, 0
        while i < len(tokens):
            if self._is_dimension_word(low[i]):
                used[i] = True
            if low[i] in ("by", "per", "each") and i + 1 < len(tokens):
                nxt = low[i + 1]
                dim = self.dimensions.get(nxt) or self.dimensions.get(nxt.rstrip("s"))
                hits = self._idx.get(nxt) or self._idx.get(nxt.rstrip("s")) or []
                hit = hits[0] if len(hits) == 1 else None
                if dim:
                    group_by.append(dim)
                    used[i] = used[i + 1] = True
                    i += 2
                    continue
                if hit and hit[1] in self.parents:
                    group_by.append(hit[0])
                    group_parent[hit[0]] = hit[1]
                    used[i] = used[i + 1] = True
                    i += 2
                    continue
            for n in range(min(MAX_NGRAM, len(tokens) - i), 0, -1):
                key = " ".join(low[i:i + n])
                if key in self._idx:
                    for dim, member, via in self._idx[key]:
                        entities.append(Entity(" ".join(tokens[i:i + n]), dim, member, via))
                    used[i:i + n] = [True] * n
                    i += n
                    break
            else:
                i += 1
        return LinkResult(entities, group_by, group_parent,
                          self._unresolved(tokens, low, used, quoted))

    def _is_dimension_word(self, word: str) -> bool:
        return (word in self.dimensions or word.rstrip("s") in self.dimensions
                or word.removesuffix("es") in self.dimensions)

    def _unresolved(self, tokens, low, used, quoted) -> list[str]:
        """Runs of unmatched, non-stopword tokens that look like names (have a capital letter
        or a digit), plus anything in quotes that matched nothing."""
        out = [q.strip() for q in quoted if q.strip().lower() not in self._idx]
        run: list[int] = []
        for i in range(len(tokens) + 1):
            free = i < len(tokens) and not used[i] and low[i] not in STOPWORDS
            if free:
                run.append(i)
                continue
            if run:
                words = [tokens[j] for j in run][:4]
                phrase = " ".join(words)
                name_like = any(w[0].isupper() or any(c.isdigit() for c in w) for w in words)
                if name_like and not phrase.isdigit() and phrase not in out and not any(
                        phrase.lower() in q.lower() for q in out):
                    out.append(phrase)
                run = []
        return out[:3]
