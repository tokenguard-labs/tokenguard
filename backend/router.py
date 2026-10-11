"""The brain: analyze -> decide -> call local/cloud -> escalate if needed -> metrics."""
import logging
import time
from typing import Any, Dict, List, Optional

from . import cloud_catalog, config
from .analyzer import analyze
from .contracts import ContextResult
from .context import ResolvedRequest, optimize_context, redact_text, repo_stats, resolve_request
from .llm import Emit, LLMResult, call_cloud, call_local, effective_local_model, effective_model, effective_provider
from .tokens import count_tokens

log = logging.getLogger("localpilot.router")


def decide(score: float) -> str:
    if score < config.LOCAL_THRESHOLD:
        return "local"
    if score > config.CLOUD_THRESHOLD:
        return "cloud"
    return "local_first"


def compress(req: ResolvedRequest, trace: Optional[List[str]] = None) -> ContextResult:
    """Run the context optimizer (selection + compression + redaction) for this request's repo."""
    ctx = optimize_context(req.prompt, req.repo_path, code=req.code, token_budget=config.CLOUD_CONTEXT_TOKEN_BUDGET)
    if trace is not None:
        trace.append(
            f"context compression [{ctx.optimizer}] on {req.repo_path or 'no repo'}: "
            f"{ctx.original_tokens} -> {ctx.optimized_tokens} tokens, {len(ctx.files_selected)} file(s), "
            f"{ctx.secrets_redacted} secret(s) masked"
        )
    return ctx


def preview(prompt: str, repo_path: Optional[str] = None, code: str = "") -> Dict[str, Any]:
    """Dry run: the routing decision and token estimates, without calling any model.
    Lets a UI ask the user for consent before anything is sent to the cloud."""
    req = resolve_request(prompt, repo_path, code)
    repo_tokens, num_files = repo_stats(req.repo_path)
    analysis = analyze(req.prompt, code=req.code, repo_context_tokens=repo_tokens, num_repo_files=num_files)
    ctx = compress(req)
    prompt_tokens = count_tokens(req.prompt)
    provider = effective_provider()
    model = effective_model(provider)
    estimated_input = prompt_tokens + ctx.optimized_tokens
    in_price, out_price = cloud_catalog.price(provider, model)
    return {
        "decision": decide(analysis.score),
        "complexity_score": analysis.score,
        "task_type": analysis.task_type,
        "factors": analysis.factors,
        "reasons": analysis.reasons,
        "original_tokens": prompt_tokens + ctx.original_tokens,
        "estimated_cloud_tokens": estimated_input,
        "files_selected": ctx.files_selected,
        "secrets_redacted": ctx.secrets_redacted,
        "context_optimizer": ctx.optimizer,
        "repo_path": req.repo_path,
        "context_sources": req.sources,
        "min_local_confidence": config.MIN_LOCAL_CONFIDENCE,
        "cloud_provider": provider,
        "cloud_model": model,
        "estimated_cost_usd": cloud_catalog.estimate_cost(
            provider, model, estimated_input, config.DEFAULT_OUTPUT_TOKENS_ESTIMATE),
        "pricing": {"input": in_price, "output": out_price},
        "prices_updated": cloud_catalog.PRICES_UPDATED,
    }


