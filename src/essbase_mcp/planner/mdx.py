"""Build a simple, valid MDX query from linked members."""
from __future__ import annotations

from collections import OrderedDict

from essbase_mcp.planner.linker import Entity, MemberIndex


def q(member: str) -> str:
    return f"[{member}]"


def build_mdx(entities: list[Entity], group_by: list[str], index: MemberIndex,
              cube: dict, defaults: dict) -> tuple[str, list[str]]:
    warnings: list[str] = []
    by_dim: OrderedDict[str, list[str]] = OrderedDict()
    for e in entities:
        by_dim.setdefault(e.dimension, [])
        if e.member not in by_dim[e.dimension]:
            by_dim[e.dimension].append(e.member)

    accounts = next((d for d, t in index.dim_type.items() if t == "accounts"), "Measures")
    measures = by_dim.pop(accounts, None) or [defaults.get(accounts, index.top.get(accounts))]
    columns = "{" + ", ".join(q(m) for m in measures) + "}"

    rows_dim, rows = None, None
    if group_by:
        rows_dim = group_by[0]
        picked = by_dim.pop(rows_dim, None)
        rows = ("{" + ", ".join(q(m) for m in picked) + "}" if picked
                else f"{q(index.top[rows_dim])}.Children")
    else:
        multi = next((d for d, ms in by_dim.items() if len(ms) > 1), None)
        if multi:
            rows_dim = multi
            rows = "{" + ", ".join(q(m) for m in by_dim.pop(multi)) + "}"

    slicer = []
    for dim, members in by_dim.items():
        if len(members) > 1:
            warnings.append(f"Several {dim} members given; using {members[0]} only.")
        slicer.append(q(members[0]))
    for dim, member in defaults.items():  # e.g. Scenario: Actual
        if dim not in (accounts, rows_dim) and dim not in by_dim and dim in index.top:
            slicer.append(q(member))

    mdx = f"SELECT {columns} ON COLUMNS"
    if rows:
        mdx += f", NON EMPTY {rows} ON ROWS"
    mdx += f" FROM {q(cube['application'])}.{q(cube['database'])}"
    if slicer:
        mdx += " WHERE (" + ", ".join(slicer) + ")"
    return mdx, warnings
