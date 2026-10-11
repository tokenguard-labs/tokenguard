"""Symbol and reference extraction.

Deterministic and dependency-free: symbols reuse the existing heuristic chunker
(`context_engine.chunker`), references are identifier scans, and imports reuse
`context_engine.imports`. A tree-sitter backend can replace `parse_file` later without changing
the store or resolver; it must bump the parser/grammar version in `env.py` so derived rows
invalidate.
"""

from __future__ import annotations

import ast
import os
import re
from dataclasses import dataclass, field
from typing import List

from ..context_engine import chunker, imports
from ..context_engine.config import ContextConfig
from ..context_engine.selector import STOPWORDS
from ..context_engine.walker import FileInfo

_CFG = ContextConfig()

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
# Module-level assignments (unindented `NAME = ...` / `NAME: T = ...`) are indexed as constants.
_PY_ASSIGN = re.compile(r"^(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*(?::[^=\n]+)?=")
_DYNAMIC = re.compile(
    r"\b(getattr|setattr|globals|locals|eval|exec|__import__|importlib|require_relative)\s*\(|"
    r"\bBeanFactory\b|\bClass\.forName\b"
)

# Tokens that carry no signal even though they are not in STOPWORDS.
_CODE_NOISE = {
    "self", "cls", "this", "args", "kwargs", "true", "false", "none", "null", "undefined",
    "string", "number", "boolean", "void", "public", "private", "protected", "static", "final",
    "const", "let", "var", "func", "function", "return", "import", "export", "default", "from",
    "async", "await", "yield", "raise", "throw", "catch", "finally", "else", "elif", "then",
    "print", "console", "log", "len", "str", "int", "float", "list", "dict", "set", "tuple",
    "type", "interface", "extends", "implements", "object", "value", "values", "items", "keys",
    "name", "path", "data", "result", "response", "request", "error", "message", "status",
}
_MAX_REFS = 64


@dataclass
class Symbol:
    name: str
    kind: str
    start_line: int
    end_line: int
    signature: str
    content: str


@dataclass
class RawRef:
    kind: str          # "imports" | "reference"
    name: str
    line: int = 0


@dataclass
class ParsedFile:
    symbols: List[Symbol] = field(default_factory=list)
    refs: List[RawRef] = field(default_factory=list)
    dynamic: bool = False


def language_of(rel_path: str) -> str:
    ext = os.path.splitext(rel_path)[1].lower()
    return {
        ".py": "python", ".pyi": "python",
        ".ts": "typescript", ".tsx": "tsx", ".js": "javascript", ".jsx": "jsx",
        ".mjs": "javascript", ".cjs": "javascript",
        ".go": "go", ".rs": "rust", ".java": "java",
    }.get(ext, "other")


def _signature(content: str) -> str:
    for line in content.split("\n"):
        stripped = line.strip()
        if stripped and not stripped.startswith(("//", "#", "*", "/*")):
            return stripped[:200]
    first = content.split("\n", 1)[0]
    return first.strip()[:200]


def _python_methods(info: FileInfo) -> List[Symbol]:
    """Methods inside classes are real symbols (e.g. unittest `test_*`), but the heuristic chunker
    only splits top-level definitions. Parse the AST to recover them."""
    try:
        tree = ast.parse(info.text)
    except SyntaxError:
        return []
    lines = info.text.split("\n")
    out: List[Symbol] = []
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        for child in node.body:
            if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            start = child.lineno
            end = getattr(child, "end_lineno", child.lineno)
            content = "\n".join(lines[start - 1:end])
            out.append(Symbol(name=child.name, kind="method", start_line=start, end_line=end,
                              signature=_signature(content), content=content))
    return out


def parse_file(info: FileInfo) -> ParsedFile:
    parsed = ParsedFile()
    parsed.dynamic = bool(_DYNAMIC.search(info.text))

    # Use symbol-level chunking directly: `chunk_file` collapses a single-symbol file into one
    # file-level chunk (good for context, bad for an index). Fall back to it only when the
    # language-specific splitter yields nothing.
    ext = os.path.splitext(info.rel_path)[1].lower()
    chunks = chunker._chunk_python(info) if ext == ".py" else []
    if not chunks:
        chunks = chunker._chunk_braces(info)
    if not chunks:
        chunks = chunker.chunk_file(info, _CFG)

    seen_symbols = set()
    for chunk in chunks:
        name = (chunk.symbol or "").strip()
        if not name or name == "__module__" or name in seen_symbols:
            continue
        seen_symbols.add(name)
        parsed.symbols.append(
            Symbol(
                name=name,
                kind=chunk.kind,
                start_line=chunk.start_line,
                end_line=chunk.end_line,
                signature=_signature(chunk.content),
                content=chunk.content,
            )
        )

    if ext == ".py":
        for symbol in _python_methods(info):
            if symbol.name not in seen_symbols:
                seen_symbols.add(symbol.name)
                parsed.symbols.append(symbol)
        for lineno, line in enumerate(info.text.split("\n"), start=1):
            m = _PY_ASSIGN.match(line)
            if m and m.group("name") not in seen_symbols:
                name = m.group("name")
                seen_symbols.add(name)
                parsed.symbols.append(
                    Symbol(name=name, kind="const", start_line=lineno, end_line=lineno,
                           signature=line.strip()[:200], content=line.rstrip("\n"))
                )

    for spec in imports.extract_imports(info):
        parsed.refs.append(RawRef(kind="imports", name=spec))

    seen_refs = set()
    for match in _IDENT.finditer(info.text):
        token = match.group(0)
        low = token.lower()
        if low in STOPWORDS or low in _CODE_NOISE or token in seen_symbols or token in seen_refs:
            continue
        seen_refs.add(token)
        parsed.refs.append(RawRef(kind="reference", name=token, line=info.text.count("\n", 0, match.start()) + 1))
        if len(seen_refs) >= _MAX_REFS:
            break

    return parsed
