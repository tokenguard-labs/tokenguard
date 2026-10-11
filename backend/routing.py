"""Explainable, cost-aware routing policy for LocalPilot.

Three decisions are kept deliberately separate:

    A. ROUTING PREFERENCE   (auto | local | cloud)                     -- explicit user intent
    B. INITIAL ROUTING      (local_first | cloud_first | local_only | cloud_only)
                                                                       -- capability + complexity
    C. POST-RESPONSE ESCALATION (return local | escalate | explain)    -- confidence + validation

Layer A (hard policy) runs *before* any complexity score and takes precedence over it. A
complexity classifier that confidently labels a request "simple" never overrides an explicit
local-only preference or a privacy restriction, and a high self-reported confidence never
overrides an objective validation failure.

Everything here is deterministic and network-free so the policy can be unit-tested with mocked
capabilities. The router supplies the runtime facts (model reachability, cloud permission).
"""
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from . import analyzer, config

log = logging.getLogger("localpilot.routing")

RoutingPreference = str                                   # auto | local | cloud
InitialRoute = str                                        # local_first | cloud_first | local_only | cloud_only
Complexity = str                                          # simple | medium | complex
EscalationKind = str                                      # confidence | needs_cloud | validation | error | missing_confidence

_PARAM_RE = re.compile(r"(\d+(?:\.\d+)?)\s*b\b")


@dataclass(frozen=True)
class ModelProfile:
    """A small, verifiable capability profile for a local model.

    Fields reflect measured/known behaviour, not assumptions baked into the model name (except
    the parameter count, which is read from the id as a starting heuristic).
    """
    model_id: str
    params_billions: Optional[float] = None
    max_context_tokens: int = config.LOCAL_MAX_CONTEXT_TOKENS
    supports_tool_calling: bool = config.LOCAL_SUPPORTS_TOOL_CALLING
    supports_code_generation: bool = True
    supports_multifile_reasoning: str = "limited"          # yes | limited | no
    healthy: bool = True
    source: str = "heuristic"                              # heuristic | override

    def as_dict(self) -> Dict[str, Any]:
        return {
            "model_id": self.model_id,
            "params_billions": self.params_billions,
            "max_context_tokens": self.max_context_tokens,
            "supports_tool_calling": self.supports_tool_calling,
            "supports_code_generation": self.supports_code_generation,
            "supports_multifile_reasoning": self.supports_multifile_reasoning,
            "healthy": self.healthy,
            "source": self.source,
        }


_PROFILE_CACHE: Dict[tuple, ModelProfile] = {}


def _params_billions(model_id: str) -> Optional[float]:
    match = _PARAM_RE.search(model_id.lower())
    return float(match.group(1)) if match else None


def profile_for(model_id: str, healthy: bool = True) -> ModelProfile:
    """Capability profile for a local model id, honouring config.LOCAL_MODEL_PROFILES overrides."""
    key = (model_id, bool(healthy))
    cached = _PROFILE_CACHE.get(key)
    if cached is not None:
        return cached

    params = _params_billions(model_id)
    if params is None:
        multifile, code_gen = "limited", True
    else:
        multifile = "yes" if params >= 13 else ("limited" if params >= 3 else "no")
        code_gen = params >= 1.0
    profile = ModelProfile(
        model_id=model_id,
        params_billions=params,
        supports_code_generation=code_gen,
        supports_multifile_reasoning=multifile,
        healthy=healthy,
    )

    overrides = _model_overrides().get(model_id)
    if overrides:
        unknown = set(overrides) - set(profile.as_dict())
        if unknown:
            log.warning("ignoring unknown profile fields for %s: %s", model_id, sorted(unknown))
        profile = ModelProfile(
            model_id=model_id,
            params_billions=overrides.get("params_billions", profile.params_billions),
            max_context_tokens=int(overrides.get("max_context_tokens", profile.max_context_tokens)),
            supports_tool_calling=bool(overrides.get("supports_tool_calling", profile.supports_tool_calling)),
            supports_code_generation=bool(overrides.get("supports_code_generation", profile.supports_code_generation)),
            supports_multifile_reasoning=str(
                overrides.get("supports_multifile_reasoning", profile.supports_multifile_reasoning)),
            healthy=healthy,
            source="override",
        )
    _PROFILE_CACHE[key] = profile
    return profile


def _model_overrides() -> Dict[str, Dict[str, Any]]:
    raw = config.LOCAL_MODEL_PROFILES
    if not isinstance(raw, dict):
        log.warning("LOCAL_MODEL_PROFILES is not a JSON object; ignoring")
        return {}
    return {k: v for k, v in raw.items() if isinstance(v, dict)}


@dataclass
class CapabilityCheck:
    local_capable: bool
    reasons: List[str] = field(default_factory=list)
    profile: Optional[ModelProfile] = None


