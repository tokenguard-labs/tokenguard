"""The brain: analyze -> hard-policy + capability checks -> decide -> call local/cloud ->
post-response escalation -> metrics.

The decision logic lives in `routing.py`; this module supplies the runtime facts (which model is
selected, whether Ollama and the cloud are reachable/permitted) and runs the pipeline.
"""
import logging
import re
import time
from typing import Any, Dict, List, Optional

from . import classifier, cloud_catalog, config, routing
from .analyzer import analyze
from .contracts import ContextResult
from .context import ResolvedRequest, optimize_context, redact_text, repo_stats, resolve_request
from .llm import (
    Emit,
    LLMResult,
    call_cloud,
    call_local,
    effective_local_model,
    effective_model,
    effective_provider,
    ollama_reachable,
)
from .tokens import count_tokens

log = logging.getLogger("localpilot.router")

# Local models often answer with a refusal when the context is missing or the task is beyond them.
# A refusal is a validation failure, not a confident answer, so it may be escalated even at high
# self-reported confidence.
_REFUSAL = re.compile(
    r"\b(i apologize|i'?m sorry|i can(?:no|')t (?:see|access|provide|determine|answer|help)|"
    r"cannot (?:see|access|provide|determine|answer)|i don'?t have (?:access|enough)|"
    r"there is no (?:code|context|information)|not enough (?:context|information)|"
    r"i am unable to|i'?m unable to|as an ai)\b",
    re.IGNORECASE,
)


def looks_like_refusal(answer: str) -> bool:
    return bool(_REFUSAL.search(answer or ""))


def cloud_allowed() -> bool:
    """Whether anything may leave the machine: cloud on and not blocked by the privacy policy."""
    return config.CLOUD_ENABLED and not config.CLOUD_TRANSMISSION_PROHIBITED


def context_budget(requested: str, complexity: str, *, task_type: str = "general",
                   dependency: float = 0.0) -> int:
    """Context budget for this request: large for complex/cloud tasks, the local window for local.

    Complex cloud tasks should not be capped at the tiny default; but we stay well under the
    destination model's window to leave room for the answer (see CLOUD_MAX_CONTEXT_TOKENS).
    Uses the task signals too, because the weighted category needs context size it does not have
    yet (a keyword-hard task like "refactor ..." scores only medium before context is attached)."""
    complex_like = complexity == "complex" or task_type == "complex" or dependency >= 0.6
    wants_cloud = requested == "cloud" or (requested == "auto" and complex_like)
    if not wants_cloud:
        return max(1000, config.LOCAL_MAX_CONTEXT_TOKENS - config.LOCAL_CONTEXT_MARGIN_TOKENS)
    provider = effective_provider()
    model = effective_model(provider)
    info = cloud_catalog.model_info(provider, model) or {}
    window = int(info.get("context") or 0) or config.CLOUD_MODEL_CONTEXT_DEFAULT
    reserve = config.DEFAULT_OUTPUT_TOKENS_ESTIMATE + 1000
    return max(1000, min(config.CLOUD_MAX_CONTEXT_TOKENS, window - reserve))


def compress(req: ResolvedRequest, trace: Optional[List[str]] = None,
             budget: Optional[int] = None, *, assist: bool = True,
             semantic: bool = True) -> ContextResult:
    """Run the context optimizer (selection + compression + redaction) for this request's repo."""
    ctx = optimize_context(req.prompt, req.repo_path, code=req.code,
                           token_budget=budget or config.CLOUD_CONTEXT_TOKEN_BUDGET,
                           assist=assist, semantic=semantic)
    if trace is not None:
        trace.append(
            f"context compression [{ctx.optimizer}] on {req.repo_path or 'no repo'}: "
            f"{ctx.original_tokens} -> {ctx.optimized_tokens} tokens, {len(ctx.files_selected)} file(s), "
            f"{ctx.secrets_redacted} secret(s) masked"
        )
    return ctx


async def _plan_for(req: ResolvedRequest, requested: str, *, assist: bool = True,
                    semantic: bool = True, classify: bool = True):
    """Build the RoutePlan for a resolved request: compress, size the relevant context, classify."""
    repo_tokens, num_files = repo_stats(req.repo_path)  # noqa: F841 (repo size feeds the file factor)
    local_model = effective_local_model()
    local_available = await ollama_reachable()
    # Choose the budget from the task (and destination model) before compressing, so a complex,
    # repo-wide, cloud-bound request isn't squeezed into the small local-context default.
    preliminary = analyze(req.prompt, code=req.code, num_repo_files=num_files, context_tokens=0)
    budget = context_budget(requested, preliminary.complexity, task_type=preliminary.task_type,
                            dependency=preliminary.factors.get("dependency", 0.0))
    ctx = compress(req, budget=budget, assist=assist, semantic=semantic)
    # The capability check and the scorer use the *relevant* (optimized) context, not the whole repo.
    estimated_context_tokens = count_tokens(req.prompt) + ctx.optimized_tokens
    analysis = analyze(
        req.prompt,
        code=req.code,
        num_repo_files=num_files,
        context_tokens=ctx.optimized_tokens,
    )
    # Tie-breaker: only in the medium band, only for auto routing, only when the local model is up.
    # Simple/complex requests and forced routes stay instant and deterministic.
    hint = None
    hint_reason = None
    if (classify and config.LOCAL_LLM_CLASSIFIER and analysis.complexity == "medium"
            and requested == "auto" and local_available):
        model_cls = await classifier.classify_local(req.prompt, model=local_model)
        if model_cls is not None:
            hint = model_cls.complexity
            hint_reason = model_cls.reason
    plan = routing.plan_route(
        requested,
        prompt=req.prompt,
        analysis=analysis,
        estimated_context_tokens=estimated_context_tokens,
        local_model=local_model,
        local_available=local_available,
        cloud_allowed=cloud_allowed(),
        complexity_hint=hint,
        model_complexity=hint,
        model_complexity_reason=hint_reason,
    )
    return plan, ctx


