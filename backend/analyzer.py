"""Rule-based task analyzer: turns a coding request into an explainable complexity estimate.

Each factor produces a 0..1 sub-score; the final routing score is a weighted sum (weights are
configurable heuristics, see `config.COMPLEXITY_WEIGHTS`). The same score is also exposed as a
0..100 complexity number and a simple/medium/complex category, so callers can reason about the
request without treating one weighted number as the whole policy.

Five factors (all 0..1):
    task_type     - how hard the kind of task usually is (complex/medium/simple keyword tiers)
    context_size  - tokens of selected code + attached repo
    files         - files mentioned in the prompt + repo size
    dependency    - cross-file / whole-repo scope (imports, modules, "throughout")
    reasoning     - multi-step reasoning markers + prompt length

The score is floored at `TASK_TYPE_FLOOR * task_type` when strong (complex) keywords matched, so
a clearly hard task with no code attached still routes to the cloud.
"""
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from . import config
from .tokens import count_tokens

# Keywords -> how hard that kind of task usually is (0 = trivial, 1 = needs a strong model)
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
# Localized, single-unit work: not trivial, but bounded to one function/module.
MEDIUM_KEYWORDS = {
    "unit test": 0.50, "write tests": 0.50, "tests for": 0.50, "test the": 0.45,
    "modify": 0.50, "update this function": 0.50, "change this function": 0.50,
    "add error handling": 0.55, "error handling": 0.50, "handle empty": 0.50, "handle null": 0.50,
    "add validation": 0.55, "input validation": 0.55, "validate the input": 0.55,
    "structured": 0.45, "suggest improvements": 0.50, "refactor this function": 0.55,
}
SIMPLE_KEYWORDS = {
    "explain": 0.2, "what does": 0.15, "what is": 0.15, "docstring": 0.1,
    "comment": 0.15, "rename": 0.1, "typo": 0.05, "format": 0.15,
    "syntax": 0.15, "regex": 0.3, "convert": 0.3, "translate": 0.3,
    "type hint": 0.15, "one-liner": 0.1, "simple": 0.15,
    "hello world": 0.0, "list comprehension": 0.1, "print": 0.1,
}
REASONING_MARKERS = [
    "why", "trade-off", "tradeoff", "compare", "best approach", "step by step",
    "should i", "pros and cons", "root cause", "and then", "first", "also",
]
# Cross-file / repository-wide scope, counted separately from the task-type keywords.
DEPENDENCY_MARKERS = {
    "whole codebase": 1.0, "entire codebase": 1.0, "across the codebase": 1.0,
    "across the repo": 0.9, "throughout": 0.8, "everywhere": 0.7,
    "multiple files": 0.8, "cross-module": 0.9, "cross module": 0.9,
    "between modules": 0.8, "dependencies": 0.7, "dependency": 0.6,
    "import graph": 0.9, "end-to-end": 0.7, "integration": 0.6,
}
# Capabilities the local execution path cannot provide (it has no tool/function calling).
TOOL_REQUIRED_MARKERS = [
    "run the tests", "run tests", "execute the", "execute it", "call the api",
    "use a tool", "tool call", "tool-calling", "function call", "terminal",
    "shell command", "npm install", "pip install",
]
FILE_PATTERN = re.compile(r"\b[\w./-]+\.(py|js|ts|tsx|jsx|java|go|rs|cpp|c|h|rb|php|cs|kt|swift|sql|yaml|yml|json)\b")


@dataclass
class Analysis:
    score: float                       # 0..1 routing score (weighted factors, floored)
    factors: Dict[str, float]
    reasons: List[str] = field(default_factory=list)
    task_type: str = "general"
    complexity: str = "medium"         # simple | medium | complex
    complexity_score_100: int = 0      # 0..100 display/decision score


def complexity_category(score_100: int) -> str:
    """Map a 0..100 score to simple | medium | complex using the configured boundaries."""
    if score_100 <= config.COMPLEXITY_SIMPLE_MAX:
        return "simple"
    if score_100 <= config.COMPLEXITY_MEDIUM_MAX:
        return "medium"
    return "complex"


