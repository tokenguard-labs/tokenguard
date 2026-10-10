"""Data contract shared with Person 1 (orchestrator) and Person 3 (dashboard).

Field names here are FROZEN. Do not rename without notifying both peers.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class Chunk:
    file: str
    start_line: int
    end_line: int
    score: float
    reason: str
    symbol: str = ""
    kind: str = "file"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class TokenStats:
    original: int
    selected: int
    saved: int
    saved_pct: float
    counter: str = "heuristic/chars4"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Redaction:
    file: str
    line: int
    rule: str
    preview: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ContextStats:
    files_scanned: int = 0
    files_considered: int = 0
    files_selected: int = 0
    duration_ms: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ContextRequest:
    query: str
    repo_path: str
    task_type: str = "general"
    max_tokens: int = 4000
    top_k: int = 8
    include_globs: list = field(default_factory=list)
    exclude_globs: list = field(default_factory=list)
    redact_secrets: bool = True
    chunking: str = "symbol"
    expand_deps: bool = True


@dataclass
class ContextResult:
    context_text: str
    selected_files: list
    chunks: list
    tokens: TokenStats
    redactions: list
    stats: ContextStats

    def to_dict(self) -> dict:
        return {
            "context_text": self.context_text,
            "selected_files": list(self.selected_files),
            "chunks": [c.to_dict() if hasattr(c, "to_dict") else dict(c) for c in self.chunks],
            "tokens": self.tokens.to_dict(),
            "redactions": [
                r.to_dict() if hasattr(r, "to_dict") else dict(r) for r in self.redactions
            ],
            "stats": self.stats.to_dict(),
        }
