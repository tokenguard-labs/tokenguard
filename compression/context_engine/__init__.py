"""LocalPilot Context & Token Optimization Engine (Person 2).

Pure function component:

    (query, repo_path, budget) -> (context_text, metadata)

No LLM calls, deterministic, stdlib-only core.
"""

from .config import ContextConfig
from .engine import ContextEngine
from .models import (
    Chunk,
    ContextRequest,
    ContextResult,
    ContextStats,
    Redaction,
    TokenStats,
)
from .tokens import TokenCounter

__all__ = [
    "ContextConfig",
    "ContextEngine",
    "ContextRequest",
    "ContextResult",
    "ContextStats",
    "TokenCounter",
    "Chunk",
    "Redaction",
    "TokenStats",
    "walk_repo",
    "select",
    "redact",
]


def walk_repo(*args, **kwargs):
    from .walker import walk_repo as _walk_repo

    return _walk_repo(*args, **kwargs)


def select(*args, **kwargs):
    from .selector import select as _select

    return _select(*args, **kwargs)


def redact(*args, **kwargs):
    from .redact import redact as _redact

    return _redact(*args, **kwargs)