def _task_type_score(prompt_l: str, reasons: List[str]) -> Tuple[float, str]:
    complex_hits = {k: v for k, v in COMPLEX_KEYWORDS.items() if k in prompt_l}
    if complex_hits:
        top = max(complex_hits, key=complex_hits.get)
        reasons.append(f"complex task keywords: {', '.join(sorted(complex_hits))}")
        # Several distinct complex signals compound: +0.05 per extra hit
        score = min(complex_hits[top] + 0.05 * (len(complex_hits) - 1), 1.0)
        simple_hits = {k: v for k, v in SIMPLE_KEYWORDS.items() if k in prompt_l}
        if simple_hits and len(complex_hits) == 1:  # e.g. "explain this race condition" - still hard, but a bit less
            score = (score * 0.75) + (min(simple_hits.values()) * 0.25)
        return score, "complex"
    medium_hits = {k: v for k, v in MEDIUM_KEYWORDS.items() if k in prompt_l}
    if medium_hits:
        reasons.append(f"localized task keywords: {', '.join(sorted(medium_hits))}")
        return max(medium_hits.values()), "medium"
    simple_hits = {k: v for k, v in SIMPLE_KEYWORDS.items() if k in prompt_l}
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


def mentioned_files(prompt: str) -> List[str]:
    return sorted({m.group(0) for m in FILE_PATTERN.finditer(prompt)})


def _files_score(num_mentioned: int, num_repo_files: int, reasons: List[str]) -> float:
    score = min(num_mentioned / 4, 1.0)
    if num_mentioned:
        reasons.append(f"{num_mentioned} file(s) mentioned")
    if num_repo_files > 1:
        # A repo is attached: assume the task may span files, scaled by repo size
        score = max(score, min(num_repo_files / 50, 0.6))
        reasons.append(f"repo attached with {num_repo_files} source files")
    return score


def _dependency_score(prompt_l: str, num_mentioned: int, num_repo_files: int, reasons: List[str]) -> float:
    hits = {k: v for k, v in DEPENDENCY_MARKERS.items() if k in prompt_l}
    score = max(hits.values()) if hits else 0.0
    if hits:
        reasons.append(f"cross-file scope: {', '.join(sorted(hits))}")
    # More affected/available files implies more dependency reasoning, but never a hard rule.
    if num_mentioned >= 3:
        score = max(score, 0.7)
        reasons.append(f"{num_mentioned} files in scope")
    elif num_repo_files > 3:
        score = max(score, min(num_repo_files / 100, 0.5))
    return score


def requires_tools(prompt_l: str) -> bool:
    return any(m in prompt_l for m in TOOL_REQUIRED_MARKERS)


def analyze(
    prompt: str,
    code: str = "",
    repo_context_tokens: int = 0,
    num_repo_files: int = 0,
    context_tokens: Optional[int] = None,
) -> Analysis:
    """Score a request. `context_tokens` overrides the raw code+repo token estimate (used when the
    optimizer has already produced the *relevant* context size)."""
    prompt_l = prompt.lower()
    reasons: List[str] = []

    task_score, task_type = _task_type_score(prompt_l, reasons)
    mentioned = mentioned_files(prompt)
    if context_tokens is None:
        context_tokens = count_tokens(code) + repo_context_tokens
    factors = {
        "task_type": task_score,
        "context_size": _context_size_score(context_tokens, reasons),
        "files": _files_score(len(mentioned), num_repo_files, reasons),
        "dependency": _dependency_score(prompt_l, len(mentioned), num_repo_files, reasons),
        "reasoning": _reasoning_score(prompt, prompt_l, reasons),
    }
    weights = config.COMPLEXITY_WEIGHTS
    total = sum(weights.get(k, 0.0) for k in factors) or 1.0
    score = sum(weights.get(k, 0.0) * v for k, v in factors.items()) / total
    # A clearly hard or localized task keeps a floor even with no code attached (otherwise the
    # empty context/files factors cap a prompt-only request below its category).
    floor_factor = config.TASK_TYPE_FLOOR if task_type == "complex" else (
        config.MEDIUM_TASK_FLOOR if task_type == "medium" else 0.0)
    floor = floor_factor * task_score
    if floor > score:
        score = floor
        reasons.append(f"strong task signal: score raised to {floor_factor} x task_type")
    score = round(min(max(score, 0.0), 1.0), 3)
    score_100 = int(round(score * 100))
    return Analysis(
        score=score,
        factors={k: round(v, 3) for k, v in factors.items()},
        reasons=reasons,
        task_type=task_type,
        complexity=complexity_category(score_100),
        complexity_score_100=score_100,
    )
