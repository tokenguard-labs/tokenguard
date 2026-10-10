"""Stage 4 (refined): import extraction, resolution, and one-hop expansion.

Heuristic but dependency-free. Builds a file -> imported-files graph and finds
direct imports/importers of the seed files so context stays coherent.
"""

from __future__ import annotations

import posixpath
import re

_PY_FROM = re.compile(r"^\s*from\s+([.\w]+)\s+import\b", re.MULTILINE)
_PY_IMPORT = re.compile(r"^\s*import\s+([.\w]+)", re.MULTILINE)
_JS_FROM = re.compile(r"""\bfrom\s+["']([^"']+)["']""")
_JS_REQUIRE = re.compile(r"""\brequire\(\s*["']([^"']+)["']\s*\)""")
_JS_BARE = re.compile(r"""^\s*import\s+["']([^"']+)["']""", re.MULTILINE)

_JS_EXTS = [".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"]
_INDEX = ["index.ts", "index.tsx", "index.js", "index.jsx"]
_PY_EXTS = [".py", ".pyi"]


def extract_imports(info) -> list:
    rel = info.rel_path
    text = info.text
    specs: list = []
    if rel.endswith(".py"):
        specs.extend(_PY_FROM.findall(text))
        for raw in _PY_IMPORT.findall(text):
            specs.append(raw.split(",")[0].strip())
    else:
        specs.extend(_JS_FROM.findall(text))
        specs.extend(_JS_REQUIRE.findall(text))
        specs.extend(_JS_BARE.findall(text))
    seen = set()
    out = []
    for s in specs:
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _resolve_python(spec: str, from_file: str, names: set):
    base = posixpath.dirname(from_file)
    if spec.startswith("."):
        parts = spec.split(".", 1)
        dots = len(spec) - len(spec.lstrip("."))
        remainder = spec[dots:]
        root = base
        for _ in range(max(0, dots - 1)):
            root = posixpath.dirname(root)
        target = posixpath.join(root, remainder.replace(".", "/")) if remainder else root
        candidates = [target + e for e in _PY_EXTS] + [posixpath.join(target, "__init__.py")]
    else:
        target = spec.replace(".", "/")
        candidates = [target + e for e in _PY_EXTS] + [posixpath.join(target, "__init__.py")]
        if "/" not in target:
            # Absolute top-level module: try relative to from_file's directory.
            candidates.append(posixpath.join(base, target) + ".py")
    for cand in candidates:
        cand = posixpath.normpath(cand)
        if cand in names:
            return cand
    return None


def _resolve_js(spec: str, from_file: str, names: set):
    if not (spec.startswith("./") or spec.startswith("../") or spec.startswith("/")):
        return None  # package import; not in-repo
    base = posixpath.dirname(from_file)
    target = posixpath.normpath(posixpath.join(base, spec))
    candidates = [target + e for e in _JS_EXTS]
    candidates += [posixpath.join(target, idx) for idx in _INDEX]
    for cand in candidates:
        cand = posixpath.normpath(cand)
        if cand in names:
            return cand
    return None


def resolve(spec: str, from_file: str, names: set):
    if from_file.endswith(".py"):
        return _resolve_python(spec, from_file, names)
    return _resolve_js(spec, from_file, names)


def build_import_graph(files: list) -> dict:
    names = {f.rel_path for f in files}
    graph = {rel: set() for rel in names}
    for info in files:
        for spec in extract_imports(info):
            target = resolve(spec, info.rel_path, names)
            if target and target != info.rel_path:
                graph[info.rel_path].add(target)
    return graph


def neighbors(seeds, graph: dict, hops: int = 1) -> set:
    found: set = set()
    frontier = set(seeds)
    for _ in range(max(0, hops)):
        nxt: set = set()
        for node in frontier:
            nxt |= graph.get(node, set())
        for node, targets in graph.items():
            if node in found or node in seeds:
                continue
            if any(seed in targets for seed in frontier):
                nxt.add(node)
        nxt -= found
        found |= nxt
        frontier = nxt
    found -= set(seeds)
    return found


def seeds_for(neighbor: str, seeds, graph: dict) -> str:
    """Pick a human-readable seed that motivated including ``neighbor``."""
    for seed in seeds:
        if neighbor in graph.get(seed, set()):
            return seed
    for seed in seeds:
        if seed in graph.get(neighbor, set()):
            return seed
    return next(iter(seeds), "")
