"""Zero-shot complexity tie-breaker using the already-loaded local model.

The deterministic analyzer is fast but shallow (keyword-based `task_type`). This module asks the
local model to classify a request into simple / medium / complex with a strict JSON schema. It is
used *only as a tie-breaker in the medium band* (see `router._plan_for`), where the heuristic is
least certain, so simple and complex requests stay instant and deterministic.

It never raises and returns None on any failure (Ollama down, timeout, unparseable output), so the
router silently falls back to the heuristic. Results are cached per (model, prompt) with a TTL.
"""
import hashlib
import json
import logging
import time
from dataclasses import dataclass
from typing import Optional

import httpx

from . import config

log = logging.getLogger("localpilot.classifier")

COMPLEXITIES = ("simple", "medium", "complex")

SYSTEM_PROMPT = """You are a routing classifier for a coding assistant. Classify the user's request by how much work and reasoning it needs.

Return ONLY a JSON object:
{"complexity": "simple|medium|complex", "needs_cloud": <true|false>, "reason": "<short reason>"}

Definitions:
- simple: trivial and self-contained - explain a short snippet, write a regex, rename, format, a one-liner, fix a syntax error.
- medium: localized to one function/module - write unit tests for one function, modify/debug one function, add error handling or validation, a small refactor.
- complex: spans many files or the whole repo - multi-file refactor, architecture/design, migrations, concurrency bugs, repository-wide analysis.

Judge the intent and scope, not just keywords. Keep "reason" under 15 words."""


@dataclass
class ModelClassification:
    complexity: str
    needs_cloud: bool = False
    reason: str = ""
    model: str = ""


_cache: dict[tuple, tuple[float, Optional[ModelClassification]]] = {}


def _parse(raw: str) -> Optional[ModelClassification]:
    """Parse the model's JSON reply; None when it is unusable."""
    data = None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        # Fall back to the first {...} block the model emitted.
        start, end = (raw or "").find("{"), (raw or "").rfind("}")
        if start != -1 and end > start:
            try:
                data = json.loads(raw[start:end + 1])
            except json.JSONDecodeError:
                return None
    if not isinstance(data, dict):
        return None
    complexity = str(data.get("complexity", "")).strip().lower()
    if complexity not in COMPLEXITIES:
        return None
    return ModelClassification(
        complexity=complexity,
        needs_cloud=bool(data.get("needs_cloud", False)),
        reason=str(data.get("reason", ""))[:200],
    )


async def classify_local(prompt: str, model: Optional[str] = None) -> Optional[ModelClassification]:
    """Classify `prompt` with the local model. Cached; returns None on any failure."""
    if not config.LOCAL_LLM_CLASSIFIER:
        return None
    model = model or config.LOCAL_MODEL
    key = (model, hashlib.sha1(prompt.encode("utf-8", "ignore")).hexdigest())
    now = time.time()
    cached = _cache.get(key)
    if cached and now - cached[0] < config.LOCAL_LLM_CLASSIFIER_TTL_S:
        return cached[1]

    user = prompt if len(prompt) <= 4000 else prompt[:4000]
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ],
        "format": "json",
        "stream": False,
        "keep_alive": config.LOCAL_KEEP_ALIVE,
        "options": {"temperature": 0.0, "num_predict": 120},
    }
    result: Optional[ModelClassification] = None
    try:
        async with httpx.AsyncClient(timeout=config.LOCAL_LLM_CLASSIFIER_TIMEOUT_S) as client:
            resp = await client.post(f"{config.OLLAMA_URL}/api/chat", json=payload)
            resp.raise_for_status()
            content = (resp.json().get("message") or {}).get("content", "")
        result = _parse(content)
        if result is not None:
            result.model = model
    except Exception as e:  # noqa: BLE001 - never let classification break routing
        log.warning("local classifier unavailable: %s", e)
        result = None
    _cache[key] = (now, result)
    return result


def clear_cache() -> None:
    _cache.clear()
