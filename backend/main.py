"""LocalPilot API. Run with:  make backend  (or, from the repo root: uvicorn backend.main:app --port 8000)"""
import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Literal, Optional

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from . import llm, model_catalog, router, system

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    # Free the local model when the backend (and with it, the extension) stops.
    await llm.unload_local_model_on_exit()


app = FastAPI(title="LocalPilot", version="0.1.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.middleware("http")
async def llm_overrides_middleware(request, call_next):
    """Bind editor-supplied LLM settings (provider/model/API key) to this request.

    The VS Code extension keeps API keys in SecretStorage and sends them as headers, so keys
    never live in .env or on disk. Missing headers fall back to the backend's env config.
    """
    provider = request.headers.get("x-llm-provider")
    model = request.headers.get("x-llm-model")
    api_key = request.headers.get("x-llm-api-key")
    local_model = request.headers.get("x-local-model")
    if not (provider or model or api_key or local_model):
        return await call_next(request)
    token = llm.set_overrides(llm.LLMOverrides(
        provider=provider, model=model, api_key=api_key, local_model=local_model))
    try:
        return await call_next(request)
    finally:
        llm.reset_overrides(token)


class ChatRequest(BaseModel):
    prompt: str
    repo_path: Optional[str] = None
    code: str = ""  # optional selected code / file contents from the editor
    force_route: Literal["auto", "local", "cloud"] = "auto"


class ChatResponse(BaseModel):
    answer: str
    route: str
    model: str
    escalated: bool
    escalation_reason: Optional[str]
    confidence: Optional[float]
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
    trace: List[str]


class AnalyzeRequest(BaseModel):
    prompt: str
    repo_path: Optional[str] = None
    code: str = ""


@app.post("/analyze")
async def analyze_route(req: AnalyzeRequest):
    """Routing decision only - no model is called. decision: local | local_first | cloud"""
    return router.preview(req.prompt, req.repo_path, req.code)


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
    the disconnect. (request.is_disconnected() can't see it through the HTTP middleware.)"""
    while (await request.receive())["type"] != "http.disconnect":
        pass


@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest, request: Request):
    # Run the pipeline as a task so a client disconnect (the sidebar's Stop button) cancels it.
    # Cancelling closes the connection to Ollama / the cloud API (so generation stops) and
    # releases the local-model lock, so the next prompt doesn't queue behind a dropped answer.
    work = asyncio.create_task(router.handle(req.prompt, req.repo_path, req.code, req.force_route))
    hangup = asyncio.create_task(_wait_for_disconnect(request))
    try:
        await asyncio.wait({work, hangup}, return_when=asyncio.FIRST_COMPLETED)
        if not work.done():
            work.cancel()
            logging.info("/chat cancelled by client")
            raise HTTPException(499, "cancelled by client")
        result = work.result()
    except HTTPException:
        raise
    except httpx.HTTPStatusError as e:
        raise HTTPException(502, f"upstream LLM error: {e.response.status_code} {e.response.text[:300]}")
    except Exception as e:
        raise HTTPException(500, str(e))
    finally:
        hangup.cancel()
        if not work.done():
            work.cancel()  # the request itself was cancelled (e.g. server shutdown)
    HISTORY.append({"prompt": req.prompt, **{k: v for k, v in result.items() if k != "answer"}})
    return result


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
        "avg_latency_ms": int(sum(h["latency_ms"] for h in real) / len(real)) if real else 0,
        "history": HISTORY[-50:],
    }


@app.get("/health")
async def health():
    # Installed local models, host resources and pullable recommendations (one Ollama call).
    local = await _local_model_info()
    # Actually verify the configured provider instead of echoing its name: a placeholder or
    # rejected key must not look "connected". Cached for CLOUD_HEALTHCHECK_TTL_S.
    cloud = await llm.validate_cloud_provider()
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
        "cloud": cloud,
    }
