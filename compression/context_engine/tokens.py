"""Token counting with optional tiktoken acceleration.

Uses ``tiktoken`` (cl100k_base) when available; otherwise a characters/4
heuristic. The active counter is reported in the result so Person 3 can label
the dashboard honestly.
"""

from __future__ import annotations

import math


class TokenCounter:
    def __init__(self) -> None:
        self._enc = None
        self.label = "heuristic/chars4"
        self._cache: dict = {}
        try:  # pragma: no cover - depends on environment
            import tiktoken  # type: ignore

            self._enc = tiktoken.get_encoding("cl100k_base")
            self.label = "tiktoken/cl100k_base"
        except Exception:
            self._enc = None

    def count(self, text: str) -> int:
        if not text:
            return 0
        if self._enc is not None:  # pragma: no cover - depends on environment
            return len(self._enc.encode(text, disallowed_special=()))
        return max(1, math.ceil(len(text) / 4))

    def count_file(self, info) -> int:
        key = (info.path, info.mtime, info.size)
        cached = self._cache.get(key)
        if cached is None:
            cached = self.count(info.text)
            self._cache[key] = cached
        return cached


def truncate_to_tokens(
    text: str, max_tokens: int, counter: TokenCounter, suffix: str = "\n// [truncated]"
) -> str:
    """Trim ``text`` line-wise until it fits ``max_tokens`` (binary search)."""
    if counter.count(text) <= max_tokens:
        return text

    lines = text.split("\n")
    lo, hi = 0, len(lines)
    best = 0
    while lo < hi:
        mid = (lo + hi + 1) // 2
        candidate = "\n".join(lines[:mid]) + suffix
        if counter.count(candidate) <= max_tokens:
            best = mid
            lo = mid
        else:
            hi = mid - 1

    if best <= 0:
        return suffix.strip() or "[truncated]"
    return "\n".join(lines[:best]) + suffix