async def handle(
    prompt: str, repo_path: Optional[str] = None, code: str = "", force_route: str = "auto",
    emit: Optional[Emit] = None, detail: str = "brief",
) -> Dict[str, Any]:
    """Run the pipeline. With `emit`, also stream progress: "start" {route, model} when a model
    begins answering (a second "start" means the earlier text was superseded by an escalation),
    then that model's "delta"/"reset" events."""
    start = time.perf_counter()
    trace: List[str] = []

    req = resolve_request(prompt, repo_path, code)
    prompt = req.prompt
    trace.append("context sources: " + ("; ".join(req.sources) or "prompt only"))
    repo_tokens, num_files = repo_stats(req.repo_path)
    analysis = analyze(prompt, code=req.code, repo_context_tokens=repo_tokens, num_repo_files=num_files)
    decision = decide(analysis.score) if force_route == "auto" else force_route
    trace.append(f"score={analysis.score} -> {decision}")

    ctx: Optional[ContextResult] = None
    local: Optional[LLMResult] = None
    cloud: Optional[LLMResult] = None
    escalated = False
    escalation_reason = None

    if decision in ("local", "local_first"):
        try:
            # Local model is private, so it can see the same optimized context
            ctx = compress(req, trace)
            if emit:
                emit("start", {"route": "local", "model": effective_local_model()})
            local = await call_local(prompt, ctx.context, emit, detail)
            trace.append(f"local confidence={local.confidence} needs_cloud={local.needs_cloud} "
                         f"detail={detail} more_available={local.more_available}")
            if local.truncated:
                trace.append("local answer hit the length cap")
            if local.confidence < config.MIN_LOCAL_CONFIDENCE:
                escalation_reason = f"local confidence {local.confidence} < {config.MIN_LOCAL_CONFIDENCE}"
            elif local.needs_cloud:
                escalation_reason = "local model flagged needs_cloud"
        except Exception as e:
            log.exception("local model failed")
            escalation_reason = f"local model error: {e}"
        if escalation_reason and force_route != "local":
            escalated = True
            trace.append(f"escalating: {escalation_reason}")

    prompt_secrets = 0
    if decision == "cloud" or escalated:
        # Only the cloud path needs redaction: local never leaves the machine
        cloud_prompt, prompt_secrets = redact_text(prompt)
        # Every global call goes through the optimizer: only the selected, compressed, redacted
        # context leaves the machine (reused if the local attempt already built it for this request)
        if ctx is None:
            ctx = compress(req, trace)
        if emit:
            provider = effective_provider()
            emit("start", {"route": "cloud", "model": effective_model(provider) or provider,
                           "escalation_reason": escalation_reason if escalated else None})
        cloud = await call_cloud(cloud_prompt, ctx.context, emit)
        if cloud.fallback_reason:
            trace.append(f"cloud fallback: {cloud.fallback_reason}")

    final = cloud or local
    if final is None:
        raise RuntimeError(escalation_reason or "no model produced an answer")

    assert ctx is not None  # whichever model answered ran compress() first
    # Cloud-only baseline: what a normal assistant would send (prompt + all code/repo)
    baseline_tokens = count_tokens(prompt) + ctx.original_tokens
    sent_tokens = cloud.input_tokens if cloud else 0
    # Cost of a real cloud call only (mock answers are not billed); None when prices are unknown.
    cost_usd = None
    if cloud and not cloud.is_mock:
        cost_usd = cloud_catalog.estimate_cost(
            effective_provider(), cloud.model, cloud.input_tokens, cloud.output_tokens)
    return {
        "answer": final.answer,
        "route": "cloud" if cloud else "local",
        "model": final.model,
        "escalated": escalated,
        "escalation_reason": escalation_reason if escalated else None,
        "confidence": local.confidence if local else None,
        "detail": detail,
        "more_available": bool(local and not cloud and local.more_available),
        "truncated": bool(local and not cloud and local.truncated),
        "complexity_score": analysis.score,
        "task_type": analysis.task_type,
        "factors": analysis.factors,
        "reasons": analysis.reasons,
        "original_tokens": baseline_tokens,
        "sent_tokens": sent_tokens,
        "tokens_saved": max(baseline_tokens - sent_tokens, 0),
        "files_selected": ctx.files_selected,
        "secrets_redacted": (ctx.secrets_redacted + prompt_secrets) if cloud else 0,
        "redaction_details": ctx.redaction_details if cloud else [],
        "context_optimizer": ctx.optimizer,
        "repo_path": req.repo_path,
        "context_sources": req.sources,
        "latency_ms": int((time.perf_counter() - start) * 1000),
        "local_latency_ms": local.latency_ms if local else None,
        "cloud_latency_ms": cloud.latency_ms if cloud else None,
        "cloud_fallback_reason": cloud.fallback_reason if cloud else None,
        "is_mock": bool(final.is_mock),
        "cloud_input_tokens": cloud.input_tokens if cloud else 0,
        "cloud_output_tokens": cloud.output_tokens if cloud else 0,
        "cost_usd": cost_usd,
        "trace": trace,
    }
