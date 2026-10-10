"""Stage 3/5: lexical relevance scoring and top-K selection (P2-lite)."""

from __future__ import annotations

import math
import re

from .config import ContextConfig
from .models import Chunk

_SPLIT = re.compile(r"[^A-Za-z0-9]+")
_WORDS = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")

STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "if", "then", "else", "for", "of",
    "to", "in", "on", "at", "by", "with", "from", "as", "is", "are", "was",
    "were", "be", "been", "being", "do", "does", "did", "how", "what", "why",
    "when", "where", "which", "who", "this", "that", "these", "those", "my",
    "our", "your", "it", "its", "can", "could", "should", "would", "will",
    "not", "no", "yes", "please", "explain", "fix", "help", "code", "function",
    "file", "add", "make", "write", "test", "tests", "unit", "bug", "issue",
    "error", "failing", "fails", "fail", "broken", "doesnt", "doesn", "cant",
    "here", "there", "about", "into", "over", "after", "before",
}


def extract_terms(query: str) -> set:
    """Split a query into lowercase search terms (handles camelCase/snake_case)."""
    terms: set = set()
    for raw in _SPLIT.split(query):
        if not raw:
            continue
        for part in _WORDS.findall(raw):
            low = part.lower()
            if len(low) >= 2 and low not in STOPWORDS and not low.isdigit():
                terms.add(low)
        low = raw.lower()
        if len(low) >= 2 and low not in STOPWORDS and not low.isdigit():
            terms.add(low)
    return terms


def score_file(info, terms: set) -> tuple:
    """Return ``(score, reason)`` for a single file. Score in ``0..1``."""
    if not terms:
        return 0.0, "no query terms"

    path_l = info.rel_path.lower()
    content_l = info.text.lower()

    path_hits = sorted(t for t in terms if t in path_l)
    content_hits = sorted(t for t in terms if t in content_l)

    path_score = len(path_hits) / len(terms)
    content_score = len(content_hits) / len(terms)
    score = 0.6 * path_score + 0.4 * content_score

    reasons = []
    if path_hits:
        reasons.append("path match: " + ", ".join(path_hits))
    if content_hits:
        reasons.append("content match: " + ", ".join(content_hits[:5]))
    return round(score, 6), ("; ".join(reasons) if reasons else "weak match")


# Field weights: a hit in the symbol/path is worth more than in the body.
_SYMBOL_W = 1.6
_PATH_W = 1.2
_CONTENT_W = 1.0


def score_chunk(chunk, terms: set, idf: dict | None = None, total_idf: float | None = None) -> tuple:
    """Symbol/path/content score for a code chunk.

    When ``idf`` is supplied, terms are weighted by inverse document frequency
    across the chunk corpus, so ubiquitous tokens (``get``, ``requests``) no
    longer dominate discriminative ones (``httpadapter``, ``redirect``).
    Score is normalized to ``0.._SYMBOL_W``.
    """
    if not terms:
        return 0.0, "no query terms"

    path_l = chunk.file.lower()
    symbol_l = (chunk.symbol or "").lower()
    content_l = chunk.content.lower()

    if idf is None:
        path_hits = sorted(t for t in terms if t in path_l)
        symbol_hits = sorted(t for t in terms if symbol_l and t in symbol_l)
        content_hits = sorted(t for t in terms if t in content_l)
        path_score = len(path_hits) / len(terms)
        symbol_score = len(symbol_hits) / len(terms)
        content_score = len(content_hits) / len(terms)
        score = 0.35 * path_score + 0.35 * symbol_score + 0.30 * content_score
        reasons = []
        if symbol_hits:
            reasons.append("symbol match: " + ", ".join(symbol_hits[:4]))
        if path_hits:
            reasons.append("path match: " + ", ".join(path_hits[:4]))
        if content_hits:
            reasons.append("content match: " + ", ".join(content_hits[:5]))
        return round(score, 6), ("; ".join(reasons) if reasons else "weak match")

    symbol_hits = sorted(t for t in terms if symbol_l and t in symbol_l)
    path_hits = sorted(t for t in terms if t in path_l)
    content_hits = sorted(t for t in terms if t in content_l)

    weight = 0.0
    for t in terms:
        w = idf.get(t, 1.0)
        if symbol_l and t in symbol_l:
            weight += w * _SYMBOL_W
        elif t in path_l:
            weight += w * _PATH_W
        elif t in content_l:
            weight += w * _CONTENT_W
    denom = total_idf or sum(idf.get(t, 1.0) for t in terms) or 1.0
    score = weight / denom

    reasons = []
    if symbol_hits:
        reasons.append("symbol match: " + ", ".join(symbol_hits[:4]))
    if path_hits:
        reasons.append("path match: " + ", ".join(path_hits[:4]))
    if content_hits:
        reasons.append("content match: " + ", ".join(content_hits[:5]))
    return round(score, 6), ("; ".join(reasons) if reasons else "weak match")


def _idf(chunks: list, terms: set) -> dict:
    n = len(chunks) or 1
    df = {t: 0 for t in terms}
    for c in chunks:
        text = (c.file + " " + (c.symbol or "") + " " + c.content).lower()
        for t in terms:
            if t in text:
                df[t] += 1
    return {t: math.log((n + 1) / (df[t] + 1)) + 1.0 for t in terms}


def rank_chunks(query: str, chunks: list) -> list:
    """Return ``[(score, reason, chunk), ...]`` in input order (IDF-weighted)."""
    terms = extract_terms(query)
    if not terms or not chunks:
        return [(score_chunk(c, terms), c) for c in chunks]
    idf = _idf(chunks, terms)
    total_idf = sum(idf.values()) or 1.0
    return [(score_chunk(c, terms, idf, total_idf), c) for c in chunks]


def select(query: str, files: list, cfg: ContextConfig) -> tuple:
    """Rank files and return ``(chunks, files_considered)``.

    Deterministic tie-break by relative path. Zero-relevance queries fall back
    to the most recently modified files so the result is never empty.
    """
    terms = extract_terms(query)
    scored = [(score_file(f, terms), f) for f in files]
    positive = [(s, r, f) for ((s, r), f) in scored if s > 0]

    if positive:
        positive.sort(key=lambda x: (-x[0], x[2].rel_path))
        pool = positive[: cfg.top_k]
        considered = len(positive)
    else:
        recent = sorted(files, key=lambda f: (-f.mtime, f.rel_path))[: cfg.top_k]
        pool = [(0.0, "fallback: recent", f) for f in recent]
        considered = 0

    chunks = [
        Chunk(
            file=f.rel_path,
            start_line=1,
            end_line=f.line_count,
            score=float(s),
            reason=r,
        )
        for (s, r, f) in pool
    ]
    return chunks, considered
