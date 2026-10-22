"""Adapter: Person 2's context engine behind Person 1's frozen contract.

Contract: compression.optimize_context(prompt, repo_path, code, token_budget)
          -> backend.contracts.ContextResult
          compression.redact_text(text) -> (redacted_text, count)

The heavy lifting lives in ./context_engine (the "Person 2" implementation).
This module only maps inputs, formats the output, enforces the token budget with
Person 1's counter, and redacts secrets.
"""
from __future__ import annotations

import logging
import os
import re
from collections import Counter
from typing import List, Optional, Tuple

from backend.contracts import ContextResult
from backend.tokens import count_tokens  # same counter as the router/UI

from .context_engine import ContextEngine, ContextRequest
from .context_engine.config import ContextConfig
from .context_engine.redact import redact as _redact
from .context_engine.walker import walk_repo

log = logging.getLogger("localpilot.context")

OPTIMIZER_NAME = "person2-v1"
INDEXED_OPTIMIZER_NAME = "codeindex-v1"

_ENGINE = ContextEngine()


def _index_enabled() -> bool:
    return os.getenv("CONTEXT_INDEX_ENABLED", "true").lower() in ("1", "true", "yes")


def _indexed_context(query: str, repo_path: str, budget: int, *, assist: bool = True,
                     semantic: bool = True):
    """Repo context from the persistent index, or None to fall back to the in-memory engine."""
    if not _index_enabled():
        return None
    try:
        from . import codeindex

        return codeindex.build(
            repo_path,
            query,
            budget=budget,
            index_dir=os.getenv("CONTEXT_INDEX_DIR") or None,
            min_confidence=float(os.getenv("CONTEXT_INDEX_MIN_CONFIDENCE", "0.5")),
            assist_llm=assist,
            semantic=semantic,
        )
    except Exception:
        log.exception("codeindex optimizer failed; using the in-memory context engine")
        return None

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]{3,}")
_CODE_NOISE = {
    "self", "this", "return", "import", "from", "const", "function", "class", "def", "async",
    "await", "export", "default", "true", "false", "none", "null", "undefined", "string",
    "number", "boolean", "void", "public", "private", "static", "else", "elif", "while",
    "with", "lambda", "yield", "pass", "break", "continue", "throw", "catch", "finally",
    "print", "console", "file", "type", "interface", "extends", "implements", "require",
}
_MAX_CODE_TERMS = 25


def _query_from(prompt: str, code: str) -> str:
    """The engine ranks files lexically, so a vague prompt ("explain this") has no terms and
    would fall back to the most recently modified files. Add the most frequent identifiers of
    the attached code so the files related to that code are selected instead."""
    if not code.strip():
        return prompt
    counts = Counter(t for t in _IDENT.findall(code) if t.lower() not in _CODE_NOISE)
    terms = [t for t, _ in counts.most_common(_MAX_CODE_TERMS)]
    return f"{prompt} {' '.join(terms)}".strip()


def _keyword_fallback(query: str, repo_files, budget: int) -> Tuple[str, List[str]]:
    """Naive lexical selection over the walked repo files.

    Last-resort context when both the indexed and the in-memory engines fail: without it the
    router would answer with zero repo context and the LLM would guess ("random" answers).
    """
    if not repo_files or budget <= 0:
        return "", []
    terms = [t.lower() for t in _IDENT.findall(query) if t.lower() not in _CODE_NOISE][:_MAX_CODE_TERMS]

    def score(f) -> int:
        text = f.text.lower()
        path = f.rel_path.lower()
        return sum(text.count(t) + 5 * path.count(t) for t in terms)

    ranked = sorted(repo_files, key=score, reverse=True)
    best = score(ranked[0]) if ranked else 0
    if best == 0:
        ranked = sorted(repo_files, key=lambda f: f.rel_path)  # vague prompt: any context beats none

    parts: List[str] = []
    selected: List[str] = []
    used = 0
    for f in ranked:
        block = f"# File: {f.rel_path}\n{f.text}"
        cost = count_tokens(block)
        if used + cost > budget:
            continue
        used += cost
        parts.append(block)
        selected.append(f.rel_path)
    return "\n\n".join(parts), selected


