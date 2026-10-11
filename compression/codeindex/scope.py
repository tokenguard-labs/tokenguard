"""Detect the scope of a request: is it corpus-wide, and which languages does it involve?

"Refactor all the python code here to typescript" is not a top-k relevance query — the scope is the
whole Python corpus, and the target language (TypeScript) conventions matter too. This module turns
such a request into an explicit `TaskScope` so retrieval can build a corpus universe instead of
returning a handful of lexically-matching files.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Set

# Phrases that imply "over the whole repository / corpus", not a single symbol.
CORPUS_MARKERS = (
    "all the", "all files", "all of the", "all our", "all my", "every file", "every api",
    "entire", "whole repo", "whole codebase", "whole project", "throughout", "across the codebase",
    "across the repo", "across the repository", "the codebase", "the repo", "repository",
    "project-wide", "repo-wide", "codebase-wide",
)

# Language name / extension -> canonical language tag (matches store language_of()).
LANGS = {
    "python": "python", "py": "python", "pyi": "python",
    "typescript": "typescript", "ts": "typescript", "tsx": "tsx",
    "javascript": "javascript", "js": "javascript", "jsx": "jsx", "node": "javascript",
    "go": "go", "golang": "go", "rust": "rust", "rs": "rust", "java": "java",
    "ruby": "ruby", "rb": "ruby", "csharp": "other", "c#": "other", "php": "other",
    "kotlin": "other", "kt": "other", "swift": "other", "scala": "other", "sql": "other",
}

# Which canonical languages a target request should pull exemplars from.
TARGET_TAGS = {"typescript": ("typescript", "tsx"), "javascript": ("javascript", "jsx"),
               "python": ("python",), "go": ("go",), "rust": ("rust",), "java": ("java",)}


@dataclass
class TaskScope:
    corpus_wide: bool = False
    source_languages: Set[str] = field(default_factory=set)
    target_languages: Set[str] = field(default_factory=set)
    reason: str = ""

    def as_dict(self) -> dict:
        return {
            "corpus_wide": self.corpus_wide,
            "source_languages": sorted(self.source_languages),
            "target_languages": sorted(self.target_languages),
            "reason": self.reason,
        }

    def target_tags(self) -> Set[str]:
        tags: Set[str] = set()
        for lang in self.target_languages:
            tags.update(TARGET_TAGS.get(lang, (lang,)))
        return tags


def detect_scope(query: str) -> TaskScope:
    ql = (query or "").lower()
    corpus = any(marker in ql for marker in CORPUS_MARKERS)

    targets: Set[str] = set()
    for m in re.finditer(r"\b(?:to|into|as|in)\s+([a-z][a-z+#]*)", ql):
        lang = LANGS.get(m.group(1))
        if lang:
            targets.add(lang)

    mentioned: Set[str] = set()
    for m in re.finditer(r"[a-z][a-z+#]*", ql):
        lang = LANGS.get(m.group(0))
        if lang:
            mentioned.add(lang)

    sources = mentioned - targets
    reason = ""
    if corpus:
        reason = "corpus-wide request"
    if targets:
        reason = (reason + "; " if reason else "") + f"target language: {', '.join(sorted(targets))}"
    return TaskScope(corpus_wide=corpus, source_languages=sources, target_languages=targets,
                     reason=reason)
