"""Derivation environments for the repository index.

A content hash answers "did the source change?" but not "did the parser, grammar, resolver or
chunker change?". Derived artifacts are therefore keyed by a *derivation environment* as well:

    derivation_key = H(source_hash || env_digest(kind))

Bumping any component (or setting the matching LOCALPILOT_* env var, used by tests to simulate an
upgrade) changes `env_digest(kind)` and forces a scoped re-derivation of that artifact kind only.
See docs/codebase-intelligence.md §5.1.
"""

from __future__ import annotations

import hashlib
import json
import os

# Component versions. Bump when the code that produces an artifact changes shape/meaning.
EXTRACTION_SCHEMA_VERSION = "3"  # 3: file text retained for corpus-wide signature maps
PARSER_ID = "fallback-regex"
PARSER_VERSION = "1"
GRAMMAR_VERSION = "0"
RESOLVER_VERSION = "1"
CHUNKER_VERSION = "1"
TOKENIZER_ID = "chars4-or-tiktoken"
REDACTOR_VERSION = "1"

# Which components shape which artifact kind.
ARTIFACT_ENV_KEYS = {
    "symbols": ("extraction", "parser", "grammar", "chunker"),
    "edges": ("extraction", "parser", "grammar", "resolver"),
    "embeddings": ("extraction", "parser", "grammar", "chunker", "embedder"),
}


def _versions() -> dict:
    return {
        "extraction": os.getenv("LOCALPILOT_EXTRACTION_VERSION", EXTRACTION_SCHEMA_VERSION),
        "parser": os.getenv("LOCALPILOT_PARSER_ID", PARSER_ID)
        + "@"
        + os.getenv("LOCALPILOT_PARSER_VERSION", PARSER_VERSION),
        "grammar": os.getenv("LOCALPILOT_GRAMMAR_VERSION", GRAMMAR_VERSION),
        "resolver": os.getenv("LOCALPILOT_RESOLVER_VERSION", RESOLVER_VERSION),
        "chunker": os.getenv("LOCALPILOT_CHUNKER_VERSION", CHUNKER_VERSION),
        "tokenizer": os.getenv("LOCALPILOT_TOKENIZER_ID", TOKENIZER_ID),
        "redactor": os.getenv("LOCALPILOT_REDACTOR_VERSION", REDACTOR_VERSION),
        "embedder": os.getenv("CONTEXT_EMBED_MODEL", "nomic-embed-text")
        + "@"
        + os.getenv("CONTEXT_EMBED_BACKEND", "auto"),
    }


def _digest(obj) -> str:
    blob = json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha1(blob).hexdigest()[:16]


def env_for(kind: str) -> dict:
    """The environment components that shape `kind` (all of them for unknown kinds)."""
    versions = _versions()
    keys = ARTIFACT_ENV_KEYS.get(kind)
    if keys is None:
        return versions
    return {k: versions[k] for k in keys}


def env_digest(kind: str) -> str:
    return _digest(env_for(kind))


def source_hash(text: str) -> str:
    """Content hash of normalized source (line endings normalized)."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha1(normalized.encode("utf-8", "ignore")).hexdigest()


def derivation_key(source_hash_value: str, kind: str) -> str:
    return _digest({"source": source_hash_value, "env": env_digest(kind)})
