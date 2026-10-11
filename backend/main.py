"""LocalPilot API. Run with:  make backend  (or, from the repo root: uvicorn backend.main:app --port 8000)"""
import asyncio
import json
import logging
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Literal, Optional

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from . import cloud_catalog, config, llm, model_catalog, router, system

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    # Free the local model when the backend (and with it, the extension) stops.
    await llm.unload_local_model_on_exit()


class LLMOverridesMiddleware:
    """Bind editor-supplied LLM settings (provider/model/API key) to this request.

    The VS Code extension keeps API keys in SecretStorage and sends them as headers, so keys
    never live in .env or on disk. Missing headers fall back to the backend's env config.

    Plain ASGI rather than @app.middleware("http"): that wrapper hides client disconnects from the
    endpoint and ends before a streamed body does, both of which /chat/stream relies on.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope["headers"]}
        overrides = llm.LLMOverrides(
            provider=headers.get("x-llm-provider"),
            model=headers.get("x-llm-model"),
            api_key=headers.get("x-llm-api-key"),
            local_model=headers.get("x-local-model"),
            base_url=headers.get("x-llm-base-url"),
        )
        if not (overrides.provider or overrides.model or overrides.api_key or overrides.local_model
                or overrides.base_url):
            return await self.app(scope, receive, send)
        token = llm.set_overrides(overrides)
        try:
            await self.app(scope, receive, send)
        finally:
            llm.reset_overrides(token)


app = FastAPI(title="LocalPilot", version="0.1.0", lifespan=lifespan)
app.add_middleware(LLMOverridesMiddleware)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


class ChatRequest(BaseModel):
    prompt: str
    repo_path: Optional[str] = None
    code: str = ""  # optional selected code / file contents from the editor
    force_route: Literal["auto", "local", "cloud"] = "auto"
    # Local answer length: "brief" by default; "detailed" re-asks for the longer explanation.
    detail: Literal["brief", "detailed"] = "brief"


class ChatResponse(BaseModel):
    answer: str
    route: str
    model: str
    escalated: bool
    escalation_reason: Optional[str]
    escalation_kind: Optional[str] = None
    confidence: Optional[float]
    confidence_threshold: float = 0.7
    low_confidence: bool = False  # local answer below confidence_threshold
    detail: str = "brief"
    more_available: bool = False  # local answer was kept short; a detailed one can be requested
    truncated: bool = False  # local answer hit the length cap
    complexity_score: float
    task_type: str
    factors: Dict[str, float]
    reasons: List[str]
    original_tokens: int
    sent_tokens: int
    tokens_saved: int
    files_selected: List[str]
    secrets_redacted: int
    redaction_details: List[str]
    context_optimizer: str
    repo_path: Optional[str]
    context_sources: List[str]
    latency_ms: int
    local_latency_ms: Optional[int]
    cloud_latency_ms: Optional[int]
    cloud_fallback_reason: Optional[str] = None
    is_mock: bool = False
    cloud_input_tokens: int = 0
    cloud_output_tokens: int = 0
    cost_usd: Optional[float] = None
    # --- explainable routing policy ---
    requested_route: str = "auto"
    initial_route: str = "local_first"
    final_route: str = "local"
    complexity: str = "medium"
    heuristic_complexity: str = "medium"
    model_complexity: Optional[str] = None
    model_complexity_reason: Optional[str] = None
    classified_by: str = "heuristic"
    complexity_score_100: int = 0
    local_capable: bool = True
    cloud_allowed: bool = True
    escalate_allowed: bool = True
    capability_reasons: List[str] = []
    routing_reasons: List[str] = []
    local_model: str = ""
    model_profile: Dict[str, Any] = {}
    estimated_context_tokens: int = 0
    requires_tools: bool = False
    requires_multifile: bool = False
    escalations_used: int = 0
    context_metadata: Dict[str, Any] = {}
    trace: List[str]


class AnalyzeRequest(BaseModel):
    prompt: str
    repo_path: Optional[str] = None
    code: str = ""
    force_route: Literal["auto", "local", "cloud"] = "auto"


@app.post("/analyze")
async def analyze_route(req: AnalyzeRequest):
    """Routing decision only - no model is called. decision: local | local_first | cloud"""
    return await router.preview(req.prompt, req.repo_path, req.code, req.force_route)


_RECOMMENDED_FIELDS = ("name", "params", "size_gb", "min_ram_gb", "comfortable", "note", "tags", "description")


async def _local_model_info() -> Dict[str, Any]:
    """Installed local models, host resources, and pullable recommendations that fit."""
    reachable, installed = await llm.ollama_models()
    resources = system.system_resources()
    recommended = [
        {k: m[k] for k in _RECOMMENDED_FIELDS}
        for m in model_catalog.recommend(resources)
        if m["fits"] and m["name"] not in installed
    ]
    return {
        "ollama": reachable,
        "local_model": llm.effective_local_model(),
        "local_loaded": llm.loaded_local_model(),
        "installed": installed,
        "recommended": recommended,
        "pulling": llm.pulling_models(),
        "system": resources,
    }


@app.get("/models")
async def models():
    """Local models: installed, plus recommendations that fit this machine and can be pulled."""
    info = await _local_model_info()
    return {
        "local_model": info["local_model"],
        "local_loaded": info["local_loaded"],
        "available": info["installed"],
        "recommended": info["recommended"],
        "pulling": info["pulling"],
        "system": info["system"],
        "ollama": info["ollama"],
    }


@app.get("/cloud/models")
async def cloud_models(force: bool = False):
    """Cloud models for the active provider: live ids merged with curated metadata.

    Without a usable key we fall back to the curated catalog so the picker still works.
    """
    provider = llm.effective_provider()
    live = await llm.list_cloud_models(force=force)
    ids = live["ids"]
    models = cloud_catalog.merge(provider, ids)
    if not models:  # no live list (no key / endpoint missing): show the curated catalog
        models = cloud_catalog.all_models(provider)
    current = llm.effective_model(provider) or cloud_catalog.default_model(provider)
    return {
        "provider": provider,
        "base_url": live["base_url"],
        "configured": live["configured"],
        "valid": live["valid"],
        "detail": live["detail"],
        "current": current,
        "default": cloud_catalog.default_model(provider),
        "models": models,
        "prices_updated": cloud_catalog.PRICES_UPDATED,
    }


class PullRequest(BaseModel):
    model: str


@app.post("/models/pull")
async def pull_model(req: PullRequest):
    """Start downloading a model with `ollama pull` (background); returns the current status."""
    name = req.model.strip()
    if not name:
        raise HTTPException(400, "model name is required")
    _, installed = await llm.ollama_models()
    if name in installed:
        return {"model": name, "status": "installed"}
    return {"model": name, "status": llm.start_pull(name)}


class LocalModelRequest(BaseModel):
    model: Optional[str] = None  # None / "" unloads everything
    preload: bool = True


@app.put("/local-model")
async def select_local_model(req: LocalModelRequest):
    """Switch the active local Ollama model, keeping only one loaded in memory.

    Unloads the previously loaded model first, then optionally preloads the new one.
    """
    model = (req.model or "").strip() or None
    if model:
        available = await llm.list_local_models()
        if available and model not in available:
            raise HTTPException(400, f"model {model!r} is not available in Ollama")
    return await llm.switch_local_model(model, preload=req.preload)


# In-memory history for the dashboard (resets on restart; fine for a demo)
HISTORY: List[Dict[str, Any]] = []


async def _wait_for_disconnect(request: Request) -> None:
    """Return once the client hangs up. The body is already read, so the next ASGI message is
    the disconnect."""
    while (await request.receive())["type"] != "http.disconnect":
        pass


def _http_error(e: Exception) -> HTTPException:
    """Map a pipeline failure to the HTTP error /chat reports (and /chat/stream's error event)."""
    if isinstance(e, HTTPException):
        return e
    if isinstance(e, httpx.HTTPStatusError):
        return HTTPException(502, f"upstream LLM error: {e.response.status_code} {e.response.text[:300]}")
    return HTTPException(500, str(e))


def _record(prompt: str, result: Dict[str, Any]) -> None:
    HISTORY.append({"prompt": prompt, **{k: v for k, v in result.items() if k != "answer"}})


@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest, request: Request):
    # Run the pipeline as a task so a client disconnect (the sidebar's Stop button) cancels it.
    # Cancelling closes the connection to Ollama / the cloud API (so generation stops) and
    # releases the local-model lock, so the next prompt doesn't queue behind a dropped answer.
    work = asyncio.create_task(router.handle(req.prompt, req.repo_path, req.code, req.force_route, detail=req.detail))
    hangup = asyncio.create_task(_wait_for_disconnect(request))
    try:
        await asyncio.wait({work, hangup}, return_when=asyncio.FIRST_COMPLETED)
        if not work.done():
            work.cancel()
            logging.info("/chat cancelled by client")
            raise HTTPException(499, "cancelled by client")
        result = work.result()
    except Exception as e:
        raise _http_error(e)
    finally:
        hangup.cancel()
        if not work.done():
            work.cancel()  # the request itself was cancelled (e.g. server shutdown)
    _record(req.prompt, result)
    return result


