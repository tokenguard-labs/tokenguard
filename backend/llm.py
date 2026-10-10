"""LLM clients: Ollama (local) and a pluggable cloud provider."""
import asyncio
import json
import logging
import re
import time
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Optional

import httpx

from . import config

log = logging.getLogger("localpilot.llm")


@dataclass
class LLMOverrides:
    """Per-request LLM settings sent by the editor (VS Code SecretStorage-backed).

    Any field left as None falls back to the backend's environment configuration,
    so the extension can override just the provider, model or API key.
    """
    provider: Optional[str] = None
    model: Optional[str] = None
    api_key: Optional[str] = None
    local_model: Optional[str] = None
    base_url: Optional[str] = None  # custom OpenAI-compatible endpoint


_overrides: ContextVar[Optional[LLMOverrides]] = ContextVar("llm_overrides", default=None)


def set_overrides(overrides: Optional[LLMOverrides]):
    """Bind overrides to the current request context. Returns a token for reset_overrides."""
    return _overrides.set(overrides)


def reset_overrides(token) -> None:
    _overrides.reset(token)


def current_overrides() -> Optional[LLMOverrides]:
    return _overrides.get()


def effective_provider() -> str:
    ov = _overrides.get()
    return ((ov.provider if ov else None) or config.CLOUD_PROVIDER).lower()


def effective_model(provider: str) -> str:
    ov = _overrides.get()
    if ov and ov.model:
        return ov.model
    return {
        "deepseek": config.DEEPSEEK_MODEL,
        "openai": config.OPENAI_MODEL,
        "anthropic": config.ANTHROPIC_MODEL,
        "custom": config.CUSTOM_MODEL,
    }.get(provider, "")


def effective_key(provider: str) -> str:
    ov = _overrides.get()
    if ov and ov.api_key:
        return ov.api_key
    return {
        "deepseek": config.DEEPSEEK_API_KEY,
        "openai": config.OPENAI_API_KEY,
        "anthropic": config.ANTHROPIC_API_KEY,
        "custom": config.CUSTOM_API_KEY,
    }.get(provider, "")


def effective_base_url(provider: str) -> str:
    """Base URL (no trailing slash) for OpenAI-compatible providers, honouring the request override."""
    ov = _overrides.get()
    if ov and ov.base_url:
        return ov.base_url.rstrip("/")
    if provider == "deepseek":
        return config.DEEPSEEK_URL.rsplit("/chat/completions", 1)[0].rstrip("/")
    if provider == "openai":
        return config.OPENAI_BASE_URL.rstrip("/")
    if provider == "custom":
        return (config.CUSTOM_BASE_URL or "").rstrip("/")
    return ""


def effective_local_model() -> str:
    ov = _overrides.get()
    return (ov.local_model if ov and ov.local_model else None) or config.LOCAL_MODEL


async def ollama_models() -> tuple[bool, list[str]]:
    """(reachable, names) of the models Ollama has pulled. Names is empty when unreachable."""
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            resp = await client.get(f"{config.OLLAMA_URL}/api/tags")
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        log.warning("could not list Ollama models: %s", e)
        return False, []
    names = [(m.get("name") or m.get("model")) for m in data.get("models", [])]
    return True, sorted({n for n in names if n})


async def list_local_models() -> list[str]:
    """Names of the models Ollama has pulled. Empty list when Ollama is unreachable."""
    _, names = await ollama_models()
    return names


# --- Model downloads (ollama pull) --------------------------------------------------------
_pulls: dict[str, str] = {}  # model name -> "pulling" | "installed" | "error: ..."


def pull_status(name: str) -> Optional[str]:
    return _pulls.get(name)


def pulling_models() -> list[str]:
    return [name for name, status in _pulls.items() if status == "pulling"]


async def _run_pull(name: str) -> None:
    try:
        # timeout=None: a model download can take many minutes
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream(
                "POST", f"{config.OLLAMA_URL}/api/pull", json={"name": name, "stream": True}
            ) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        chunk = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if chunk.get("error"):
                        raise RuntimeError(chunk["error"])  # Ollama streams errors with HTTP 200
        _pulls[name] = "installed"
        log.info("pulled local model %s", name)
    except Exception as e:
        _pulls[name] = f"error: {_describe_error(e)}"
        log.warning("pull failed for %s: %s", name, _describe_error(e))


def start_pull(name: str) -> str:
    """Start downloading a model in the background; returns the current status."""
    if _pulls.get(name) == "pulling":
        return "pulling"
    _pulls[name] = "pulling"
    asyncio.create_task(_run_pull(name))
    return "pulling"


