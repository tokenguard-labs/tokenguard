"""Codebase intelligence engine: persistent incremental index + evidence-grounded context.

Public entry point: `build(repo_path, query, budget, task_type) -> ContextPackage`.

Design of record: docs/codebase-intelligence.md. This package implements Stage 1 (persistent,
derivation-environment-aware index + uncertainty-aware graph) and the retrieval/assembly needed to
produce a package with provenance and an adequacy verdict. Deferred to later stages: tree-sitter
parsers (a versioned parser backend drops in behind `extract`), embeddings/semantic retrieval, the
utility/marginal planner, and local-model assistance.
"""

from .package import Adequacy, ContextPackage, Evidence, build
from .store import CodebaseIndex, SyncReport, default_index_dir

__all__ = [
    "build",
    "CodebaseIndex",
    "SyncReport",
    "default_index_dir",
    "ContextPackage",
    "Evidence",
    "Adequacy",
]
