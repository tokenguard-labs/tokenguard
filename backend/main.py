"""LocalPilot API. Run with:  make backend  (or, from the repo root: uvicorn backend.main:app --port 8000)"""
import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from typing import Any, Awaitable, Callable, Dict, List, Literal, Optional

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from . import agent, cloud_catalog, config, context, llm, model_catalog, router, system

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

# Per-session auth: the VS Code extension generates a random token and passes it to the backend
# (TOKENGUARD_SESSION_TOKEN env var) when it spawns the process; every request must then echo it
# in the X-Token-Guard-Session header. This keeps any web page open in the browser (which can
# always POST to localhost) and DNS-rebinding pages from calling this API and reading the
# workspace. A backend started by hand (`make backend`) has no token, so it falls back to
# loopback-Host-only protection.
SESSION_TOKEN = os.getenv("TOKENGUARD_SESSION_TOKEN", "")
if not SESSION_TOKEN:
    logging.warning("TOKENGUARD_SESSION_TOKEN is not set: the API is restricted to loopback "
                    "Host requests instead of token auth (fine for a manually started dev backend).")

_SWAGGER_PATHS = ("/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc")


class SessionAuthMiddleware:
    """Require the session token (or, without one, a loopback Host) on every API request."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        if scope.get("path") not in _SWAGGER_PATHS:
            headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope["headers"]}
            if SESSION_TOKEN:
                if headers.get("x-token-guard-session") != SESSION_TOKEN:
                    return await _reject(scope, receive, send, "missing or invalid session token")
            else:
                # Against DNS rebinding: a page that rebinds a public name to 127.0.0.1 arrives
                # with that name in Host. Real clients always use localhost/127.0.0.1.
                host = headers.get("host", "").rsplit(":", 1)[0].strip("[]").lower()
                if host not in ("localhost", "127.0.0.1", "::1"):
                    return await _reject(scope, receive, send,
                                         f"rejected Host {host or '<none>'} (loopback only)")
        return await self.app(scope, receive, send)


async def _reject(scope, receive, send, detail: str) -> None:
    response = JSONResponse({"detail": detail}, status_code=403)
    await response(scope, receive, send)


app.add_middleware(SessionAuthMiddleware)


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
    reachable, installed_list = await llm.ollama_model_list()
    installed = [m["name"] for m in installed_list]
    resources = system.system_resources()
    # RAM-fit info per installed model, for "this model is too big for your RAM" warnings.
    fit = {m["name"]: model_catalog.fit(m["name"], round(m["size"] / 1024 ** 3, 1), resources)
           for m in installed_list}
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
        "installed_sizes": {m["name"]: m["size"] for m in installed_list},
        "fit": fit,
        "recommended": recommended,
        "pulling": llm.pulling_models(),
        "pull_progress": llm.pull_progress(),
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
        "installed_sizes": info["installed_sizes"],
        "fit": info["fit"],
        "recommended": info["recommended"],
        "pulling": info["pulling"],
        "pull_progress": info["pull_progress"],
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


@app.get("/models/pull")
async def pull_progress():
    """Download progress per model: state, Ollama's status line, bytes completed/total, percent."""
    return llm.pull_progress()


@app.post("/models/pull/cancel")
async def cancel_pull(req: PullRequest):
    """Stop an in-flight download. Ollama keeps finished layers, so pulling again resumes."""
    return {"model": req.model, "cancelled": llm.cancel_pull(req.model.strip())}


@app.delete("/models/{name:path}")
async def delete_model(name: str):
    """Delete a pulled model from disk to free space (unloads it first if it is in memory)."""
    name = name.strip()
    if not name:
        raise HTTPException(400, "model name is required")
    try:
        await llm.delete_local_model(name)
    except LookupError as e:
        raise HTTPException(404, str(e))
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Ollama could not delete {name!r}: {e}")
    return {"model": name, "deleted": True}


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
    return HTTPException(500, str(e) or type(e).__name__)  # e.g. httpx.ReadTimeout has no message


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
    async def run(emit) -> Dict[str, Any]:
        result = await router.handle(req.prompt, req.repo_path, req.code, req.force_route, emit=emit, detail=req.detail)
        _record(req.prompt, result)
        return ChatResponse(**result).model_dump()

    return _sse_response(request, run, "/chat/stream")


