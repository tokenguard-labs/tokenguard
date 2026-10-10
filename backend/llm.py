"""LLM clients: Ollama (local) and a pluggable cloud provider."""
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Optional

import httpx

from . import config

log = logging.getLogger("localpilot.llm")

SYSTEM_PROMPT = "You are an expert coding assistant. Be concise and correct. Use markdown code blocks for code."

LOCAL_JSON_INSTRUCTIONS = """You are a coding assistant running as a small local model.
Answer the user's request, then honestly rate your confidence that the answer is fully correct and complete.
If the task needs deep multi-file reasoning, architecture decisions, or knowledge you lack, set needs_cloud to true.

Respond ONLY with a JSON object of this exact shape:
{"answer": "<your full answer in markdown>", "confidence": <number 0.0-1.0>, "needs_cloud": <true|false>}"""

HEDGE_PATTERNS = re.compile(
    r"\b(i'?m not sure|i am not sure|not certain|i don'?t know|cannot determine|can'?t determine|"
    r"without (more|additional) (context|information)|it depends|might be|may be the issue|unclear)\b",
    re.IGNORECASE,
)


@dataclass
class LLMResult:
    answer: str
    model: str
    latency_ms: int
    input_tokens: int = 0
    output_tokens: int = 0
    confidence: Optional[float] = None
    needs_cloud: bool = False
    fallback_reason: Optional[str] = None  # set when the real cloud call failed and the mock answered


def _parse_local_json(raw: str):
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            return raw, 0.5, False
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return raw, 0.5, False
    answer = str(data.get("answer", "")).strip()
    try:
        confidence = float(data.get("confidence", 0.5))
    except (TypeError, ValueError):
        confidence = 0.5
    return answer, max(0.0, min(confidence, 1.0)), bool(data.get("needs_cloud", False))


def _adjust_confidence(answer: str, confidence: float) -> float:
    """Small models over-report confidence; penalize obvious signs of uncertainty."""
    if len(answer) < 20:
        confidence -= 0.4
    hedges = len(HEDGE_PATTERNS.findall(answer))
    confidence -= 0.1 * min(hedges, 3)
    return round(max(0.0, min(confidence, 1.0)), 2)


async def call_local(prompt: str, context: str = "") -> LLMResult:
    user_content = f"{prompt}\n\n### Code context\n{context}" if context else prompt
    payload = {
        "model": config.LOCAL_MODEL,
        "messages": [
            {"role": "system", "content": LOCAL_JSON_INSTRUCTIONS},
            {"role": "user", "content": user_content},
        ],
        "format": "json",
        "stream": False,
        "options": {"temperature": 0.2, "num_ctx": config.LOCAL_NUM_CTX},
    }
    start = time.perf_counter()
    async with httpx.AsyncClient(timeout=config.LOCAL_TIMEOUT_S) as client:
        resp = await client.post(f"{config.OLLAMA_URL}/api/chat", json=payload)
        resp.raise_for_status()
        data = resp.json()
    latency = int((time.perf_counter() - start) * 1000)

    answer, confidence, needs_cloud = _parse_local_json(data["message"]["content"])
    return LLMResult(
        answer=answer,
        model=config.LOCAL_MODEL,
        latency_ms=latency,
        input_tokens=data.get("prompt_eval_count", 0),
        output_tokens=data.get("eval_count", 0),
        confidence=_adjust_confidence(answer, confidence),
        needs_cloud=needs_cloud,
    )


async def _call_anthropic(user_content: str) -> LLMResult:
    if not config.ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")
    start = time.perf_counter()
    async with httpx.AsyncClient(timeout=config.CLOUD_TIMEOUT_S) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": config.ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": config.ANTHROPIC_MODEL,
                "max_tokens": config.CLOUD_MAX_TOKENS,
                "system": SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": user_content}],
            },
        )
        resp.raise_for_status()
        data = resp.json()
    text = "".join(block.get("text", "") for block in data["content"] if block["type"] == "text")
    return LLMResult(
        answer=text,
        model=config.ANTHROPIC_MODEL,
        latency_ms=int((time.perf_counter() - start) * 1000),
        input_tokens=data["usage"]["input_tokens"],
        output_tokens=data["usage"]["output_tokens"],
    )


async def _call_openai_compatible(
    user_content: str, url: str, api_key: str, model: str, max_tokens: int
) -> LLMResult:
    start = time.perf_counter()
    async with httpx.AsyncClient(timeout=config.CLOUD_TIMEOUT_S) as client:
        resp = await client.post(
            url,
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": model,
                "max_tokens": max_tokens,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_content},
                ],
            },
        )
        resp.raise_for_status()
        data = resp.json()
    usage = data.get("usage", {})
    return LLMResult(
        answer=data["choices"][0]["message"]["content"],
        model=data.get("model", model),
        latency_ms=int((time.perf_counter() - start) * 1000),
        input_tokens=usage.get("prompt_tokens", 0),
        output_tokens=usage.get("completion_tokens", 0),
    )


async def _call_mock(user_content: str) -> LLMResult:
    """Stand-in cloud model so the pipeline works end-to-end before an API key is available."""
    import asyncio

    from .tokens import count_tokens

    await asyncio.sleep(1.2)
    return LLMResult(
        answer=f"[MOCK CLOUD ANSWER] Received {count_tokens(user_content)} tokens of request + context. "
        "Set DEEPSEEK_API_KEY in backend/.env (CLOUD_PROVIDER=deepseek) to get a real answer.",
        model="mock-cloud",
        latency_ms=1200,
        input_tokens=count_tokens(user_content),
        output_tokens=30,
    )


async def _call_deepseek(user_content: str) -> LLMResult:
    key = config.DEEPSEEK_API_KEY
    if not key or key == config.DEEPSEEK_API_KEY_PLACEHOLDER:
        raise RuntimeError(f"{config.DEEPSEEK_API_KEY_ENV} is not set (still the placeholder)")
    return await _call_openai_compatible(
        user_content, config.DEEPSEEK_URL, key, config.DEEPSEEK_MODEL,
        min(config.CLOUD_MAX_TOKENS, config.DEEPSEEK_MAX_TOKENS),
    )


async def _call_openai(user_content: str) -> LLMResult:
    if not config.OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is not set")
    return await _call_openai_compatible(
        user_content, f"{config.OPENAI_BASE_URL}/chat/completions", config.OPENAI_API_KEY,
        config.OPENAI_MODEL, config.CLOUD_MAX_TOKENS,
    )


_PROVIDERS = {"deepseek": _call_deepseek, "anthropic": _call_anthropic, "openai": _call_openai}


def _describe_error(e: Exception) -> str:
    if isinstance(e, httpx.HTTPStatusError):
        return f"HTTP {e.response.status_code}: {e.response.text[:200]}"
    return f"{type(e).__name__}: {e}"[:300]


async def call_cloud(prompt: str, context: str = "") -> LLMResult:
    user_content = f"{prompt}\n\n### Relevant code context\n{context}" if context else prompt
    provider = config.CLOUD_PROVIDER.lower()
    call = _PROVIDERS.get(provider)
    if call is None:
        return await _call_mock(user_content)
    try:
        return await call(user_content)
    except Exception as e:
        if not config.CLOUD_FALLBACK_TO_MOCK:
            raise
        reason = f"{provider} call failed ({_describe_error(e)}); answered with the mock"
        log.warning(reason)
        result = await _call_mock(user_content)
        result.fallback_reason = reason
        return result
