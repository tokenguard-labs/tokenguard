"""LocalPilot API. Run with:  make backend  (or, from the repo root: uvicorn backend.main:app --port 8000)"""
import logging
from typing import Any, Dict, List, Literal, Optional

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from . import config, router

logging.basicConfig(level=logging.INFO)

app = FastAPI(title="LocalPilot", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


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
    trace: List[str]


class AnalyzeRequest(BaseModel):
    prompt: str
    repo_path: Optional[str] = None
    code: str = ""


@app.post("/analyze")
async def analyze_route(req: AnalyzeRequest):
    """Routing decision only - no model is called. decision: local | local_first | cloud"""
    return router.preview(req.prompt, req.repo_path, req.code)


# In-memory history for the dashboard (resets on restart; fine for a demo)
HISTORY: List[Dict[str, Any]] = []


@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest):
    try:
        result = await router.handle(req.prompt, req.repo_path, req.code, req.force_route)
    except httpx.HTTPStatusError as e:
        raise HTTPException(502, f"upstream LLM error: {e.response.status_code} {e.response.text[:300]}")
    except Exception as e:
        raise HTTPException(500, str(e))
    HISTORY.append({"prompt": req.prompt, **{k: v for k, v in result.items() if k != "answer"}})
    return result


@app.get("/stats")
async def stats():
    n = len(HISTORY)
    local = sum(1 for h in HISTORY if h["route"] == "local")
    original = sum(h["original_tokens"] for h in HISTORY)
    sent = sum(h["sent_tokens"] for h in HISTORY)
    return {
        "total_requests": n,
        "local_requests": local,
        "cloud_requests": n - local,
        "escalations": sum(1 for h in HISTORY if h["escalated"]),
        "total_original_tokens": original,
        "total_sent_tokens": sent,
        "total_tokens_saved": original - sent,
        "cloud_token_reduction_pct": round(100 * (original - sent) / original, 1) if original else 0.0,
        "avg_latency_ms": int(sum(h["latency_ms"] for h in HISTORY) / n) if n else 0,
        "history": HISTORY[-50:],
    }


@app.get("/health")
async def health():
    ollama_ok = False
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            ollama_ok = (await client.get(f"{config.OLLAMA_URL}/api/tags")).status_code == 200
    except Exception:
        pass
    return {
        "status": "ok",
        "ollama": ollama_ok,
        "local_model": config.LOCAL_MODEL,
        "cloud_provider": config.CLOUD_PROVIDER,
    }