def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


@app.post("/chat/stream")
async def chat_stream(req: ChatRequest, request: Request):
    """/chat as Server-Sent Events, so answers render while they are generated.

    Events: start {route, model, escalation_reason?} when a model begins answering (a later
    start replaces the earlier text), delta {text}, reset {reason} (discard the text so far),
    then exactly one of done {ChatResponse} or error {status, message}. A ": ping" comment is
    sent every SSE_HEARTBEAT_S while nothing else is, so clients can detect a dead connection.
    """
    queue: asyncio.Queue = asyncio.Queue()

    def emit(event: str, data: Dict[str, Any]) -> None:
        queue.put_nowait((event, data))

    async def run() -> None:
        try:
            result = await router.handle(req.prompt, req.repo_path, req.code, req.force_route, emit=emit, detail=req.detail)
        except Exception as e:
            err = _http_error(e)
            emit("error", {"status": err.status_code, "message": err.detail})
            return
        _record(req.prompt, result)
        emit("done", ChatResponse(**result).model_dump())

    # Created here so the task inherits this request's LLM overrides. As with /chat, a client
    # disconnect cancels it, which stops generation and frees the local-model lock.
    work = asyncio.create_task(run())

    async def watch_hangup() -> None:
        await _wait_for_disconnect(request)
        if not work.done():
            work.cancel()
            logging.info("/chat/stream cancelled by client")

    hangup = asyncio.create_task(watch_hangup())

    async def events():
        try:
            yield ": connected\n\n"
            while True:
                try:
                    event, data = await asyncio.wait_for(queue.get(), config.SSE_HEARTBEAT_S)
                except asyncio.TimeoutError:
                    if work.done() and queue.empty():
                        return  # cancelled before it could report anything
                    yield ": ping\n\n"
                    continue
                yield _sse(event, data)
                if event in ("done", "error"):
                    return
        finally:
            hangup.cancel()
            if not work.done():
                work.cancel()

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/stats")
async def stats():
    n = len(HISTORY)
    # Mock answers are stand-ins, not real cloud calls: keep them out of cloud counts and
    # token cost-savings so the dashboard reflects actual cloud usage.
    real = [h for h in HISTORY if not h.get("is_mock")]
    mock = n - len(real)
    local = sum(1 for h in real if h["route"] == "local")
    original = sum(h["original_tokens"] for h in real)
    sent = sum(h["sent_tokens"] for h in real)
    total_cost = sum(h.get("cost_usd") or 0.0 for h in real)
    return {
        "total_requests": n,
        "real_requests": len(real),
        "mock_requests": mock,
        "local_requests": local,
        "cloud_requests": len(real) - local,
        "escalations": sum(1 for h in real if h["escalated"]),
        "total_original_tokens": original,
        "total_sent_tokens": sent,
        "total_tokens_saved": original - sent,
        "cloud_token_reduction_pct": round(100 * (original - sent) / original, 1) if original else 0.0,
        "total_cost_usd": round(total_cost, 4),
        "avg_latency_ms": int(sum(h["latency_ms"] for h in real) / len(real)) if real else 0,
        "history": HISTORY[-50:],
    }


@app.get("/health")
async def health(force: bool = False):
    # Installed local models, host resources and pullable recommendations (one Ollama call).
    local = await _local_model_info()
    # Actually verify the configured provider instead of echoing its name: a placeholder or
    # rejected key must not look "connected". Cached for CLOUD_HEALTHCHECK_TTL_S (force bypasses).
    cloud = await llm.validate_cloud_provider(force=force)
    return {
        "status": "ok",
        "ollama": local["ollama"],
        "local_model": local["local_model"],
        "local_loaded": local["local_loaded"],
        "local_models": local["installed"],
        "local_recommended": local["recommended"],
        "local_pulling": local["pulling"],
        "system": local["system"],
        "cloud_provider": cloud["provider"],
        "cloud_model": cloud["model"],
        "cloud_configured": cloud["configured"],
        "cloud_valid": cloud["valid"],
        "cloud_detail": cloud["detail"],
        "cloud_model_info": cloud_catalog.model_info(cloud["provider"], cloud["model"]),
        "cloud": cloud,
    }
