"""Stage 2 (refined): split files into symbol-level chunks.

Strategy:
- Python: real AST symbols (functions / classes) plus a header chunk.
- Other languages: blank-line paragraphs merged by brace depth, which yields
  one chunk per top-level function/class/const for conventionally formatted code.
- Fallback: fixed line windows.

This is intentionally heuristic and dependency-free.
"""

from __future__ import annotations

import ast
import os
import re
from dataclasses import dataclass

from .config import ContextConfig
from .walker import FileInfo


@dataclass
class CodeChunk:
    file: str
    start_line: int
    end_line: int
    symbol: str
    kind: str
    content: str


_SYMBOL_FN = re.compile(r"\b(?:function|class|interface|type|enum)\s+(\w+)")
_SYMBOL_VAR = re.compile(r"\b(?:const|let|var)\s+(\w+)")
_SYMBOL_METHOD = re.compile(r"^\s*(?:async\s+)?(\w+)\s*\(")


def _first_code_line(content: str) -> str:
    """Skip leading comment/blank lines so symbols after a doc comment resolve."""
    for line in content.split("\n"):
        stripped = line.strip()
        if not stripped:
            continue
        if (
            stripped.startswith("//")
            or stripped.startswith("*")
            or stripped.startswith("/*")
            or stripped.startswith("#")
        ):
            continue
        return line
    lines = content.split("\n", 1)
    return lines[0] if lines else ""


def _kind_from_first(first: str) -> str:
    if re.search(r"\bclass\s+\w+", first):
        return "class"
    if re.search(r"\binterface\s+\w+", first):
        return "interface"
    if re.search(r"\b(?:type|enum)\s+\w+", first):
        return "type"
    if re.search(r"\b(?:function|def)\s+\w+", first):
        return "function"
    if re.search(r"\b(?:const|let|var)\s+\w+", first):
        return "const"
    return "block"


def _symbol_from_first(first: str) -> str:
    for rx in (_SYMBOL_FN, _SYMBOL_VAR):
        m = rx.search(first)
        if m:
            return m.group(1)
    m = _SYMBOL_METHOD.match(first.strip())
    if m:
        return m.group(1)
    return ""


def _brace_delta(text: str) -> int:
    """Net brace balance, ignoring comments and string/template literals."""
    depth = 0
    in_block_comment = False
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        two = text[i : i + 2]
        if in_block_comment:
            if two == "*/":
                in_block_comment = False
                i += 2
                continue
            i += 1
            continue
        if two == "//":
            j = text.find("\n", i)
            i = n if j == -1 else j
            continue
        if two == "/*":
            in_block_comment = True
            i += 2
            continue
        if c in "\"'`":
            quote = c
            i += 1
            while i < n:
                if text[i] == "\\":
                    i += 2
                    continue
                if text[i] == quote:
                    i += 1
                    break
                i += 1
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        i += 1
    return depth


def _paragraphs(text: str) -> list:
    lines = text.split("\n")
    paras = []
    current = []
    start = 1
    for idx, line in enumerate(lines, start=1):
        if line.strip() == "":
            if current:
                paras.append((start, idx - 1, "\n".join(current)))
                current = []
        else:
            if not current:
                start = idx
            current.append(line)
    if current:
        paras.append((start, len(lines), "\n".join(current)))
    return paras


def _chunk_python(info: FileInfo) -> list:
    try:
        tree = ast.parse(info.text)
    except SyntaxError:
        return []
    lines = info.text.split("\n")
    nodes = [
        n
        for n in tree.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    ]
    chunks: list = []
    cursor = 0
    for node in nodes:
        start = node.lineno - 1
        if start > cursor:
            header = "\n".join(lines[cursor:start]).strip("\n")
            if header.strip():
                chunks.append(
                    CodeChunk(
                        info.rel_path,
                        cursor + 1,
                        start,
                        "" if chunks else "__module__",
                        "header" if chunks else "module",
                        header,
                    )
                )
        end = getattr(node, "end_lineno", node.lineno)
        content = "\n".join(lines[start:end])
        chunks.append(
            CodeChunk(
                info.rel_path,
                node.lineno,
                end,
                node.name,
                "class" if isinstance(node, ast.ClassDef) else "function",
                content,
            )
        )
        cursor = end
    trailing = "\n".join(lines[cursor:]).strip("\n")
    if trailing.strip():
        chunks.append(
            CodeChunk(info.rel_path, cursor + 1, len(lines), "__module__", "module", trailing)
        )
    return chunks


