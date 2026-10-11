"""Bounded local-model assistance (Stage 3).

The index stays the source of truth; the local model only:
- **reranks** a deterministic shortlist (it may reorder candidates it is shown), and
- **proposes** symbol names it thinks are missing (the index then resolves them to real files).

Both are bounded (a fixed shortlist / a name list), cached, and fail open: any error or timeout
leaves the deterministic output unchanged. This is the "model proposes, index disposes" contract
from docs/codebase-intelligence.md §6.4-6.5.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from typing import Callable, List, Optional, Set

log = logging.getLogger("localpilot.codeindex.assist")

RERANK_SYSTEM = """You rank code candidates for a coding request. You are given numbered candidates \
(path: symbol — why). Return ONLY JSON: {"order": [<ids most relevant first>]}. \
Include only ids that genuinely help answer the request; omit the rest. Never invent ids."""

PROPOSE_SYSTEM = """You are given a coding request and the context already selected. List symbol or \
file names that are likely REQUIRED but NOT already present. Return ONLY JSON: \
{"needed": ["<name>", ...]}. Only names that appear in the code; do not invent. Empty list is fine."""

_cache: dict = {}


def enabled() -> bool:
    return os.getenv("CONTEXT_LLM_ASSIST", "true").lower() in ("1", "true", "yes")


def _timeout() -> float:
    return float(os.getenv("CONTEXT_LLM_ASSIST_TIMEOUT_S", "20"))


def _ttl() -> float:
    return float(os.getenv("CONTEXT_LLM_ASSIST_TTL_S", "300"))


def _call_chat(system: str, user: str, model: str) -> str:
    """Default backend: the local Ollama model, non-streaming JSON. Overridable for tests."""
    import httpx

    url = f"{os.getenv('OLLAMA_URL', 'http://localhost:11434')}/api/chat"
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "format": "json",
        "stream": False,
        "options": {"temperature": 0.0, "num_predict": 200},
    }
    with httpx.Client(timeout=_timeout()) as client:
        resp = client.post(url, json=payload)
        resp.raise_for_status()
        return (resp.json().get("message") or {}).get("content", "")


# Injectable for tests.
_call_json: Callable[[str, str, str], str] = _call_chat


def _parse(raw: str) -> Optional[dict]:
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        start, end = (raw or "").find("{"), (raw or "").rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(raw[start:end + 1])
            except json.JSONDecodeError:
                return None
    return None


def _cache_get(key):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < _ttl():
        return hit[1]
    return None


def clear_cache() -> None:
    _cache.clear()


def rerank(query: str, candidates: List, model: Optional[str] = None,
           top_k: Optional[int] = None) -> List:
    """Reorder the top-K candidates by the local model's judgement. Fail open to input order."""
    if not enabled() or len(candidates) < 3:
        return candidates
    model = model or os.getenv("LOCAL_MODEL", "")
    if not model:
        return candidates
    top_k = top_k or int(os.getenv("CONTEXT_LLM_RERANK_TOP_K", "24"))
    shortlist = candidates[:top_k]
    fingerprint = hashlib.sha1(
        "|".join(f"{c.path}:{c.name}:{c.start_line}" for c in shortlist).encode()
    ).hexdigest()[:12]
    key = ("rerank", model, fingerprint, hashlib.sha1(query.encode()).hexdigest()[:12])
    cached = _cache_get(key)
    if cached is not None:
        order = cached
    else:
        lines = [f"{i + 1}. {c.path}:{c.name} ({c.kind}) — {c.reason}" for i, c in enumerate(shortlist)]
        user = f"Request: {query}\n\nCandidates:\n" + "\n".join(lines)
        try:
            order = (_parse(_call_json(RERANK_SYSTEM, user, model)) or {}).get("order")
        except Exception as e:  # noqa: BLE001 - fail open
            log.info("rerank unavailable: %s", e)
            order = None
        if not isinstance(order, list):
            order = None
        _cache[key] = (time.time(), order)

    if not order:
        return candidates
    # Conservative: boost the picked candidates' scores rather than hard-reordering, so a strong
    # deterministic match is never dropped by an errant small-model judgement (it can only nudge
    # borderline candidates up). The index/lexical score stays the backbone.
    bonus = float(os.getenv("CONTEXT_LLM_RERANK_BONUS", "0.15"))
    picked_keys = set()
    for item in order:
        try:
            idx = int(item)
        except (TypeError, ValueError):
            continue
        if 1 <= idx <= len(shortlist):
            c = shortlist[idx - 1]
            picked_keys.add((c.path, c.name, c.start_line))
    if not picked_keys:
        return candidates
    for c in candidates:
        if (c.path, c.name, c.start_line) in picked_keys:
            c.score = round(c.score + bonus, 6)
    return sorted(candidates, key=lambda c: (-c.score, c.path, c.start_line))


def propose_missing(query: str, selected_summary: str, model: Optional[str] = None) -> Set[str]:
    """Names the model thinks are needed but absent. The caller resolves them against the index."""
    if not enabled():
        return set()
    model = model or os.getenv("LOCAL_MODEL", "")
    if not model:
        return set()
    key = ("propose", model, hashlib.sha1((query + selected_summary).encode()).hexdigest()[:12])
    cached = _cache_get(key)
    if cached is not None:
        return set(cached)
    user = f"Request: {query}\n\nSelected context:\n{selected_summary}"
    try:
        names = (_parse(_call_json(PROPOSE_SYSTEM, user, model)) or {}).get("needed")
    except Exception as e:  # noqa: BLE001 - fail open
        log.info("propose_missing unavailable: %s", e)
        names = None
    result = {str(n) for n in names} if isinstance(names, list) else set()
    _cache[key] = (time.time(), sorted(result))
    return result