def check_capability(
    profile: ModelProfile,
    *,
    required_context_tokens: int,
    local_available: bool = True,
    requires_tools: bool = False,
    requires_multifile: bool = False,
) -> CapabilityCheck:
    """Can the local model reasonably execute this request? Pure, no model calls."""
    reasons: List[str] = []
    capable = True

    if not local_available:
        capable = False
        reasons.append("local model is not reachable")
    if requires_tools and not profile.supports_tool_calling:
        capable = False
        reasons.append("task needs tool/function calling, unsupported on the local path")
    if requires_multifile:
        if profile.supports_multifile_reasoning == "no":
            capable = False
            reasons.append(f"task spans multiple files; {profile.model_id} handles this unreliably")
        elif profile.supports_multifile_reasoning == "limited":
            reasons.append("multi-file task; local model capability is limited (will verify with escalation)")

    budget = profile.max_context_tokens - config.LOCAL_CONTEXT_MARGIN_TOKENS
    if required_context_tokens > budget:
        capable = False
        reasons.append(
            f"relevant context ~{required_context_tokens} tokens exceeds the local window "
            f"({budget} usable of {profile.max_context_tokens})")

    if capable and not reasons:
        reasons.append("local model is capable for this request")
    return CapabilityCheck(local_capable=capable, reasons=reasons, profile=profile)


@dataclass
class RoutePlan:
    requested: RoutingPreference
    complexity: Complexity                                 # effective category (heuristic or tie-breaker)
    complexity_score: float
    complexity_score_100: int
    initial_route: InitialRoute
    decision: str                                          # legacy: local | local_first | cloud
    local_capable: bool
    cloud_allowed: bool
    escalate_allowed: bool
    reasons: List[str]
    capability_reasons: List[str]
    factors: Dict[str, float]
    analysis: analyzer.Analysis
    profile: ModelProfile
    local_model: str
    estimated_context_tokens: int
    requires_tools: bool
    requires_multifile: bool
    heuristic_complexity: Complexity = "medium"
    model_complexity: Optional[Complexity] = None          # set when the local-LLM tie-breaker ran
    model_complexity_reason: Optional[str] = None          # the classifier's own one-line reason
    classified_by: str = "heuristic"                       # heuristic | local_llm

    def as_dict(self) -> Dict[str, Any]:
        return {
            "requested_route": self.requested,
            "complexity": self.complexity,
            "heuristic_complexity": self.heuristic_complexity,
            "model_complexity": self.model_complexity,
            "model_complexity_reason": self.model_complexity_reason,
            "classified_by": self.classified_by,
            "complexity_score": self.complexity_score,
            "complexity_score_100": self.complexity_score_100,
            "initial_route": self.initial_route,
            "decision": self.decision,
            "local_capable": self.local_capable,
            "cloud_allowed": self.cloud_allowed,
            "escalate_allowed": self.escalate_allowed,
            "routing_reasons": list(self.reasons),
            "capability_reasons": list(self.capability_reasons),
            "factors": dict(self.factors),
            "task_type": self.analysis.task_type,
            "reasons": list(self.analysis.reasons),
            "local_model": self.local_model,
            "model_profile": self.profile.as_dict(),
            "estimated_context_tokens": self.estimated_context_tokens,
            "requires_tools": self.requires_tools,
            "requires_multifile": self.requires_multifile,
        }


_DECISION = {
    "local_only": "local",
    "local_first": "local_first",
    "cloud_first": "cloud",
    "cloud_only": "cloud",
}


