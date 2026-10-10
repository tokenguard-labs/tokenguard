"""LLM clients: Ollama (local) and a pluggable cloud provider."""
import asyncio
import json
import logging
import re
import time
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable, Dict, Optional

import httpx

from . import config

log = logging.getLogger("localpilot.llm")

# Streaming hook: emit(event, data). Model calls send "delta" {"text"} as tokens arrive and
# "reset" {"reason"} when text already sent is being thrown away (e.g. mock fallback mid-answer).
Emit = Callable[[str, Dict[str, Any]], None]


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
    }.get(provider, "")


def effective_key(provider: str) -> str:
    ov = _overrides.get()
    if ov and ov.api_key:
        return ov.api_key
    return {
        "deepseek": config.DEEPSEEK_API_KEY,
        "openai": config.OPENAI_API_KEY,
        "anthropic": config.ANTHROPIC_API_KEY,
    }.get(provider, "")


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


class _JSONAnswerStream:
    """Decode the "answer" string of the local model's JSON reply while it is still streaming,
    so the answer can be shown token by token. The full raw text is kept for the final parse."""

    _KEY = re.compile(r'"answer"\s*:\s*"')
    _ESCAPES = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}

    def __init__(self) -> None:
        self.raw = ""
        self._pos: Optional[int] = None  # index just past the decoded part of the answer
        self._done = False

    def feed(self, chunk: str) -> str:
        """Append raw model output; return the newly decoded part of the answer (may be "")."""
        self.raw += chunk
        if self._done:
            return ""
        if self._pos is None:
            match = self._KEY.search(self.raw)
            if not match:
                return ""
            self._pos = match.end()
        buf, i, out = self.raw, self._pos, []
        while i < len(buf):
            c = buf[i]
            if c == '"':
                self._done = True
                break
            if c != "\\":
                out.append(c)
                i += 1
                continue
            if i + 1 >= len(buf):
                break  # escape split across chunks: wait for the rest
            esc = buf[i + 1]
            if esc != "u":
                out.append(self._ESCAPES.get(esc, esc))
                i += 2
                continue
            if i + 6 > len(buf):
                break
            try:
                cp = int(buf[i + 2:i + 6], 16)
            except ValueError:
                out.append(buf[i + 2:i + 6])
                i += 6
                continue
            if 0xD800 <= cp < 0xDC00:  # high surrogate: decode it together with its low half
                if i + 8 > len(buf):
                    break  # can't tell yet whether "\u<low>" follows
                if buf[i + 6:i + 8] == "\\u" and i + 12 > len(buf):
                    break
                try:
                    low = int(buf[i + 8:i + 12], 16)
                    out.append(chr(0x10000 + ((cp - 0xD800) << 10) + (low - 0xDC00)))
                    i += 12
                    continue
                except ValueError:
                    pass
            out.append(chr(cp))
            i += 6
        self._pos = i
        return "".join(out)


async def _raise_for_status(resp: httpx.Response) -> None:
    """raise_for_status for a streamed response (reads the body first so errors can quote it)."""
    if resp.is_error:
        await resp.aread()
        resp.raise_for_status()


async def _sse_data(resp: httpx.Response) -> AsyncIterator[Dict[str, Any]]:
    """The JSON `data:` payloads of a server-sent-events response (OpenAI / Anthropic streams)."""
    async for line in resp.aiter_lines():
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            return
        if data:
            yield json.loads(data)


def _adjust_confidence(answer: str, confidence: float) -> float:
    """Small models over-report confidence; penalize obvious signs of uncertainty."""
    if len(answer) < 20:
        confidence -= 0.4
    hedges = len(HEDGE_PATTERNS.findall(answer))
    confidence -= 0.1 * min(hedges, 3)
    return round(max(0.0, min(confidence, 1.0)), 2)


async def call_local(prompt: str, context: str = "", emit: Optional[Emit] = None) -> LLMResult:
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
        "stream": True,
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
        stream = _JSONAnswerStream()
        data: Dict[str, Any] = {}
        # Streamed: the timeout applies between chunks, not to the whole generation.
        async with httpx.AsyncClient(timeout=config.LOCAL_TIMEOUT_S) as client:
            async with client.stream("POST", f"{config.OLLAMA_URL}/api/chat", json=payload) as resp:
                await _raise_for_status(resp)
                async for line in resp.aiter_lines():
                    if not line.strip():
                        continue
                    chunk = json.loads(line)
                    if chunk.get("error"):
                        raise RuntimeError(chunk["error"])  # Ollama streams errors with HTTP 200
                    delta = stream.feed((chunk.get("message") or {}).get("content", ""))
                    if delta and emit:
                        emit("delta", {"text": delta})
                    if chunk.get("done"):
                        data = chunk  # the final chunk carries the token counts
        latency = int((time.perf_counter() - start) * 1000)

    answer, confidence, needs_cloud = _parse_local_json(stream.raw)
    return LLMResult(
        answer=answer,
        model=model,
        latency_ms=latency,
        input_tokens=data.get("prompt_eval_count", 0),
        output_tokens=data.get("eval_count", 0),
        confidence=_adjust_confidence(answer, confidence),
        needs_cloud=needs_cloud,
    )


