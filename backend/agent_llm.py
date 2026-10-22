"""One tool-calling chat step for the agent, on the cloud provider or the local Ollama model.

Messages use the OpenAI chat format throughout (system / user / assistant with tool_calls /
tool with tool_call_id) and are converted for Anthropic and Ollama here. Provider selection, keys
and models come from llm.py, so the editor's per-request overrides apply as for /chat.
"""
from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

import httpx

from . import config, llm
from .tokens import count_tokens


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: Dict[str, Any]


@dataclass
class AssistantTurn:
    text: str
    tool_calls: List[ToolCall] = field(default_factory=list)
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    is_mock: bool = False

    def message(self) -> Dict[str, Any]:
        """This turn as an OpenAI-format assistant message, for the running transcript."""
        msg: Dict[str, Any] = {"role": "assistant", "content": self.text or ""}
        if self.tool_calls:
            msg["tool_calls"] = [
                {"id": c.id, "type": "function",
                 "function": {"name": c.name, "arguments": json.dumps(c.arguments)}}
                for c in self.tool_calls
            ]
        return msg


def _new_id() -> str:
    return "call_" + uuid.uuid4().hex[:12]


# Called with the number of tokens generated so far while a (slow, local) step is running.
Progress = Callable[[int], None]


def _parse_args(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        value = json.loads(raw, strict=False)  # tolerate raw newlines/tabs inside strings
    except (TypeError, json.JSONDecodeError):
        return {"__invalid_json__": str(raw)[:500]}
    return value if isinstance(value, dict) else {"__invalid_json__": str(raw)[:500]}


_JSON_BLOCK = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```|(\{\s*\"name\".*\})", re.DOTALL)


def _text_tool_calls(text: str, tool_names: set) -> tuple[List[ToolCall], str]:
    """Small local models often print the call as JSON instead of using the tool API.
    Returns the recovered calls and the text without them."""
    calls, spans = [], []
    for m in _JSON_BLOCK.finditer(text or ""):
        try:
            data = json.loads(m.group(1) or m.group(2), strict=False)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        name = data.get("name") or data.get("tool")
        args = data.get("arguments", data.get("parameters", data.get("args", {})))
        if name in tool_names and isinstance(args, (dict, str)):
            calls.append(ToolCall(_new_id(), name, _parse_args(args)))
            spans.append(m.span())
    for start, end in reversed(spans):
        text = text[:start] + text[end:]
    return calls, text.strip()


# --- OpenAI-compatible (DeepSeek, OpenAI, custom) ---------------------------------------------

def _openai_target(provider: str) -> tuple[str, str, str, int]:
    """(chat completions URL, key, model, max_tokens) for an OpenAI-compatible provider."""
    key = llm.effective_key(provider)
    model = llm.effective_model(provider)
    if provider == "deepseek":
        if not key or key == config.DEEPSEEK_API_KEY_PLACEHOLDER:
            raise RuntimeError(f"{config.DEEPSEEK_API_KEY_ENV} is not set (still the placeholder)")
        return config.DEEPSEEK_URL, key, model, min(config.CLOUD_MAX_TOKENS, config.DEEPSEEK_MAX_TOKENS)
    base = llm.effective_base_url(provider)
    if provider == "custom" and not base:
        raise RuntimeError("custom base URL is not set (tokenguard.customBaseUrl / CUSTOM_BASE_URL)")
    if not key:
        raise RuntimeError(f"{provider.upper()}_API_KEY is not set")
    if not model:
        raise RuntimeError(f"no model configured for {provider}")
    return f"{base}/chat/completions", key, model, config.CLOUD_MAX_TOKENS


def _openai_tools(tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [{"type": "function", "function": t} for t in tools]


async def _openai_step(provider: str, messages, tools) -> AssistantTurn:
    url, key, model, max_tokens = _openai_target(provider)
    start = time.perf_counter()
    async with httpx.AsyncClient(timeout=config.CLOUD_TIMEOUT_S) as client:
        resp = await client.post(url, headers={"Authorization": f"Bearer {key}"}, json={
            "model": model, "max_tokens": max_tokens, "messages": messages,
            "tools": _openai_tools(tools), "tool_choice": "auto", "temperature": 0.2,
        })
        resp.raise_for_status()
        data = resp.json()
    msg = (data.get("choices") or [{}])[0].get("message") or {}
    calls = [ToolCall(c.get("id") or _new_id(), (c.get("function") or {}).get("name", ""),
                      _parse_args((c.get("function") or {}).get("arguments")))
             for c in msg.get("tool_calls") or []]
    usage = data.get("usage") or {}
    return AssistantTurn(
        text=msg.get("content") or "", tool_calls=calls, model=data.get("model") or model,
        input_tokens=usage.get("prompt_tokens") or count_tokens(json.dumps(messages)),
        output_tokens=usage.get("completion_tokens") or 0,
        latency_ms=int((time.perf_counter() - start) * 1000),
    )


# --- Anthropic ---------------------------------------------------------------------------------

def to_anthropic(messages: List[Dict[str, Any]]) -> tuple[str, List[Dict[str, Any]]]:
    """OpenAI-format transcript -> (system, Anthropic messages). Consecutive tool results are
    merged into one user turn, as the Messages API requires."""
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    out: List[Dict[str, Any]] = []
    for m in messages:
        role = m["role"]
        if role == "system":
            continue
        if role == "tool":
            block = {"type": "tool_result", "tool_use_id": m["tool_call_id"], "content": m["content"]}
            if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list) \
                    and all(b.get("type") == "tool_result" for b in out[-1]["content"]):
                out[-1]["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
        elif role == "assistant":
            blocks: List[Dict[str, Any]] = []
            if m.get("content"):
                blocks.append({"type": "text", "text": m["content"]})
            for c in m.get("tool_calls") or []:
                blocks.append({"type": "tool_use", "id": c["id"], "name": c["function"]["name"],
                               "input": _parse_args(c["function"]["arguments"])})
            out.append({"role": "assistant", "content": blocks or [{"type": "text", "text": "(no output)"}]})
        else:
            out.append({"role": "user", "content": m["content"]})
    return system, out


async def _anthropic_step(messages, tools) -> AssistantTurn:
    key = llm.effective_key("anthropic")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")
    model = llm.effective_model("anthropic")
    system, msgs = to_anthropic(messages)
    start = time.perf_counter()
    async with httpx.AsyncClient(timeout=config.CLOUD_TIMEOUT_S) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": model, "max_tokens": config.CLOUD_MAX_TOKENS, "system": system, "messages": msgs,
                  "tools": [{"name": t["name"], "description": t["description"], "input_schema": t["parameters"]}
                            for t in tools]},
        )
        resp.raise_for_status()
        data = resp.json()
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    calls = [ToolCall(b["id"], b["name"], b.get("input") or {})
             for b in data.get("content", []) if b.get("type") == "tool_use"]
    usage = data.get("usage") or {}
    return AssistantTurn(text=text, tool_calls=calls, model=data.get("model") or model,
                         input_tokens=usage.get("input_tokens", 0), output_tokens=usage.get("output_tokens", 0),
                         latency_ms=int((time.perf_counter() - start) * 1000))


# --- Ollama (local) ----------------------------------------------------------------------------

def to_ollama(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    names = {}
    out = []
    for m in messages:
        if m["role"] == "assistant" and m.get("tool_calls"):
            calls = []
            for c in m["tool_calls"]:
                names[c["id"]] = c["function"]["name"]
                calls.append({"function": {"name": c["function"]["name"],
                                           "arguments": _parse_args(c["function"]["arguments"])}})
            out.append({"role": "assistant", "content": m.get("content") or "", "tool_calls": calls})
        elif m["role"] == "tool":
            out.append({"role": "tool", "content": m["content"], "tool_name": names.get(m["tool_call_id"], "")})
        else:
            out.append({"role": m["role"], "content": m.get("content") or ""})
    return out


async def _ollama_step(messages, tools, progress: Optional[Progress] = None) -> AssistantTurn:
    """Streamed, so a slow CPU generation can report progress; tool calls arrive whole in a chunk."""
    model = llm.effective_local_model()
    payload = {
        "model": model, "messages": to_ollama(messages), "tools": _openai_tools(tools), "stream": True,
        "keep_alive": config.LOCAL_KEEP_ALIVE,
        "options": {"temperature": 0.2, "num_ctx": config.AGENT_LOCAL_NUM_CTX,
                    "num_predict": config.AGENT_LOCAL_NUM_PREDICT},
    }
    parts: List[str] = []
    raw_calls: List[Dict[str, Any]] = []
    final: Dict[str, Any] = {}
    chunks, last_report = 0, 0.0
    async with llm.local_model_slot(model):
        start = time.perf_counter()
        # Streamed: the timeout applies between chunks (a cold model load is the longest gap).
        async with httpx.AsyncClient(timeout=httpx.Timeout(config.AGENT_LOCAL_TIMEOUT_S, connect=10)) as client:
            async with client.stream("POST", f"{config.OLLAMA_URL}/api/chat", json=payload) as resp:
                if resp.status_code == 400:
                    body = (await resp.aread()).decode("utf-8", errors="replace")
                    if "does not support tools" in body:
                        raise RuntimeError(f"the local model {model} does not support tool calling; pick a "
                                           "tool-capable model (e.g. qwen2.5-coder:7b) or use the global LLM")
                await llm._raise_for_status(resp)
                async for line in resp.aiter_lines():
                    if not line.strip():
                        continue
                    chunk = json.loads(line)
                    if chunk.get("error"):
                        raise RuntimeError(chunk["error"])  # Ollama streams errors with HTTP 200
                    msg = chunk.get("message") or {}
                    parts.append(msg.get("content") or "")
                    raw_calls.extend(msg.get("tool_calls") or [])
                    chunks += 1
                    now = time.perf_counter()
                    if progress and now - last_report > 0.5:
                        last_report = now
                        progress(chunks)
                    if chunk.get("done"):
                        final = chunk
    calls = [ToolCall(_new_id(), (c.get("function") or {}).get("name", ""),
                      _parse_args((c.get("function") or {}).get("arguments")))
             for c in raw_calls]
    return AssistantTurn(text="".join(parts), tool_calls=calls, model=model,
                         input_tokens=final.get("prompt_eval_count", 0),
                         output_tokens=final.get("eval_count", chunks),
                         latency_ms=int((time.perf_counter() - start) * 1000))


# --- Mock --------------------------------------------------------------------------------------

async def _mock_step(messages, tools) -> AssistantTurn:
    """Scripted stand-in so the agent UI works end to end without a key: look around, then stop."""
    steps = sum(1 for m in messages if m["role"] == "assistant")
    if steps == 0:
        return AssistantTurn("[MOCK] Looking at the workspace first.",
                             [ToolCall(_new_id(), "list_files", {"path": "."})], model="mock-agent", is_mock=True)
    return AssistantTurn("", [ToolCall(_new_id(), "finish", {
        "summary": "[MOCK AGENT] No real model ran, so no changes were made. Set a cloud API key "
                   "(or pick a tool-capable local model) to use the agent."})], model="mock-agent", is_mock=True)


def looks_like_tool_call(text: str, tool_names: set) -> bool:
    """Text that tried to be a tool call but could not be parsed (typically broken JSON escaping)."""
    names = "|".join(re.escape(n) for n in tool_names)
    return bool(re.search(r'"(?:name|tool)"\s*:\s*"(?:' + names + r')"', text or ""))


async def step(messages: List[Dict[str, Any]], tools: List[Dict[str, Any]], route: str,
               progress: Optional[Progress] = None) -> AssistantTurn:
    """Ask the model for its next move. route: "cloud" (configured provider) or "local" (Ollama)."""
    if route == "local":
        turn = await _ollama_step(messages, tools, progress)
    else:
        provider = llm.effective_provider()
        if provider == "mock":
            turn = await _mock_step(messages, tools)
        elif provider == "anthropic":
            turn = await _anthropic_step(messages, tools)
        elif provider in ("deepseek", "openai", "custom"):
            turn = await _openai_step(provider, messages, tools)
        else:
            raise RuntimeError(f"unsupported cloud provider {provider!r}")
    if not turn.tool_calls and turn.text:
        turn.tool_calls, text = _text_tool_calls(turn.text, {t["name"] for t in tools})
        if turn.tool_calls:
            turn.text = text
    return turn
