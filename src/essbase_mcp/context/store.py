"""Load/save the context file (YAML). See docs/CONTEXT_FILE.md for the format."""
from __future__ import annotations

from pathlib import Path

import yaml

# Sections a human curates; a rebuild never overwrites them.
CURATED = ("glossary", "intents", "examples", "defaults")


def load(path: str | Path) -> dict:
    return yaml.safe_load(Path(path).read_text()) or {}


def save(ctx: dict, path: str | Path) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump(ctx, sort_keys=False, allow_unicode=True, width=100))


def merge(generated: dict, existing: dict | None) -> dict:
    """Keep curated sections from the existing file; refresh everything else."""
    if not existing:
        return generated
    out = dict(generated)
    for key in CURATED:
        if key == "intents":
            # curated mappings win, but newly suggested intents still appear
            out[key] = {**generated.get(key, {}), **(existing.get(key) or {})}
        elif existing.get(key):
            out[key] = existing[key]
    return out
