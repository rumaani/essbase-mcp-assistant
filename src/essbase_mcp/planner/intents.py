"""Deterministic intent classification. Order matters: first match wins."""
from __future__ import annotations

import re

RULES: list[tuple[str, re.Pattern]] = [
    ("calculation", re.compile(r"\b(run|execute|launch)\b.*\bcalc|\bcalc(ulation)? script\b|\baggregate\b", re.IGNORECASE)),
    ("list_applications", re.compile(r"\b(list|show|what|which)\b.*\b(applications|apps)\b", re.IGNORECASE)),
    ("list_databases", re.compile(r"\b(list|show|what|which)\b.*\b(databases|cubes)\b", re.IGNORECASE)),
    ("get_dimensions", re.compile(r"\bdimensions\b", re.IGNORECASE)),
    ("get_members", re.compile(r"\b(members|children|descendants|hierarchy)\b|\bwhat('s| is| are)? under\b", re.IGNORECASE)),
]


def classify(text: str, has_entities: bool) -> tuple[str, float]:
    for intent, pattern in RULES:
        if pattern.search(text):
            return intent, 0.9
    if has_entities:
        return "data_query", 0.7
    return "unknown", 0.0