# --- Single local model at a time ---------------------------------------------------------
# Ollama keeps every model it has served resident until its keep_alive expires, so switching
# models would otherwise leave several in RAM/VRAM. We track which model we loaded and unload
# the previous one before loading another; a lock serializes the switch/load.
_loaded_local_model: Optional[str] = None
_local_lock = asyncio.Lock()


def loaded_local_model() -> Optional[str]:
    """The local model we last loaded into Ollama (None when nothing is loaded)."""
    return _loaded_local_model


async def _ollama_unload(model: str) -> None:
    """Tell Ollama to evict a model from memory immediately (keep_alive=0, no prompt)."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            await client.post(
                f"{config.OLLAMA_URL}/api/generate",
                json={"model": model, "keep_alive": 0, "stream": False},
            )
    except Exception as e:
        log.warning("could not unload local model %s: %s", model, e)


async def _ollama_preload(model: str) -> None:
    """Load a model into memory with a 1-token generation so the next prompt is warm."""
    async with httpx.AsyncClient(timeout=config.LOCAL_TIMEOUT_S) as client:
        resp = await client.post(
            f"{config.OLLAMA_URL}/api/generate",
            json={
                "model": model,
                "prompt": "hi",
                "stream": False,
                "keep_alive": config.LOCAL_KEEP_ALIVE,
                "options": {"num_predict": 1},
            },
        )
        resp.raise_for_status()


def _same_model(a: Optional[str], b: Optional[str]) -> bool:
    return bool(a) and a == b


async def switch_local_model(model: Optional[str], preload: bool = True) -> dict:
    """Make `model` the only local model loaded in Ollama; None/empty unloads everything.

    Unloads the previously loaded model first, then (optionally) preloads the new one so the
    next prompt is warm. Returns the resulting loaded model and the one it replaced.
    """
    global _loaded_local_model
    model = (model or "").strip() or None
    async with _local_lock:
        previous = _loaded_local_model
        if previous and not _same_model(previous, model):
            await _ollama_unload(previous)
            _loaded_local_model = None
        if model and preload:
            if not _same_model(previous, model):
                try:
                    await _ollama_preload(model)
                except Exception as e:
                    log.warning("could not preload local model %s: %s", model, e)
                    return {"loaded": None, "previous": previous}
            _loaded_local_model = model
        # preload=False: nothing loaded now; call_local loads it (unloading others) on demand.
        return {"loaded": _loaded_local_model, "previous": previous}


async def unload_local_model_on_exit() -> None:
    """Best-effort unload of the tracked model when the backend shuts down."""
    model = _loaded_local_model
    if model:
        await _ollama_unload(model)

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
    is_mock: bool = False  # true when no real cloud model answered (mock stand-in)


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
    model = effective_local_model()
    global _loaded_local_model
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": LOCAL_JSON_INSTRUCTIONS},
            {"role": "user", "content": user_content},
        ],
        "format": "json",
        "stream": False,
        "keep_alive": config.LOCAL_KEEP_ALIVE,
        "options": {"temperature": 0.2, "num_ctx": config.LOCAL_NUM_CTX},
    }
    # Enforce one local model at a time: unload any other model before this one is loaded.
    # The lock also keeps a model switch (PUT /local-model) from racing the generation.
    async with _local_lock:
        if _loaded_local_model and not _same_model(_loaded_local_model, model):
            await _ollama_unload(_loaded_local_model)
        _loaded_local_model = model
        start = time.perf_counter()
        async with httpx.AsyncClient(timeout=config.LOCAL_TIMEOUT_S) as client:
            resp = await client.post(f"{config.OLLAMA_URL}/api/chat", json=payload)
            resp.raise_for_status()
            data = resp.json()
        latency = int((time.perf_counter() - start) * 1000)

    answer, confidence, needs_cloud = _parse_local_json(data["message"]["content"])
    return LLMResult(
        answer=answer,
        model=model,
        latency_ms=latency,
        input_tokens=data.get("prompt_eval_count", 0),
        output_tokens=data.get("eval_count", 0),
        confidence=_adjust_confidence(answer, confidence),
        needs_cloud=needs_cloud,
    )


async def _call_anthropic(user_content: str) -> LLMResult:
    key = effective_key("anthropic")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")
    model = effective_model("anthropic")
    start = time.perf_counter()
    async with httpx.AsyncClient(timeout=config.CLOUD_TIMEOUT_S) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": model,
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
        model=model,
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
        is_mock=True,
    )


async def _call_deepseek(user_content: str) -> LLMResult:
    key = effective_key("deepseek")
    if not key or key == config.DEEPSEEK_API_KEY_PLACEHOLDER:
        raise RuntimeError(f"{config.DEEPSEEK_API_KEY_ENV} is not set (still the placeholder)")
    return await _call_openai_compatible(
        user_content, config.DEEPSEEK_URL, key, effective_model("deepseek"),
        min(config.CLOUD_MAX_TOKENS, config.DEEPSEEK_MAX_TOKENS),
    )


async def _call_openai(user_content: str) -> LLMResult:
    key = effective_key("openai")
    if not key:
        raise RuntimeError("OPENAI_API_KEY is not set")
    return await _call_openai_compatible(
        user_content, f"{effective_base_url('openai')}/chat/completions", key,
        effective_model("openai"), config.CLOUD_MAX_TOKENS,
    )


async def _call_custom(user_content: str) -> LLMResult:
    """Any OpenAI-compatible endpoint (Groq/OpenRouter/self-hosted) via the base-URL override."""
    base = effective_base_url("custom")
    if not base:
        raise RuntimeError("custom base URL is not set (tokenguard.customBaseUrl / CUSTOM_BASE_URL)")
    key = effective_key("custom")
    if not key:
        raise RuntimeError("CUSTOM_API_KEY is not set")
    model = effective_model("custom")
    if not model:
        raise RuntimeError("custom model is not set")
    return await _call_openai_compatible(
        user_content, f"{base}/chat/completions", key, model, config.CLOUD_MAX_TOKENS,
    )


_PROVIDERS = {
    "deepseek": _call_deepseek,
    "anthropic": _call_anthropic,
    "openai": _call_openai,
    "custom": _call_custom,
}


def _describe_error(e: Exception) -> str:
    if isinstance(e, httpx.HTTPStatusError):
        return f"HTTP {e.response.status_code}: {e.response.text[:200]}"
    return f"{type(e).__name__}: {e}"[:300]


async def call_cloud(prompt: str, context: str = "") -> LLMResult:
    user_content = f"{prompt}\n\n### Relevant code context\n{context}" if context else prompt
    provider = effective_provider()
    if provider == "mock":
        return await _call_mock(user_content)
    call = _PROVIDERS.get(provider)
    if call is None:
        reason = f"unsupported cloud provider {provider!r}; answered with the mock"
        if not config.CLOUD_FALLBACK_TO_MOCK:
            raise RuntimeError(reason)
        log.warning(reason)
        result = await _call_mock(user_content)
        result.fallback_reason = reason
        return result
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


async def _ping_openai_compatible(url: str, api_key: str, model: str) -> str:
    """Cheap chat completion (max_tokens=1) used to verify credentials. Returns detail text."""
    async with httpx.AsyncClient(timeout=config.CLOUD_TIMEOUT_S) as client:
        resp = await client.post(
            url,
            headers={"Authorization": f"Bearer {api_key}"},
            json={"model": model, "max_tokens": 1, "messages": [{"role": "user", "content": "ping"}]},
        )
        resp.raise_for_status()
    return "credentials accepted"


async def _ping_anthropic(api_key: str, model: str) -> str:
    async with httpx.AsyncClient(timeout=config.CLOUD_TIMEOUT_S) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={"model": model, "max_tokens": 1, "messages": [{"role": "user", "content": "ping"}]},
        )
        resp.raise_for_status()
    return "credentials accepted"


_health_cache: dict[tuple, tuple[float, dict]] = {}


async def validate_cloud_provider(force: bool = False) -> dict:
    """Verify the active cloud provider is usable by pinging it with a low-cost request.

    Returns {provider, configured, valid, detail}. `configured` is false when no API key is
    present (including the bundled placeholder); `valid` is only true after a successful ping.
    Effective settings honour per-request editor overrides. Results are cached per
    (provider, model, key) for CLOUD_HEALTHCHECK_TTL_S seconds because /health is polled often.
    """
    provider = effective_provider()
    key = effective_key(provider)
    model = effective_model(provider)
    base = effective_base_url(provider)
    cache_key = (provider, model, hash(key), base)
    now = time.time()
    cached = _health_cache.get(cache_key)
    if not force and cached and now - cached[0] < config.CLOUD_HEALTHCHECK_TTL_S:
        return cached[1]

    info = {"provider": provider, "model": model, "configured": False, "valid": False, "detail": ""}

    if provider == "mock":
        info.update(configured=True, valid=True, detail="mock provider (no credentials required)")
    elif provider == "deepseek":
        if not key or key == config.DEEPSEEK_API_KEY_PLACEHOLDER:
            info["detail"] = f"{config.DEEPSEEK_API_KEY_ENV} is not set (still the placeholder)"
        else:
            info["configured"] = True
            try:
                info["detail"] = await _ping_openai_compatible(config.DEEPSEEK_URL, key, model)
                info["valid"] = True
            except Exception as e:
                info["detail"] = _describe_error(e)
    elif provider == "openai":
        if not key:
            info["detail"] = "OPENAI_API_KEY is not set"
        else:
            info["configured"] = True
            try:
                info["detail"] = await _ping_openai_compatible(
                    f"{config.OPENAI_BASE_URL}/chat/completions", key, model
                )
                info["valid"] = True
            except Exception as e:
                info["detail"] = _describe_error(e)
    elif provider == "anthropic":
        if not key:
            info["detail"] = "ANTHROPIC_API_KEY is not set"
        else:
            info["configured"] = True
            try:
                info["detail"] = await _ping_anthropic(key, model)
                info["valid"] = True
            except Exception as e:
                info["detail"] = _describe_error(e)
    elif provider == "custom":
        if not base:
            info["detail"] = "custom base URL is not set"
        elif not key:
            info["detail"] = "CUSTOM_API_KEY is not set"
        else:
            info["configured"] = True
            try:
                info["detail"] = await _ping_openai_compatible(f"{base}/chat/completions", key, model)
                info["valid"] = True
            except Exception as e:
                info["detail"] = _describe_error(e)
    else:
        info["detail"] = f"unknown cloud provider {provider!r}"

    _health_cache[cache_key] = (now, info)
    return info


_cloud_models_cache: dict[tuple, tuple[float, list]] = {}


async def _fetch_openai_models(base: str, api_key: str) -> list[str]:
    async with httpx.AsyncClient(timeout=config.CLOUD_TIMEOUT_S) as client:
        resp = await client.get(f"{base}/models", headers={"Authorization": f"Bearer {api_key}"})
        resp.raise_for_status()
        data = resp.json()
    return [m.get("id") for m in data.get("data", []) if m.get("id")]


async def _fetch_anthropic_models(api_key: str) -> list[str]:
    async with httpx.AsyncClient(timeout=config.CLOUD_TIMEOUT_S) as client:
        resp = await client.get(
            "https://api.anthropic.com/v1/models",
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"},
        )
        resp.raise_for_status()
        data = resp.json()
    return [m.get("id") for m in data.get("data", []) if m.get("id")]


async def list_cloud_models(force: bool = False) -> dict:
    """Live model ids for the active provider (cached), with an indication of key validity.

    Returns {provider, base_url, configured, valid, detail, ids}. When there is no usable key
    the caller should fall back to the curated catalog.
    """
    provider = effective_provider()
    key = effective_key(provider)
    base = effective_base_url(provider)
    cache_key = (provider, hash(key), base)
    now = time.time()
    cached = _cloud_models_cache.get(cache_key)
    if not force and cached and now - cached[0] < config.CLOUD_MODELS_TTL_S:
        return cached[1]

    result = {"provider": provider, "base_url": base, "configured": False, "valid": False, "detail": "", "ids": []}

    if provider == "mock":
        result.update(configured=True, valid=True, detail="mock provider")
    elif provider == "deepseek":
        if not key or key == config.DEEPSEEK_API_KEY_PLACEHOLDER:
            result["detail"] = f"{config.DEEPSEEK_API_KEY_ENV} is not set (still the placeholder)"
        else:
            result["configured"] = True
            try:
                result["ids"] = await _fetch_openai_models(effective_base_url("deepseek"), key)
                result["valid"] = True
            except Exception as e:
                result["detail"] = _describe_error(e)
    elif provider == "openai":
        if not key:
            result["detail"] = "OPENAI_API_KEY is not set"
        else:
            result["configured"] = True
            try:
                result["ids"] = await _fetch_openai_models(base, key)
                result["valid"] = True
            except Exception as e:
                result["detail"] = _describe_error(e)
    elif provider == "anthropic":
        if not key:
            result["detail"] = "ANTHROPIC_API_KEY is not set"
        else:
            result["configured"] = True
            try:
                result["ids"] = await _fetch_anthropic_models(key)
                result["valid"] = True
            except Exception as e:
                result["detail"] = _describe_error(e)
    elif provider == "custom":
        if not base:
            result["detail"] = "custom base URL is not set"
        elif not key:
            result["detail"] = "CUSTOM_API_KEY is not set"
        else:
            result["configured"] = True
            try:
                result["ids"] = await _fetch_openai_models(base, key)
                result["valid"] = True
            except Exception as e:
                result["detail"] = _describe_error(e)
    else:
        result["detail"] = f"unknown cloud provider {provider!r}"

    _cloud_models_cache[cache_key] = (now, result)
    return result
