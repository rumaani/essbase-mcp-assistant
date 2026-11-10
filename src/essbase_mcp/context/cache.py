"""Shared, thread-safe cache of context files for many concurrent users.

- Each cube's context is parsed once and its MemberIndex built once, then shared by every
  request. Entries reload automatically when the file changes on disk (another process,
  a manual edit, or a save).
- Saves are revisioned: the caller passes the revision it edited; if someone saved in the
  meantime the save is refused with Conflict instead of silently overwriting their work.
- Builds and saves are serialized per cube; different cubes don't block each other.
- Members found by search at question time are remembered per cube (bounded), so the
  first user to ask about "Store 117" pays for the lookup and everyone after gets it free.
"""
from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

from essbase_mcp.context import store
from essbase_mcp.planner.linker import MemberIndex

MAX_LEARNED = 5000


class Conflict(Exception):
    def __init__(self, current_revision: int):
        super().__init__(f"Context was changed by someone else (now revision {current_revision}).")
        self.current_revision = current_revision


@dataclass
class CubeContext:
    cube: str
    ctx: dict                      # shared: treat as read-only; save() makes a new entry
    index: MemberIndex
    mtime: float
    # phrase -> [(dimension, member, parent)] ([] = searched, nothing found)
    lookups: OrderedDict = field(default_factory=OrderedDict)
    lock: threading.Lock = field(default_factory=threading.Lock)
    _phrase_locks: dict = field(default_factory=dict)

    def lookup_lock(self, phrase: str) -> threading.Lock:
        """One search per phrase at a time: concurrent askers wait and reuse the result."""
        with self.lock:
            if len(self._phrase_locks) > MAX_LEARNED:
                self._phrase_locks.clear()
            return self._phrase_locks.setdefault(phrase.lower(), threading.Lock())

    @property
    def revision(self) -> int:
        return int(self.ctx.get("revision", 1))

    def cached_lookup(self, phrase: str) -> list | None:
        with self.lock:
            hit = self.lookups.get(phrase.lower())
            if hit is not None:
                self.lookups.move_to_end(phrase.lower())
            return hit

    def remember(self, phrase: str, hits: list[tuple[str, str, str | None]]) -> None:
        with self.lock:
            self.lookups[phrase.lower()] = hits
            self.lookups.move_to_end(phrase.lower())
            while len(self.lookups) > MAX_LEARNED:
                self.lookups.popitem(last=False)
        for dim, member, parent in hits:
            self.index.learn(phrase, dim, member, parent)


class ContextCache:
    def __init__(self, context_dir: str):
        self.context_dir = context_dir
        self._entries: dict[str, CubeContext] = {}
        self._locks: dict[str, threading.RLock] = {}
        self._guard = threading.Lock()

    def lock_for(self, cube: str) -> threading.RLock:
        with self._guard:
            return self._locks.setdefault(cube, threading.RLock())

    def path(self, cube: str) -> Path:
        app, _, db = cube.partition(".")
        return store.path_for(self.context_dir, app, db)

    def get(self, cube: str) -> CubeContext | None:
        path = self.path(cube)
        try:
            mtime = path.stat().st_mtime
        except FileNotFoundError:
            self._entries.pop(cube, None)
            return None
        entry = self._entries.get(cube)
        if entry and entry.mtime == mtime:
            return entry
        with self.lock_for(cube):  # one thread reloads; the rest reuse its result
            entry = self._entries.get(cube)
            if entry and entry.mtime == mtime:
                return entry
            return self._install(cube, store.load(path), mtime, previous=entry)

    def _install(self, cube: str, ctx: dict, mtime: float,
                 previous: CubeContext | None) -> CubeContext:
        ctx = {**ctx, "revision": int(ctx.get("revision", 1))}  # older files have none
        entry = CubeContext(cube, ctx, MemberIndex(ctx), mtime)
        if previous:  # keep what users already looked up
            for phrase, hits in previous.lookups.items():
                entry.remember(phrase, hits)
        self._entries[cube] = entry
        return entry

    def save(self, cube: str, ctx: dict, expected_revision: int | None = None) -> CubeContext:
        path = self.path(cube)
        with self.lock_for(cube):
            current = store.load(path) if path.exists() else None
            current_rev = int(current.get("revision", 1)) if current else 0
            if expected_revision is not None and current and current_rev != expected_revision:
                raise Conflict(current_rev)
            new = {**ctx, "revision": current_rev + 1}
            store.save(new, path)
            return self._install(cube, new, path.stat().st_mtime, self._entries.get(cube))

    def list(self) -> list[dict]:
        return store.list_saved(self.context_dir)