def _sse_response(request: Request, run: Callable[[Callable], Awaitable[Dict[str, Any]]], name: str) -> StreamingResponse:
    """Run `run(emit)` and stream what it emits as SSE, ending with done {its result} or error.

    The task is created here so it inherits this request's LLM overrides. A client disconnect
    cancels it, which stops generation and frees the local-model lock.
    """
    queue: asyncio.Queue = asyncio.Queue()

    def emit(event: str, data: Dict[str, Any]) -> None:
        queue.put_nowait((event, data))

    async def work_fn() -> None:
        try:
            result = await run(emit)
        except Exception as e:
            err = _http_error(e)
            emit("error", {"status": err.status_code, "message": err.detail})
            return
        emit("done", result)

    work = asyncio.create_task(work_fn())

    async def watch_hangup() -> None:
        await _wait_for_disconnect(request)
        if not work.done():
            work.cancel()
            logging.info("%s cancelled by client", name)

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


class AgentRequest(BaseModel):
    prompt: str
    repo_path: Optional[str] = None
    code: str = ""  # editor selection
    active_file: Optional[str] = None  # workspace-relative path of the open file
    route: Literal["cloud", "local"] = "cloud"
    history: List[Dict[str, str]] = []  # earlier agent tasks this session: {prompt, summary}


@app.post("/agent/stream")
async def agent_stream(req: AgentRequest, request: Request):
    """Agent mode as Server-Sent Events: the model explores, edits (staged, never written) and
    verifies in a tool loop.

    Events: start {run_id, route, model, repo_path}, thought {text}, tool_call {call_id, name, args},
    tool_result {call_id, name, ok, output}, changes {changes: [{path, status, additions, deletions}]},
    approval {request_id, kind, command, cwd} (answer with POST /agent/approve), approval_resolved
    {request_id, approved}, then done {summary, changes: [{path, status, original, proposed, diff, ...}],
    commands, ...} or error {status, message}.
    """
    resolved = context.resolve_request(req.prompt, req.repo_path, req.code)
    if not resolved.repo_path:
        raise HTTPException(400, "Agent mode needs a workspace folder: open a folder in VS Code first.")
    if req.route == "cloud" and not router.cloud_allowed():
        raise HTTPException(403, "Cloud use is disabled by policy (CLOUD_ENABLED / CLOUD_TRANSMISSION_PROHIBITED); "
                                 "run the agent on the local LLM instead.")

    # The agent gets the prompt as typed (a pasted traceback keeps its paths) and reads files with
    # its tools; files the prompt names are passed along so it starts from them.
    root = os.path.realpath(resolved.repo_path)
    mentioned = list(dict.fromkeys(
        os.path.relpath(path, root) for _, path in context._paths_in_prompt(req.prompt)
        if os.path.isfile(path) and path.startswith(root + os.sep)
    ))

    async def run(emit) -> Dict[str, Any]:
        return await agent.AgentRun(
            req.prompt, resolved.repo_path, req.route, emit, code=req.code, active_file=req.active_file,
            history=req.history, mentioned_files=mentioned,
        ).run()

    return _sse_response(request, run, "/agent/stream")


class ApprovalRequest(BaseModel):
    run_id: str
    request_id: str
    approved: bool
    always: bool = False  # approve every further command in this run


@app.post("/agent/approve")
async def agent_approve(req: ApprovalRequest):
    run = agent.RUNS.get(req.run_id)
    if run is None or not run.resolve_approval(req.request_id, req.approved, req.always):
        raise HTTPException(404, "no pending approval with that id (the agent run may have ended)")
    return {"ok": True}


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
        "fit": local["fit"],
        "local_recommended": local["recommended"],
        "local_pulling": local["pulling"],
        "local_pull_progress": local["pull_progress"],
        "local_sizes": local["installed_sizes"],
        "system": local["system"],
        "cloud_provider": cloud["provider"],
        "cloud_model": cloud["model"],
        "cloud_configured": cloud["configured"],
        "cloud_valid": cloud["valid"],
        "cloud_detail": cloud["detail"],
        "cloud_model_info": cloud_catalog.model_info(cloud["provider"], cloud["model"]),
        "cloud": cloud,
    }
