"""Adapter: Person 2's context engine behind Person 1's frozen contract.

Contract: compression.optimize_context(prompt, repo_path, code, token_budget)
          -> backend.contracts.ContextResult
          compression.redact_text(text) -> (redacted_text, count)

The heavy lifting lives in ./context_engine (the "Person 2" implementation).
This module only maps inputs, formats the output, enforces the token budget with
Person 1's counter, and redacts secrets.
"""
from __future__ import annotations

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

OPTIMIZER_NAME = "person2-v1"

_ENGINE = ContextEngine()

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
) -> ContextResult:
    code = code or ""
    budget = max(0, int(token_budget))
    has_repo = bool(repo_path) and os.path.isdir(repo_path)

    repo_context = ""
    files_selected: List[str] = []
    repo_files: list = []
    original_tokens = count_tokens(code)

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
            try:
                result = _ENGINE.build(
                    ContextRequest(
                        query=_query_from(prompt or "", code),
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
                repo_context = ""
                files_selected = []

    # Selected editor code is always included, first.
    combined = code
    if repo_context:
        combined = f"{code}\n\n{repo_context}" if code.strip() else repo_context

    redacted, redactions = _redact(combined) if combined else ("", [])
    # Engine emits "// file: <path>"; the contract wants "# File: <path>".
    context = redacted.replace("// file: ", "# File: ")
    context = _truncate_to_budget(context, budget)

    return ContextResult(
        context=context,
        original_tokens=original_tokens,
        optimized_tokens=count_tokens(context),
        files_selected=files_selected,
        secrets_redacted=len(redactions),
        redaction_details=[f"{r.rule} in {r.file}:{r.line}" for r in redactions],
        optimizer=OPTIMIZER_NAME,
    )


def redact_text(text: str) -> Tuple[str, int]:
    """Mask secrets in free text (the prompt). Never returns the secret itself."""
    if not text:
        return text, 0
    redacted, redactions = _redact(text)
    return redacted, len(redactions)