def _chunk_braces(info: FileInfo) -> list:
    paras = _paragraphs(info.text)
    if not paras:
        return []

    segments = []
    buf_start = buf_end = None
    buf_text: list = []
    depth = 0
    for start, end, content in paras:
        if buf_start is None:
            buf_start, buf_end, buf_text = start, end, [content]
        else:
            buf_end = end
            buf_text.append(content)
        depth += _brace_delta(content)
        if depth <= 0:
            segments.append((buf_start, buf_end, "\n".join(buf_text)))
            buf_start = buf_end = None
            buf_text = []
            depth = 0
    if buf_start is not None:
        segments.append((buf_start, buf_end, "\n".join(buf_text)))

    chunks: list = []
    for start, end, content in segments:
        first = _first_code_line(content).strip()
        chunks.append(
            CodeChunk(
                info.rel_path,
                start,
                end,
                _symbol_from_first(first),
                _kind_from_first(first),
                content,
            )
        )
    return chunks


def _chunk_window(info: FileInfo, cfg: ContextConfig) -> list:
    lines = info.text.split("\n")
    total = len(lines)
    window = max(20, cfg.window_lines)
    overlap = max(0, min(cfg.window_overlap, window - 1))
    step = max(1, window - overlap)
    chunks: list = []
    start = 0
    while start < total:
        end = min(total, start + window)
        content = "\n".join(lines[start:end])
        chunks.append(
            CodeChunk(info.rel_path, start + 1, end, "", "window", content)
        )
        if end >= total:
            break
        start += step
    return chunks


def chunk_file(info: FileInfo, cfg: ContextConfig) -> list:
    """Return symbol/line chunks covering a file. Never empty for text files."""
    if not info.text.strip():
        return []

    ext = os.path.splitext(info.rel_path)[1].lower()
    chunks: list = []
    if ext == ".py":
        chunks = _chunk_python(info)
    if not chunks:
        chunks = _chunk_braces(info)

    # A lone balanced segment covering the whole file is just file-level.
    if len(chunks) <= 1:
        chunks = [
            CodeChunk(info.rel_path, 1, max(1, info.line_count), "", "file", info.text)
        ]

    # Safety: split any oversized chunk into windows.
    final: list = []
    for c in chunks:
        if (c.end_line - c.start_line + 1) > cfg.max_chunk_lines:
            lines = c.content.split("\n")
            # Reuse window splitter on the slice.
            tmp = FileInfo(
                path=info.path,
                rel_path=info.rel_path,
                size=len(c.content),
                mtime=info.mtime,
                text=c.content,
                line_count=len(lines),
            )
            for w in _chunk_window(tmp, cfg):
                final.append(
                    CodeChunk(
                        c.file,
                        c.start_line + w.start_line - 1,
                        c.start_line + w.end_line - 1,
                        c.symbol,
                        c.kind,
                        w.content,
                    )
                )
        else:
            final.append(c)
    return final


def chunk_repo(files: list, cfg: ContextConfig) -> list:
    chunks: list = []
    for info in files:
        chunks.extend(chunk_file(info, cfg))
    return chunks


def compress_chunk(chunk: CodeChunk, head_lines: int = 4) -> CodeChunk:
    """Return a cheap summary of a chunk (body elided).

    Used by the coverage pass so a relevant file can be represented when its
    full body would not fit. Functions/classes keep their declaration line;
    module/header chunks keep their first few lines (imports are informative).
    """
    if chunk.kind in ("header", "module"):
        lines = chunk.content.split("\n")
        kept = lines[:head_lines]
        marker = "  # ... (module summary)"
        content = "\n".join(kept) + "\n" + marker
    elif chunk.kind == "const":
        signature = _first_code_line(chunk.content).split("=", 1)[0].rstrip()
        content = f"{signature}\n  # value elided to fit token budget"
    elif chunk.kind == "file":
        lines = chunk.content.split("\n")
        kept = lines[:head_lines]
        content = "\n".join(kept) + "\n  # ... (file summary)"
    else:
        signature = _first_code_line(chunk.content).rstrip()
        content = f"{signature}\n    ...  # body elided to fit token budget"
    return CodeChunk(
        file=chunk.file,
        start_line=chunk.start_line,
        end_line=chunk.end_line,
        symbol=chunk.symbol,
        kind=f"{chunk.kind}+sig",
        content=content,
    )
