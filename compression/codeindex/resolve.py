"""Uncertainty-aware edge resolution.

A syntactic reference is not a runtime dependency. Edges start as `syntactic` (line 0) and are
promoted to `resolved` only when the index can ground them, with a confidence and a human-readable
basis:

    resolved  1.0  import target          (module import resolved to a repo file)
    resolved  0.9  same-file def          (name defined in the same file)
    resolved  0.7  imported def           (name defined in a file this file imports)
    resolved  0.6  unique global def      (exactly one definition repo-wide)
    syntactic 0.3  ambiguous name         (several definitions; left as a candidate, not a fact)

Edges that resolve to nothing (external packages, unknown names) are removed, so the graph never
asserts a dependency it cannot ground. This is the honesty contract from docs §5.4.
"""

from __future__ import annotations

import sqlite3
from typing import Dict, Set

from ..context_engine import imports


def resolve_all(con: sqlite3.Connection) -> None:
    known_paths: Set[str] = {r["path"] for r in con.execute("SELECT path FROM files")}
    names: Dict[str, Set[str]] = {}
    for r in con.execute("SELECT name, path FROM symbols"):
        names.setdefault(r["name"], set()).add(r["path"])

    # Phase 1: imports -> resolved target paths (also builds the per-file import map).
    imported: Dict[str, Set[str]] = {}
    for e in list(con.execute("SELECT id, src_path, dst_name FROM edges WHERE kind='imports'")):
        target = imports.resolve(e["dst_name"], e["src_path"], known_paths)
        if target and target != e["src_path"]:
            imported.setdefault(e["src_path"], set()).add(target)
            con.execute(
                "UPDATE edges SET dst_path=?, resolution='resolved', confidence=1.0, basis='import target'"
                " WHERE id=?", (target, e["id"]))
        else:
            con.execute("DELETE FROM edges WHERE id=?", (e["id"],))

    # Phase 2: references.
    for e in list(con.execute(
            "SELECT id, src_path, dst_name FROM edges WHERE kind!='imports'")):
        defs = names.get(e["dst_name"], set())
        src = e["src_path"]
        if not defs:
            con.execute("DELETE FROM edges WHERE id=?", (e["id"],))
        elif src in defs:
            con.execute(
                "UPDATE edges SET dst_path=?, resolution='resolved', confidence=0.9, basis='same-file def'"
                " WHERE id=?", (src, e["id"]))
        elif len(defs) == 1:
            con.execute(
                "UPDATE edges SET dst_path=?, resolution='resolved', confidence=0.6, basis='unique global def'"
                " WHERE id=?", (next(iter(defs)), e["id"]))
        else:
            via_import = defs & imported.get(src, set())
            if len(via_import) == 1:
                con.execute(
                    "UPDATE edges SET dst_path=?, resolution='resolved', confidence=0.7, basis='imported def'"
                    " WHERE id=?", (next(iter(via_import)), e["id"]))
            else:
                # Several definitions: record it as a candidate, never a resolved dependency.
                con.execute(
                    "UPDATE edges SET dst_path=NULL, resolution='syntactic', confidence=0.3,"
                    " basis='ambiguous name' WHERE id=?", (e["id"],))
