"""Load/save context files (YAML), one per cube: <context_dir>/<App>.<Db>.yaml.

See docs/CONTEXT_FILE.md for the format.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

import yaml

# Sections a human curates; a rebuild never overwrites them.
CURATED = ("glossary", "intents", "examples", "defaults", "notes")


def load(path: str | Path) -> dict:
    return yaml.safe_load(Path(path).read_text()) or {}


def save(ctx: dict, path: str | Path) -> None:
    """Atomic write: readers (other threads/processes) see the old file or the new one, never
    a half-written one."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(yaml.safe_dump(ctx, sort_keys=False, allow_unicode=True, width=100))
    os.replace(tmp, p)


def path_for(context_dir: str, application: str, database: str) -> Path:
    return Path(context_dir) / f"{application}.{database}.yaml"


def list_saved(context_dir: str) -> list[dict]:
    out = []
    for p in sorted(Path(context_dir).glob("*.yaml")):
        try:
            ctx = load(p)
        except yaml.YAMLError:
            continue
        cube = ctx.get("cube") or {}
        out.append({"cube": f"{cube.get('application')}.{cube.get('database')}",
                    "application": cube.get("application"), "database": cube.get("database"),
                    "generated_at": ctx.get("generated_at"), "file": str(p),
                    "dimensions": len(ctx.get("dimensions", [])),
                    "revision": ctx.get("revision", 1),
                    "glossary_terms": len(ctx.get("glossary") or {})})
    return out


def merge(generated: dict, existing: dict | None) -> dict:
    """Keep curated sections from the existing file; refresh everything else."""
    if not existing:
        return generated
    out = dict(generated)
    out["revision"] = existing.get("revision", 1)
    for key in CURATED:
        if key == "intents":
            # curated mappings win when the tool still exists; new suggestions still appear
            tools = {t["name"] for t in generated.get("tools", [])}
            kept = {k: v for k, v in (existing.get(key) or {}).items() if v in tools}
            out[key] = {**generated.get(key, {}), **kept}
        elif existing.get(key):
            out[key] = existing[key]
    return out


def members_by_dim(ctx: dict) -> dict[str, set[str]]:
    return {d["name"]: {m["name"] for m in d.get("members", [])} for d in ctx.get("dimensions", [])}


def validate_curated(ctx: dict, updates: dict) -> list[str]:
    """Check UI edits against the outline and tool catalog before saving."""
    errors = []
    dims = members_by_dim(ctx)
    known = set().union(*dims.values()) if dims else set()
    for term, target in (updates.get("glossary") or {}).items():
        targets = target if isinstance(target, list) else [target]
        if not str(term).strip():
            errors.append("Glossary terms cannot be blank")
        bad = [t for t in targets if t not in known]
        if bad:
            errors.append(f"Glossary '{term}': not members of the cube: {', '.join(bad)}")
    for dim, member in (updates.get("defaults") or {}).items():
        if dim not in dims:
            errors.append(f"Default: '{dim}' is not a dimension")
        elif member not in dims[dim]:
            errors.append(f"Default: '{member}' is not a member of {dim}")
    tools = {t["name"] for t in ctx.get("tools", [])}
    for intent, tool in (updates.get("intents") or {}).items():
        if tool and tool not in tools:
            errors.append(f"Intent '{intent}': tool '{tool}' is not in the catalog")
    if "notes" in updates and len(updates["notes"] or "") > 20000:
        errors.append("Notes are limited to 20,000 characters")
    return errors