async def _call_anthropic(user_content: str, emit: Optional[Emit] = None) -> LLMResult:
    key = effective_key("anthropic")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")
    model = effective_model("anthropic")
    start = time.perf_counter()
    parts: list[str] = []
    input_tokens = output_tokens = 0
    async with httpx.AsyncClient(timeout=config.CLOUD_TIMEOUT_S) as client:
        async with client.stream(
            "POST",
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
                "stream": True,
            },
        ) as resp:
            await _raise_for_status(resp)
            async for event in _sse_data(resp):
                kind = event.get("type")
                if kind == "message_start":
                    usage = event["message"].get("usage", {})
                    input_tokens = usage.get("input_tokens", 0)
                    output_tokens = usage.get("output_tokens", 0)
                elif kind == "content_block_delta" and event["delta"].get("type") == "text_delta":
                    text = event["delta"]["text"]
                    parts.append(text)
                    if emit:
                        emit("delta", {"text": text})
                elif kind == "message_delta":
                    output_tokens = event.get("usage", {}).get("output_tokens", output_tokens)
                elif kind == "error":
                    raise RuntimeError(f"anthropic stream error: {event.get('error')}")
    return LLMResult(
        answer="".join(parts),
        model=model,
        latency_ms=int((time.perf_counter() - start) * 1000),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


async def _call_openai_compatible(
    user_content: str, url: str, api_key: str, model: str, max_tokens: int, emit: Optional[Emit] = None
) -> LLMResult:
    from .tokens import count_tokens

    start = time.perf_counter()
    parts: list[str] = []
    usage: Dict[str, Any] = {}
    answered_by = model
    async with httpx.AsyncClient(timeout=config.CLOUD_TIMEOUT_S) as client:
        async with client.stream(
            "POST",
            url,
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": model,
                "max_tokens": max_tokens,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_content},
                ],
                "stream": True,
                "stream_options": {"include_usage": True},  # usage arrives in the last chunk
            },
        ) as resp:
            await _raise_for_status(resp)
            async for chunk in _sse_data(resp):
                answered_by = chunk.get("model") or answered_by
                usage = chunk.get("usage") or usage
                for choice in chunk.get("choices") or []:
                    text = (choice.get("delta") or {}).get("content")
                    if text:
                        parts.append(text)
                        if emit:
                            emit("delta", {"text": text})
    answer = "".join(parts)
    return LLMResult(
        answer=answer,
        model=answered_by,
        latency_ms=int((time.perf_counter() - start) * 1000),
        # Estimate if the server ignored include_usage
        input_tokens=usage.get("prompt_tokens") or count_tokens(SYSTEM_PROMPT + user_content),
        output_tokens=usage.get("completion_tokens") or count_tokens(answer),
    )


async def _call_mock(user_content: str, emit: Optional[Emit] = None) -> LLMResult:
    """Stand-in cloud model so the pipeline works end-to-end before an API key is available."""
    from .tokens import count_tokens

    answer = (f"[MOCK CLOUD ANSWER] Received {count_tokens(user_content)} tokens of request + context. "
              "Set DEEPSEEK_API_KEY in backend/.env (CLOUD_PROVIDER=deepseek) to get a real answer.")
    words = re.findall(r"\S+\s*", answer)
    for word in words:  # "stream" it over ~1.2s like a real model
        await asyncio.sleep(1.2 / len(words))
        if emit:
            emit("delta", {"text": word})
    return LLMResult(
        answer=answer,
        model="mock-cloud",
        latency_ms=1200,
        input_tokens=count_tokens(user_content),
        output_tokens=30,
        is_mock=True,
    )


async def _call_deepseek(user_content: str, emit: Optional[Emit] = None) -> LLMResult:
    key = effective_key("deepseek")
    if not key or key == config.DEEPSEEK_API_KEY_PLACEHOLDER:
        raise RuntimeError(f"{config.DEEPSEEK_API_KEY_ENV} is not set (still the placeholder)")
    return await _call_openai_compatible(
        user_content, config.DEEPSEEK_URL, key, effective_model("deepseek"),
        min(config.CLOUD_MAX_TOKENS, config.DEEPSEEK_MAX_TOKENS), emit,
    )


async def _call_openai(user_content: str, emit: Optional[Emit] = None) -> LLMResult:
    key = effective_key("openai")
    if not key:
        raise RuntimeError("OPENAI_API_KEY is not set")
    return await _call_openai_compatible(
        user_content, f"{config.OPENAI_BASE_URL}/chat/completions", key,
        effective_model("openai"), config.CLOUD_MAX_TOKENS, emit,
    )


_PROVIDERS = {"deepseek": _call_deepseek, "anthropic": _call_anthropic, "openai": _call_openai}


def _describe_error(e: Exception) -> str:
    if isinstance(e, httpx.HTTPStatusError):
        return f"HTTP {e.response.status_code}: {e.response.text[:200]}"
    return f"{type(e).__name__}: {e}"[:300]


async def call_cloud(prompt: str, context: str = "", emit: Optional[Emit] = None) -> LLMResult:
    user_content = f"{prompt}\n\n### Relevant code context\n{context}" if context else prompt
    provider = effective_provider()
    if provider == "mock":
        return await _call_mock(user_content, emit)
    call = _PROVIDERS.get(provider)
    if call is None:
        reason = f"unsupported cloud provider {provider!r}; answered with the mock"
        if not config.CLOUD_FALLBACK_TO_MOCK:
            raise RuntimeError(reason)
        log.warning(reason)
        result = await _call_mock(user_content, emit)
        result.fallback_reason = reason
        return result
    streamed = False

    def track(event: str, data: Dict[str, Any]) -> None:
        nonlocal streamed
        streamed = True
        emit(event, data)

    try:
        return await call(user_content, track if emit else None)
    except Exception as e:
        if not config.CLOUD_FALLBACK_TO_MOCK:
            raise
        reason = f"{provider} call failed ({_describe_error(e)}); answered with the mock"
        log.warning(reason)
        if streamed and emit:
            emit("reset", {"reason": reason})  # drop the partial real answer
        result = await _call_mock(user_content, emit)
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
    cache_key = (provider, model, hash(key))
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
    else:
        info["detail"] = f"unknown cloud provider {provider!r}"

    _health_cache[cache_key] = (now, info)
    return info