def plan_route(
    requested: RoutingPreference,
    *,
    prompt: str,
    analysis: analyzer.Analysis,
    estimated_context_tokens: int,
    local_model: str,
    local_available: bool = True,
    cloud_allowed: bool = True,
    requires_tools: Optional[bool] = None,
    requires_multifile: Optional[bool] = None,
    complexity_hint: Optional[Complexity] = None,
    model_complexity: Optional[Complexity] = None,
    model_complexity_reason: Optional[str] = None,
) -> RoutePlan:
    """Layer A (hard policy) + Layer B (capability + complexity) -> an initial route.

    `requested` is the explicit preference (auto | local | cloud). Any value other than the three
    known modes is treated as `auto`. `complexity_hint` is an optional tie-breaker verdict from the
    local-LLM classifier; when supplied it overrides the keyword heuristic's category.
    """
    if requested not in ("auto", "local", "cloud"):
        requested = "auto"
    category = complexity_hint if complexity_hint in ("simple", "medium", "complex") else analysis.complexity
    prompt_l = prompt.lower()
    if requires_tools is None:
        requires_tools = analyzer.requires_tools(prompt_l)
    if requires_multifile is None:
        requires_multifile = analysis.factors.get("dependency", 0.0) >= 0.6

    profile = profile_for(local_model, healthy=local_available)
    cap = check_capability(
        profile,
        required_context_tokens=estimated_context_tokens,
        local_available=local_available,
        requires_tools=requires_tools,
        requires_multifile=requires_multifile,
    )

    reasons: List[str] = []
    if category != analysis.complexity:
        reasons.append(
            f"local classifier judged this {category} (keyword heuristic said {analysis.complexity})")
    if model_complexity_reason:
        reasons.append(f"local classifier reason: {model_complexity_reason}")
    if requested == "local":
        initial, escalate_allowed = "local_only", False
        reasons.append("explicit local-only preference: no automatic cloud escalation")
    elif requested == "cloud":
        if cloud_allowed:
            initial, escalate_allowed = "cloud_only", False
            reasons.append("explicit cloud-only preference: skipping local execution")
        else:
            initial, escalate_allowed = "local_only", False
            reasons.append("cloud requested but not permitted/available: running locally instead")
    elif not cloud_allowed:
        initial = "local_first" if cap.local_capable else "local_only"
        escalate_allowed = False
        reasons.append("cloud transmission not permitted: capping this request at local execution")
    elif not cap.local_capable:
        initial, escalate_allowed = "cloud_first", False
        reasons.append("local model is not capable for this request: routing to the cloud")
    elif category == "complex":
        initial, escalate_allowed = "cloud_first", False
        reasons.append("complex task: cloud-first (when permitted)")
    else:
        initial = "local_first"
        escalate_allowed = config.ESCALATION_ENABLED
        suffix = " with confidence-based escalation" if escalate_allowed else ""
        reasons.append(f"{category} task: local-first{suffix}")
    reasons.extend(cap.reasons)

    return RoutePlan(
        requested=requested,
        complexity=category,
        complexity_score=analysis.score,
        complexity_score_100=analysis.complexity_score_100,
        initial_route=initial,
        decision=_DECISION[initial],
        local_capable=cap.local_capable,
        cloud_allowed=cloud_allowed,
        escalate_allowed=escalate_allowed,
        reasons=reasons,
        capability_reasons=cap.reasons,
        factors=analysis.factors,
        analysis=analysis,
        profile=profile,
        local_model=local_model,
        estimated_context_tokens=estimated_context_tokens,
        requires_tools=requires_tools,
        requires_multifile=requires_multifile,
        heuristic_complexity=analysis.complexity,
        model_complexity=model_complexity,
        model_complexity_reason=model_complexity_reason,
        classified_by="local_llm" if model_complexity else "heuristic",
    )


@dataclass
class EscalationDecision:
    escalate: bool
    reason: Optional[str] = None
    kind: Optional[EscalationKind] = None


def _valid_confidence(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and 0.0 <= float(value) <= 1.0


def decide_escalation(
    *,
    requested: RoutingPreference,
    confidence: Any,
    initial_route: InitialRoute = "local_first",
    needs_cloud: bool = False,
    local_failed: bool = False,
    local_error: Optional[str] = None,
    output_valid: bool = True,
    cloud_allowed: bool = True,
    escalation_enabled: Optional[bool] = None,
    escalations_used: int = 0,
    threshold: Optional[float] = None,
    missing_policy: Optional[str] = None,
) -> EscalationDecision:
    """Layer C: should a locally produced answer be escalated to the cloud?

    Kept independent from Layer B so routing and answer-quality decisions can be reasoned about,
    tested and tuned separately. `needs_cloud`/`local_failed` take precedence over confidence;
    an explicit local-only preference and privacy restrictions are never overridden.
    """
    if escalation_enabled is None:
        escalation_enabled = config.ESCALATION_ENABLED
    if threshold is None:
        threshold = config.MIN_LOCAL_CONFIDENCE
    if missing_policy is None:
        missing_policy = config.MISSING_CONFIDENCE_POLICY

    def no(reason: str) -> EscalationDecision:
        return EscalationDecision(False, reason, None)

    def yes(reason: str, kind: EscalationKind) -> EscalationDecision:
        return EscalationDecision(True, reason, kind)

    if requested == "local":
        return no("explicit local-only request: cloud escalation is disabled")
    if not escalation_enabled:
        return no("automatic escalation is disabled")
    if escalations_used >= config.MAX_CLOUD_ESCALATIONS:
        return no(f"escalation limit reached ({config.MAX_CLOUD_ESCALATIONS})")
    if not cloud_allowed:
        return no("cloud transmission is not permitted")
    if local_failed:
        return yes(f"local model failed: {local_error or 'unknown error'}", "error")
    if output_valid is False:
        return yes("local output failed validation", "validation")
    if needs_cloud:
        return yes("local model flagged needs_cloud", "needs_cloud")
    if not _valid_confidence(confidence):
        if missing_policy == "escalate":
            return yes("local confidence unavailable (missing-confidence policy: escalate)", "missing_confidence")
        return no("confidence unavailable but missing-confidence policy is keep")
    if float(confidence) < threshold:
        return yes(f"local confidence {confidence} < {threshold}", "confidence")
    return no(f"local confidence {confidence} >= {threshold}")