def _truncate_to_budget(text: str, budget: int) -> str:
    """Trim from the end until count_tokens(text) <= budget (binary search)."""
    if budget <= 0:
        return ""
    if count_tokens(text) <= budget:
        return text
    lines = text.split("\n")
    lo, hi, best = 0, len(lines), 0
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if count_tokens("\n".join(lines[:mid])) <= budget:
            best = mid
            lo = mid
        else:
            hi = mid - 1
    return "\n".join(lines[:best])


def optimize_context(
    prompt: str,
    repo_path: Optional[str],
    code: str = "",
    token_budget: int = 3000,
    *,
    assist: bool = True,
    semantic: bool = True,
) -> ContextResult:
    code = code or ""
    budget = max(0, int(token_budget))
    has_repo = bool(repo_path) and os.path.isdir(repo_path)

    repo_context = ""
    files_selected: List[str] = []
    repo_files: list = []
    original_tokens = count_tokens(code)
    secrets_redacted = 0
    redaction_details: List[str] = []
    optimizer = OPTIMIZER_NAME
    metadata: dict = {}

    if has_repo:
        try:
            repo_files = walk_repo(repo_path, ContextConfig())
        except Exception:
            repo_files = []
        original_tokens += sum(
            count_tokens(f"# File: {f.rel_path}\n{f.text}") for f in repo_files
        )

        code_tokens = count_tokens(code)
        repo_budget = max(0, budget - code_tokens - 8)  # leave room for the separator
        if repo_files and repo_budget > 0:
            query = _query_from(prompt or "", code)
            package = _indexed_context(query, repo_path, repo_budget, assist=assist, semantic=semantic)
            if package is not None:
                repo_context = package.context_text
                files_selected = list(package.files_selected)
                secrets_redacted = len(package.redactions)
                redaction_details = list(package.redactions)
                optimizer = INDEXED_OPTIMIZER_NAME
                metadata = {
                    **package.router_view(),
                    "optimizer": INDEXED_OPTIMIZER_NAME,
                    "evidence": [e.to_dict() for e in package.items[:40]],
                }
            else:
                try:
                    result = _ENGINE.build(
                        ContextRequest(
                            query=query,
                            repo_path=repo_path,
                            top_k=8,
                            max_tokens=repo_budget,
                            chunking="symbol",
                            expand_deps=True,
                            redact_secrets=False,  # redact once below, over code + repo
                        )
                    )
                    repo_context = result.context_text
                    files_selected = list(result.selected_files)
                except Exception:
                    log.exception("context engine failed; using the keyword fallback")
                    repo_context, files_selected = _keyword_fallback(query, repo_files, repo_budget)

    # Selected editor code is always included, first.
    combined = code
    if repo_context:
        combined = f"{code}\n\n{repo_context}" if code.strip() else repo_context

    redacted, redactions = _redact(combined) if combined else ("", [])
    secrets_redacted += len(redactions)
    redaction_details += [f"{r.rule} in {r.file}:{r.line}" for r in redactions]
    # Engine emits "// file: <path>"; the contract wants "# File: <path>".
    context = redacted.replace("// file: ", "# File: ")
    context = _truncate_to_budget(context, budget)

    return ContextResult(
        context=context,
        original_tokens=original_tokens,
        optimized_tokens=count_tokens(context),
        files_selected=files_selected,
        secrets_redacted=secrets_redacted,
        redaction_details=redaction_details,
        optimizer=optimizer,
        metadata=metadata,
    )


def redact_text(text: str) -> Tuple[str, int]:
    """Mask secrets in free text (the prompt). Never returns the secret itself."""
    if not text:
        return text, 0
    redacted, redactions = _redact(text)
    return redacted, len(redactions)