async def preview(prompt: str, repo_path: Optional[str] = None, code: str = "", force_route: str = "") -> Dict[str, Any]:
    """Dry run: the routing decision and token estimates, without calling any model.
    Lets a UI ask the user for consent before anything is sent to the cloud."""
    req = resolve_request(prompt, repo_path, code)
    # Fast, model-free preview: no local-LLM classifier, rerank, propose, or embeddings. The full
    # assistance runs on /chat; the preview only needs a representative decision and token estimate.
    plan, ctx = await _plan_for(req, force_route or config.DEFAULT_ROUTE,
                                assist=False, semantic=False, classify=False)
    analysis = plan.analysis
    prompt_tokens = count_tokens(req.prompt)
    provider = effective_provider()
    model = effective_model(provider)
    estimated_input = prompt_tokens + ctx.optimized_tokens
    in_price, out_price = cloud_catalog.price(provider, model)
    return {
        "decision": plan.decision,
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
        # --- explainable routing policy (Phases 3-5, 9) ---
        **plan.as_dict(),
        "routing_reasons": analysis.reasons + plan.reasons,
        "context_metadata": ctx.metadata,
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

    plan, ctx = await _plan_for(req, force_route or config.DEFAULT_ROUTE)
    analysis = plan.analysis
    trace.append(
        f"routing: requested={plan.requested} complexity={plan.complexity} ({plan.complexity_score_100}/100) "
        f"local_capable={plan.local_capable} -> {plan.initial_route}"
    )
    if plan.classified_by == "local_llm":
        trace.append(
            f"local classifier: {plan.model_complexity} (keyword heuristic: {plan.heuristic_complexity})")
    trace.append(f"score={analysis.score} -> {plan.decision}")

    local: Optional[LLMResult] = None
    cloud: Optional[LLMResult] = None
    escalated = False
    escalation_reason = None
    escalation_kind = None
    escalations_used = 0
    local_failed = False
    local_error: Optional[Exception] = None
    output_valid = True

    if plan.initial_route in ("local_only", "local_first"):
        try:
            # Local model is private, so it can see the same optimized context
            if emit:
                emit("start", {"route": "local", "model": plan.local_model})
            local = await call_local(prompt, ctx.context, emit, detail)
            trace.append(f"local confidence={local.confidence} needs_cloud={local.needs_cloud} "
                         f"detail={detail} more_available={local.more_available}")
            if local.truncated:
                trace.append("local answer hit the length cap")
            output_valid = bool((local.answer or "").strip()) and not looks_like_refusal(local.answer)
            if not output_valid and (local.answer or "").strip():
                trace.append("local output looks like a refusal -> validation failure")
        except Exception as e:
            log.exception("local model failed")
            local_failed = True
            local_error = e
            trace.append(f"local model error: {e}")

        decision = routing.decide_escalation(
            requested=plan.requested,
            confidence=local.confidence if local else None,
            initial_route=plan.initial_route,
            needs_cloud=bool(local and local.needs_cloud),
            local_failed=local_failed,
            local_error=str(local_error) if local_error else None,
            output_valid=output_valid,
            cloud_allowed=plan.cloud_allowed,
            escalation_enabled=plan.escalate_allowed,
            escalations_used=escalations_used,
        )
        if decision.escalate:
            escalated = True
            escalation_reason = decision.reason
            escalation_kind = decision.kind
            escalations_used += 1
            trace.append(f"escalating ({decision.kind}): {decision.reason}")
        elif decision.reason:
            trace.append(f"no escalation: {decision.reason}")

    prompt_secrets = 0
    if plan.initial_route in ("cloud_only", "cloud_first") or escalated:
        # Only the cloud path needs redaction: local never leaves the machine
        cloud_prompt, prompt_secrets = redact_text(prompt)
        if emit:
            provider = effective_provider()
            emit("start", {"route": "cloud", "model": effective_model(provider) or provider,
                           "escalation_reason": escalation_reason if escalated else None})
        cloud = await call_cloud(cloud_prompt, ctx.context, emit)
        if cloud.fallback_reason:
            trace.append(f"cloud fallback: {cloud.fallback_reason}")

    final = cloud or local
    if final is None:
        if plan.requested == "local":
            raise RuntimeError(
                "local-only request could not be completed locally"
                + (f": {local_error}" if local_error else "")
                + "; cloud escalation is disabled by your preference")
        raise RuntimeError(escalation_reason or (str(local_error) if local_error else None) or "no model produced an answer")

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
        "escalation_kind": escalation_kind if escalated else None,
        "confidence": local.confidence if local else None,
        "confidence_threshold": config.MIN_LOCAL_CONFIDENCE,
        # Computed here so every frontend shows the same "low confidence" state.
        "low_confidence": bool(local and not cloud and local.confidence is not None
                               and local.confidence < config.MIN_LOCAL_CONFIDENCE),
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
        # --- explainable routing policy (Phases 3-6, 9) ---
        **plan.as_dict(),
        "final_route": "cloud" if cloud else "local",
        "escalations_used": escalations_used,
        "context_metadata": ctx.metadata,
        "trace": trace,
    }
