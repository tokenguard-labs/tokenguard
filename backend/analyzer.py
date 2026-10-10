"""Rule-based task analyzer: turns a coding request into a 0..1 complexity score.

Each factor produces a 0..1 sub-score; the final score is a weighted sum.
Every factor also emits a human-readable reason so the UI can explain the route.
"""
import re
from dataclasses import dataclass, field
from typing import Dict, List

from .tokens import count_tokens

WEIGHTS = {
    "task_type": 0.40,
    "reasoning": 0.20,
    "context_size": 0.20,
    "files": 0.20,
}
TASK_TYPE_FLOOR = 0.8  # final score is at least 0.8 x task_type

# Keyword -> how hard that kind of task usually is (0 = trivial, 1 = needs a strong model)
COMPLEX_KEYWORDS = {
    "architecture": 1.0, "architect": 1.0, "design a": 0.9, "system design": 1.0,
    "refactor": 0.8, "migrate": 0.9, "migration": 0.9, "rewrite": 0.7,
    "race condition": 1.0, "deadlock": 1.0, "concurrency": 0.9, "thread-safe": 0.9,
    "memory leak": 0.9, "performance": 0.7, "optimize": 0.7, "scalab": 0.9,
    "security": 0.8, "vulnerab": 0.9, "authentication": 0.7, "auth ": 0.6,
    "debug": 0.6, "bug": 0.6, "fix": 0.5, "not working": 0.6, "crash": 0.6,
    "across": 0.7, "entire": 0.7, "whole codebase": 1.0, "codebase": 0.8,
    "multiple files": 0.9, "end-to-end": 0.8, "implement": 0.6, "feature": 0.6,
}
SIMPLE_KEYWORDS = {
    "explain": 0.2, "what does": 0.15, "what is": 0.15, "docstring": 0.1,
    "comment": 0.15, "rename": 0.1, "typo": 0.05, "format": 0.15,
    "syntax": 0.15, "regex": 0.3, "convert": 0.3, "translate": 0.3,
    "unit test": 0.35, "type hint": 0.15, "one-liner": 0.1, "simple": 0.15,
    "hello world": 0.0, "list comprehension": 0.1, "print": 0.1,
}
REASONING_MARKERS = [
    "why", "trade-off", "tradeoff", "compare", "best approach", "step by step",
    "should i", "pros and cons", "root cause", "and then", "first", "also",
]
FILE_PATTERN = re.compile(r"\b[\w./-]+\.(py|js|ts|tsx|jsx|java|go|rs|cpp|c|h|rb|php|cs|kt|swift|sql|yaml|yml|json)\b")


@dataclass
class Analysis:
    score: float
    factors: Dict[str, float]
    reasons: List[str] = field(default_factory=list)
    task_type: str = "general"


def _task_type_score(prompt_l: str, reasons: List[str]):
    complex_hits = {k: v for k, v in COMPLEX_KEYWORDS.items() if k in prompt_l}
    simple_hits = {k: v for k, v in SIMPLE_KEYWORDS.items() if k in prompt_l}
    if complex_hits:
        top = max(complex_hits, key=complex_hits.get)
        reasons.append(f"complex task keywords: {', '.join(sorted(complex_hits))}")
        # Several distinct complex signals compound: +0.05 per extra hit
        score = min(complex_hits[top] + 0.05 * (len(complex_hits) - 1), 1.0)
        if simple_hits and len(complex_hits) == 1:  # e.g. "explain this race condition" - still hard, but a bit less
            score = (score * 0.75) + (min(simple_hits.values()) * 0.25)
        return score, "complex"
    if simple_hits:
        reasons.append(f"simple task keywords: {', '.join(sorted(simple_hits))}")
        return min(simple_hits.values()), "simple"
    reasons.append("no strong task keywords, assuming medium")
    return 0.45, "general"


def _reasoning_score(prompt: str, prompt_l: str, reasons: List[str]) -> float:
    markers = [m for m in REASONING_MARKERS if m in prompt_l]
    words = len(prompt.split())
    length_score = min(words / 120, 1.0)  # long requests usually mean multi-step work
    marker_score = min(len(markers) / 3, 1.0)
    score = max(length_score, marker_score)
    if markers:
        reasons.append(f"reasoning markers: {', '.join(markers)}")
    if words > 60:
        reasons.append(f"long request ({words} words)")
    return score


def _context_size_score(context_tokens: int, reasons: List[str]) -> float:
    # ~0 tokens -> 0, 8k+ tokens -> 1 (a 7B local model struggles with big contexts)
    score = min(context_tokens / 8000, 1.0)
    if context_tokens:
        reasons.append(f"context size ~{context_tokens} tokens")
    return score


def _files_score(prompt: str, num_repo_files: int, reasons: List[str]) -> float:
    mentioned = set(m.group(0) for m in FILE_PATTERN.finditer(prompt))
    n = max(len(mentioned), 0)
    score = min(n / 4, 1.0)
    if mentioned:
        reasons.append(f"{n} file(s) mentioned")
    if num_repo_files > 1:
        # A repo is attached: assume the task may span files, scaled by repo size
        score = max(score, min(num_repo_files / 50, 0.6))
        reasons.append(f"repo attached with {num_repo_files} source files")
    return score


def analyze(prompt: str, code: str = "", repo_context_tokens: int = 0, num_repo_files: int = 0) -> Analysis:
    prompt_l = prompt.lower()
    reasons: List[str] = []

    task_score, task_type = _task_type_score(prompt_l, reasons)
    factors = {
        "task_type": task_score,
        "reasoning": _reasoning_score(prompt, prompt_l, reasons),
        "context_size": _context_size_score(count_tokens(code) + repo_context_tokens, reasons),
        "files": _files_score(prompt, num_repo_files, reasons),
    }
    score = sum(WEIGHTS[k] * v for k, v in factors.items())
    # A clearly hard task stays hard even with no code attached (otherwise the
    # empty context/files factors cap a prompt-only request at 0.6 -> never cloud)
    floor = TASK_TYPE_FLOOR * task_score
    if task_type == "complex" and floor > score:
        score = floor
        reasons.append(f"strong task signal: score raised to {TASK_TYPE_FLOOR} x task_type")
    return Analysis(
        score=round(score, 3),
        factors={k: round(v, 3) for k, v in factors.items()},
        reasons=reasons,
        task_type=task_type,
    )
