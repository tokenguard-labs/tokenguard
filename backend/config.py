"""Central settings. Override any of these with environment variables (or a .env file)."""
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

# POST /chat/stream sends an SSE comment this often (seconds) while no tokens are flowing (e.g. a
# cold local model loading), so clients can use a short idle timeout instead of a huge total one.
SSE_HEARTBEAT_S = float(os.getenv("SSE_HEARTBEAT_S", "15"))
