"""Hybrid candidate retrieval: exact symbol + lexical (IDF) + confidence-aware graph expansion.

Each candidate records which retriever produced it and, for graph-derived ones, the edge's
resolution/confidence/basis and the seed that motivated it — provenance of the retrieval decision,
not just the code. Graph expansion only follows edges at or above `min_confidence`, so a syntactic
reference is never silently promoted into a dependency.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from ..context_engine.selector import _idf, score_chunk
from .embed import cosine
from .store import CodebaseIndex
from .terms import query_terms


@dataclass
class Candidate:
    path: str
    name: str
    kind: str
    start_line: int
    end_line: int
    content: str
    score: float
    retriever: str                     # exact | lexical | graph
    reason: str
    resolution: Optional[str] = None
    confidence: float = 1.0
    via: Optional[str] = None          # seed path that pulled this in
    meta: Dict = field(default_factory=dict)


def _dir_boost(path: str, terms) -> float:
    """Nudge files whose directory/filename matches a query term (e.g. 'test' -> tests/)."""
    parts = path.lower().split("/")
    dirs, stem = parts[:-1], parts[-1].rsplit(".", 1)[0]
    dir_hits = sum(1 for t in terms if t in dirs)
    stem_hits = sum(1 for t in terms if t in stem)
    return 0.5 * dir_hits + 0.4 * stem_hits


class _Shim:
    """Minimal object matching what `context_engine.selector.score_chunk` expects."""

    __slots__ = ("file", "symbol", "content")

    def __init__(self, file: str, symbol: str, content: str) -> None:
        self.file = file
        self.symbol = symbol
        self.content = content


def retrieve(index: CodebaseIndex, query: str, *, min_confidence: float = 0.5,
             seeds: int = 8, max_candidates: int = 240, embedder=None,
             semantic_k: int = 20, semantic_weight: float = 1.2) -> List[Candidate]:
    rows = index.symbols()
    if not rows:
        return []

    terms = query_terms(query)
    shims = [_Shim(r["path"], r["name"] or "", r["content"] or "") for r in rows]
    idf = _idf(shims, terms) if terms else {}
    total_idf = sum(idf.values()) or 1.0

    candidates: Dict[tuple, Candidate] = {}

    for row, shim in zip(rows, shims):
        (score, reason) = score_chunk(shim, terms, idf or None, total_idf)
        boost = _dir_boost(row["path"], terms)
        score += boost
        name = row["name"] or ""
        retriever = "lexical"
        if name and name.lower() in terms:
            score = max(score, 0.9) + 0.5  # exact symbol mentioned in the query
            retriever = "exact"
        if score <= 0 and retriever != "exact":
            continue
        if boost > 0 and retriever == "lexical":
            reason = (reason + f"; path match: {row['path']}").strip("; ")
        key = (row["path"], name, row["start_line"])
        candidates[key] = Candidate(
            path=row["path"], name=name, kind=row["kind"] or "", start_line=row["start_line"] or 1,
            end_line=row["end_line"] or row["start_line"] or 1, content=row["content"] or "",
            score=float(score), retriever=retriever, reason=reason,
        )

    # Semantic retrieval (Stage 2): complements exact/lexical/graph when vocabulary differs
    # (e.g. "vector database" vs a `ChromaDB` class). Fail-open when no embedder is available.
    if embedder is not None:
        try:
            vectors = index.load_embeddings(embedder.name)
        except Exception:  # noqa: BLE001
            vectors = []
        if vectors:
            try:
                qvec = embedder.embed([query])[0]
            except Exception:  # noqa: BLE001
                qvec = None
            if qvec:
                scored = sorted(((cosine(qvec, v), row) for row, v in vectors),
                                key=lambda x: -x[0])[:semantic_k]
                for sim, row in scored:
                    if sim < 0.2:
                        continue
                    key = (row["path"], row["name"] or "", row["start_line"])
                    value = round(semantic_weight * sim, 6)
                    existing = candidates.get(key)
                    if existing and existing.score >= value:
                        continue
                    candidates[key] = Candidate(
                        path=row["path"], name=row["name"] or "", kind=row["kind"] or "",
                        start_line=row["start_line"] or 1,
                        end_line=row["end_line"] or row["start_line"] or 1,
                        content=row["content"] or "", score=value, retriever="semantic",
                        reason=f"semantic similarity {sim:.2f}",
                    )

    # Graph expansion from the strongest seeds only.
    seed_list = sorted(candidates.values(), key=lambda c: (-c.score, c.path, c.start_line))[:seeds]
    for seed in seed_list:
        related = index.related_paths(seed.path, direction="both", min_confidence=min_confidence)
        for direction, items in related.items():
            for item in items:
                if item["path"] == seed.path:
                    continue
                for row in index.symbols_in_path(item["path"]):
                    key = (row["path"], row["name"] or "", row["start_line"])
                    boosted = round(seed.score * 0.4 * item["confidence"], 6)
                    existing = candidates.get(key)
                    if existing and existing.score >= boosted:
                        continue
                    candidates[key] = Candidate(
                        path=row["path"], name=row["name"] or "", kind=row["kind"] or "",
                        start_line=row["start_line"] or 1, end_line=row["end_line"] or row["start_line"] or 1,
                        content=row["content"] or "", score=boosted, retriever="graph",
                        reason=f"{item['kind']} {'from' if direction == 'outgoing' else 'of'} "
                               f"{seed.name or seed.path} ({item['basis']})",
                        resolution=item["resolution"], confidence=item["confidence"], via=seed.path,
                    )

    ordered = sorted(candidates.values(), key=lambda c: (-c.score, c.path, c.start_line))
    return ordered[:max_candidates]
