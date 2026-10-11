"""Central settings. Override any of these with environment variables (or a .env file)."""
import json
import os
from pathlib import Path


def _load_dotenv() -> None:
    env_path = Path(__file__).resolve().parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()

# Local LLM (Ollama)
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
LOCAL_MODEL = os.getenv("LOCAL_MODEL", "qwen2.5-coder:1.5b")
LOCAL_TIMEOUT_S = float(os.getenv("LOCAL_TIMEOUT_S", "120"))
LOCAL_NUM_CTX = int(os.getenv("LOCAL_NUM_CTX", "8192"))  # Ollama's default window silently truncates
# How long to keep the active local model resident between prompts. Only one is kept loaded at a
# time regardless: switching models unloads the previous one first.
LOCAL_KEEP_ALIVE = os.getenv("LOCAL_KEEP_ALIVE", "30m")

# Cloud (global) LLM: "deepseek", "anthropic", "openai" (any OpenAI-compatible API), or "mock"
CLOUD_PROVIDER = os.getenv("CLOUD_PROVIDER", "deepseek")
# If the real cloud call fails (no key, network, API error), surface the error instead of silently
# answering with the mock. Opt in to mock fallback explicitly with CLOUD_FALLBACK_TO_MOCK=true.
CLOUD_FALLBACK_TO_MOCK = os.getenv("CLOUD_FALLBACK_TO_MOCK", "false").lower() in ("1", "true", "yes")
# How long (seconds) a /health provider credential check is cached before re-pinging
CLOUD_HEALTHCHECK_TTL_S = float(os.getenv("CLOUD_HEALTHCHECK_TTL_S", "60"))
# How long (seconds) a provider /models listing is cached before re-fetching
CLOUD_MODELS_TTL_S = float(os.getenv("CLOUD_MODELS_TTL_S", "300"))
# Assumed output tokens used only for the pre-send cost estimate in /analyze
DEFAULT_OUTPUT_TOKENS_ESTIMATE = int(os.getenv("DEFAULT_OUTPUT_TOKENS_ESTIMATE", "600"))

DEEPSEEK_URL = os.getenv("DEEPSEEK_URL", "https://api.deepseek.com/chat/completions")
DEEPSEEK_API_KEY_ENV = "DEEPSEEK_API_KEY"
DEEPSEEK_API_KEY_PLACEHOLDER = "your-deepseek-api-key"
DEEPSEEK_API_KEY = os.getenv(DEEPSEEK_API_KEY_ENV, DEEPSEEK_API_KEY_PLACEHOLDER)
DEEPSEEK_DEFAULT_MODEL = "deepseek-chat"  # DeepSeek-V3
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", DEEPSEEK_DEFAULT_MODEL)
DEEPSEEK_MAX_TOKENS = 8192  # deepseek-chat's output cap; larger values are rejected

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5-5")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
# Custom OpenAI-compatible endpoint (Groq/OpenRouter/self-hosted). The base URL can also be
# supplied per request by the editor via the X-LLM-Base-Url header.
CUSTOM_API_KEY = os.getenv("CUSTOM_API_KEY", "")
CUSTOM_BASE_URL = os.getenv("CUSTOM_BASE_URL", "")
CUSTOM_MODEL = os.getenv("CUSTOM_MODEL", "")
CLOUD_TIMEOUT_S = float(os.getenv("CLOUD_TIMEOUT_S", "120"))
CLOUD_MAX_TOKENS = int(os.getenv("CLOUD_MAX_TOKENS", "16000"))

# Routing thresholds
LOCAL_THRESHOLD = float(os.getenv("LOCAL_THRESHOLD", "0.35"))  # score below this -> local
CLOUD_THRESHOLD = float(os.getenv("CLOUD_THRESHOLD", "0.65"))  # score above this -> cloud
MIN_LOCAL_CONFIDENCE = float(os.getenv("MIN_LOCAL_CONFIDENCE", "0.7"))  # below -> escalate

# Context budget for cloud requests
CLOUD_CONTEXT_TOKEN_BUDGET = int(os.getenv("CLOUD_CONTEXT_TOKEN_BUDGET", "3000"))
# Hard cap on context sent to a cloud model (kept well under its window to leave room for the
# answer). Complex/cloud tasks get a budget derived from the model window up to this cap; simple
# local-first tasks stay small. Set CLOUD_MAX_CONTEXT_TOKENS=3000 to restore the old behavior.
CLOUD_MAX_CONTEXT_TOKENS = int(os.getenv("CLOUD_MAX_CONTEXT_TOKENS", "16000"))
# Window assumed for a cloud model whose catalog entry has no context size.
CLOUD_MODEL_CONTEXT_DEFAULT = int(os.getenv("CLOUD_MODEL_CONTEXT_DEFAULT", "16000"))

