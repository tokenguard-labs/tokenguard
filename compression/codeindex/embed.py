"""Embedding backends for semantic retrieval (Stage 2).

Fail-open by design: if no backend is available (no embedding model pulled / no
sentence-transformers installed), semantic retrieval is simply skipped and the lexical + graph
retrievers carry on. Backends:

- `OllamaEmbedder`  — POST {OLLAMA_URL}/api/embed (no Python deps; needs an embed model pulled,
  e.g. `ollama pull nomic-embed-text`).
- `SentenceTransformerEmbedder` — local `sentence-transformers` (offline).

The chosen model/backend is part of the "embeddings" derivation environment (`env.py`), so
switching models re-embeds rather than mixing incomparable vectors.
"""

from __future__ import annotations

import logging
import math
import os
from typing import List, Optional

log = logging.getLogger("localpilot.codeindex.embed")

DEFAULT_MODEL = "nomic-embed-text"


def _env(name: str, default: str) -> str:
    return os.getenv(name, default)


class OllamaEmbedder:
    backend = "ollama"

    def __init__(self, model: str) -> None:
        self.model = model
        self.name = model

    def embed(self, texts: List[str]) -> List[List[float]]:
        import httpx

        url = f"{_env('OLLAMA_URL', 'http://localhost:11434')}/api/embed"
        with httpx.Client(timeout=float(_env("CONTEXT_EMBED_TIMEOUT_S", "30"))) as client:
            resp = client.post(url, json={"model": self.model, "input": texts})
            resp.raise_for_status()
            data = resp.json()
        vectors = data.get("embeddings")
        if vectors is None and "embedding" in data:
            vectors = [data["embedding"]]
        if not vectors or len(vectors) != len(texts):
            raise RuntimeError("unexpected embedding response shape")
        return vectors


class SentenceTransformerEmbedder:
    backend = "sentence-transformers"

    def __init__(self, model: str) -> None:
        from sentence_transformers import SentenceTransformer  # type: ignore

        self.model = model
        self.name = f"st:{model}"
        self._st = SentenceTransformer(model)

    def embed(self, texts: List[str]) -> List[List[float]]:
        return [list(map(float, v)) for v in self._st.encode(texts, normalize_embeddings=False)]


_cached: Optional[object] = None
_cached_key: Optional[str] = None
_available: Optional[bool] = None


def get_embedder():
    """Return an available embedder, or None (semantic retrieval disabled)."""
    global _cached, _cached_key, _available
    if os.getenv("CONTEXT_SEMANTIC_ENABLED", "true").lower() not in ("1", "true", "yes"):
        return None
    backend = _env("CONTEXT_EMBED_BACKEND", "auto").lower()
    model = _env("CONTEXT_EMBED_MODEL", DEFAULT_MODEL)
    key = f"{backend}|{model}"
    if _cached_key == key:
        return _cached

    candidate = None
    if backend in ("auto", "ollama"):
        candidate = OllamaEmbedder(model)
    if candidate is None and backend in ("auto", "st", "sentence-transformers"):
        try:
            candidate = SentenceTransformerEmbedder(model)
        except Exception:
            candidate = None

    if candidate is not None:
        try:
            candidate.embed(["ping"])  # verify it actually works before committing
        except Exception as e:  # noqa: BLE001 - fail open
            log.info("embedding backend %s unavailable: %s", key, e)
            candidate = None
    if candidate is None and backend in ("auto", "ollama"):
        try:
            candidate = SentenceTransformerEmbedder(model)
            candidate.embed(["ping"])
        except Exception:
            candidate = None

    _cached, _cached_key = candidate, key
    return candidate


def cosine(a: List[float], b: List[float]) -> float:
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0