# --- Codebase intelligence index (compression/codeindex) -----------------------------------
# When true, repo context is built from the persistent incremental index; on any failure the
# router falls back to the in-memory context engine, so this is safe to leave on.
CONTEXT_INDEX_ENABLED = os.getenv("CONTEXT_INDEX_ENABLED", "true").lower() in ("1", "true", "yes")
# Where per-repo index databases live (default: ~/.cache/localpilot/index). Empty = default.
CONTEXT_INDEX_DIR = os.getenv("CONTEXT_INDEX_DIR", "")
# Minimum edge confidence for graph expansion (a syntactic reference is below this on purpose).
CONTEXT_INDEX_MIN_CONFIDENCE = float(os.getenv("CONTEXT_INDEX_MIN_CONFIDENCE", "0.5"))
# Bounded local-model assistance (Stage 3): rerank a shortlist and propose missing names.
# Fail-open and cached; the index remains the source of truth.
CONTEXT_LLM_ASSIST = os.getenv("CONTEXT_LLM_ASSIST", "true").lower() in ("1", "true", "yes")
CONTEXT_LLM_RERANK_TOP_K = int(os.getenv("CONTEXT_LLM_RERANK_TOP_K", "24"))
CONTEXT_LLM_ASSIST_TIMEOUT_S = float(os.getenv("CONTEXT_LLM_ASSIST_TIMEOUT_S", "8"))
CONTEXT_LLM_ASSIST_TTL_S = float(os.getenv("CONTEXT_LLM_ASSIST_TTL_S", "300"))
# Sufficiency loop: how many times to fill/propose missing required context and re-assemble.
CONTEXT_SUFFICIENCY_MAX_ITERS = int(os.getenv("CONTEXT_SUFFICIENCY_MAX_ITERS", "2"))
# Semantic retrieval (Stage 2). Fail-open: skipped when no embedding backend is available.
CONTEXT_SEMANTIC_ENABLED = os.getenv("CONTEXT_SEMANTIC_ENABLED", "true").lower() in ("1", "true", "yes")
CONTEXT_EMBED_BACKEND = os.getenv("CONTEXT_EMBED_BACKEND", "auto")  # auto | ollama | st
CONTEXT_EMBED_MODEL = os.getenv("CONTEXT_EMBED_MODEL", "nomic-embed-text")
CONTEXT_EMBED_MAX_SYMBOLS = int(os.getenv("CONTEXT_EMBED_MAX_SYMBOLS", "2000"))

# --- Routing policy ------------------------------------------------------------------------
# Default routing preference when a request does not specify one: auto | local | cloud.
DEFAULT_ROUTE = os.getenv("DEFAULT_ROUTE", "auto").lower()
# Cloud execution master switch. When false, every request is served locally.
CLOUD_ENABLED = os.getenv("CLOUD_ENABLED", "true").lower() in ("1", "true", "yes")
# Hard privacy policy: when true, no prompt, code or conversation history may leave the machine,
# regardless of complexity. The router explains the limitation instead of escalating.
CLOUD_TRANSMISSION_PROHIBITED = os.getenv("CLOUD_TRANSMISSION_PROHIBITED", "false").lower() in ("1", "true", "yes")

# Zero-shot local-LLM tie-breaker for the medium band (see backend/classifier.py).
LOCAL_LLM_CLASSIFIER = os.getenv("LOCAL_LLM_CLASSIFIER", "true").lower() in ("1", "true", "yes")
LOCAL_LLM_CLASSIFIER_TIMEOUT_S = float(os.getenv("LOCAL_LLM_CLASSIFIER_TIMEOUT_S", "8"))
LOCAL_LLM_CLASSIFIER_TTL_S = float(os.getenv("LOCAL_LLM_CLASSIFIER_TTL_S", "300"))

# Post-response escalation (Phase 6). Confidence is one signal among several.
ESCALATION_ENABLED = os.getenv("ESCALATION_ENABLED", "true").lower() in ("1", "true", "yes")
# At most this many automatic cloud escalations per request (guards against routing loops).
MAX_CLOUD_ESCALATIONS = int(os.getenv("MAX_CLOUD_ESCALATIONS", "1"))
# What to do when the local answer carries no usable confidence: escalate | keep.
MISSING_CONFIDENCE_POLICY = os.getenv("MISSING_CONFIDENCE_POLICY", "escalate").lower()

# Complexity score = 100 * sum(weight_i * factor_i). These are configurable heuristics, NOT
# empirically optimal. Factors are each normalised to 0..1:
#   task_type (T), context_size (K), files (F), dependency (D), reasoning (R).
COMPLEXITY_WEIGHTS = json.loads(os.getenv("COMPLEXITY_WEIGHTS", json.dumps({
    "task_type": 0.25,
    "context_size": 0.20,
    "files": 0.20,
    "dependency": 0.20,
    "reasoning": 0.15,
})))
# The complexity score stays at least this fraction x task_type when hard keywords matched.
TASK_TYPE_FLOOR = float(os.getenv("TASK_TYPE_FLOOR", "0.8"))
# Same idea for localized (medium) task keywords: keeps "modify this function" out of the simple band.
MEDIUM_TASK_FLOOR = float(os.getenv("MEDIUM_TASK_FLOOR", "0.75"))
# 0..100 category boundaries: simple <= SIMPLE_MAX < medium <= MEDIUM_MAX < complex.
COMPLEXITY_SIMPLE_MAX = int(os.getenv("COMPLEXITY_SIMPLE_MAX", "34"))
COMPLEXITY_MEDIUM_MAX = int(os.getenv("COMPLEXITY_MEDIUM_MAX", "64"))

# --- Local model capability profile --------------------------------------------------------
# Usable context window of the local model (Ollama silently truncates beyond num_ctx).
LOCAL_MAX_CONTEXT_TOKENS = int(os.getenv("LOCAL_MAX_CONTEXT_TOKENS", str(LOCAL_NUM_CTX)))
# Reserved headroom when checking whether the relevant context fits the local window.
LOCAL_CONTEXT_MARGIN_TOKENS = int(os.getenv("LOCAL_CONTEXT_MARGIN_TOKENS", "512"))
# The bundled local execution path has no tool/function-calling support.
LOCAL_SUPPORTS_TOOL_CALLING = os.getenv("LOCAL_SUPPORTS_TOOL_CALLING", "false").lower() in ("1", "true", "yes")
# Optional per-model profile overrides, JSON: {"qwen2.5-coder:7b": {"max_context_tokens": 16384, ...}}
LOCAL_MODEL_PROFILES = json.loads(os.getenv("LOCAL_MODEL_PROFILES", "{}"))
# How long (seconds) an Ollama reachability probe is cached (capability checks query it often).
OLLAMA_REACH_TTL_S = float(os.getenv("OLLAMA_REACH_TTL_S", "5"))


def validate() -> None:
    """Fail fast on an invalid routing configuration (called on import)."""
    if DEFAULT_ROUTE not in ("auto", "local", "cloud"):
        raise ValueError(f"DEFAULT_ROUTE must be auto|local|cloud, got {DEFAULT_ROUTE!r}")
    if MISSING_CONFIDENCE_POLICY not in ("escalate", "keep"):
        raise ValueError(f"MISSING_CONFIDENCE_POLICY must be escalate|keep, got {MISSING_CONFIDENCE_POLICY!r}")
    if MAX_CLOUD_ESCALATIONS < 0:
        raise ValueError("MAX_CLOUD_ESCALATIONS must be >= 0")
    if not (0.0 <= LOCAL_THRESHOLD <= CLOUD_THRESHOLD <= 1.0):
        raise ValueError("require 0 <= LOCAL_THRESHOLD <= CLOUD_THRESHOLD <= 1")
    if not (0.0 <= MIN_LOCAL_CONFIDENCE <= 1.0):
        raise ValueError("MIN_LOCAL_CONFIDENCE must be in [0, 1]")
    weights = COMPLEXITY_WEIGHTS
    if not weights or any(not isinstance(v, (int, float)) or v < 0 for v in weights.values()):
        raise ValueError("COMPLEXITY_WEIGHTS must map factor names to non-negative numbers")
    if sum(weights.values()) <= 0:
        raise ValueError("COMPLEXITY_WEIGHTS must sum to a positive value")
    if not (0 <= COMPLEXITY_SIMPLE_MAX < COMPLEXITY_MEDIUM_MAX <= 100):
        raise ValueError("require 0 <= COMPLEXITY_SIMPLE_MAX < COMPLEXITY_MEDIUM_MAX <= 100")
    if not (0.0 <= MEDIUM_TASK_FLOOR <= 1.0):
        raise ValueError("MEDIUM_TASK_FLOOR must be in [0, 1]")
    if LOCAL_MAX_CONTEXT_TOKENS <= 0:
        raise ValueError("LOCAL_MAX_CONTEXT_TOKENS must be positive")
    if not (0.0 <= CONTEXT_INDEX_MIN_CONFIDENCE <= 1.0):
        raise ValueError("CONTEXT_INDEX_MIN_CONFIDENCE must be in [0, 1]")


validate()

# POST /chat/stream sends an SSE comment this often (seconds) while no tokens are flowing (e.g. a
# cold local model loading), so clients can use a short idle timeout instead of a huge total one.
SSE_HEARTBEAT_S = float(os.getenv("SSE_HEARTBEAT_S", "15"))
